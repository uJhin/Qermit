# Copyright 2019-2021 Cambridge Quantum Computing
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from pytket import OpType, Circuit
from pytket.passes import RebaseUFR, DecomposeBoxes  # type: ignore
from pytket.backends import Backend
from pytket.utils import QubitPauliOperator, get_operator_expectation_value
from typing import List, Tuple
import copy
from qermit import (
    MitEx,
    ObservableTracker,
    SymbolsDict,
    MitTask,
    AnsatzCircuit,
    ObservableExperiment,
    TaskGraph,
)
from qermit.taskgraph import gen_compiled_MitRes
from .cdr_post import (
    cdr_calibration_task_gen,
    cdr_correction_task_gen,
    _PolyCDRCorrect,
    cdr_quality_check_task_gen,
)
import numpy as np
import random
from enum import Enum
import warnings


class LikelihoodFunction(Enum):
    def none(
        self, qpo_noisy: QubitPauliOperator, qpo_exact: QubitPauliOperator
    ) -> float:
        """
        Returns probability 1 of accepting returned results.

        :param qpo_noisy: Results calculated from device of choice.
        :type qpo_noisy: QubitPauliOperators
        :param qpo_exact: Results calculated from noiseless simulator of choice.
        :type qpo_exact: QubitPauliOperator

        :return: Always 1, meaning any result is accepted.
        :rtype: float
        """
        return 1


def sample_weighted_clifford_angle(rz_angle: float, **kwargs) -> float:
    """
    Calculates a weights distribution over different possible Clifford gates from input gate.
    Clifford gates prepared by taking S gate to the power of n in {0,4}.
    n value sampled from calculated weights distribution.
    Distribution calculation as in B1, page 6 arXiv:2005.10189.

    :param rz_angle: Angle of rotation in rz axis.
    :type rz_angle: float
    :key: seed

    :return: An angle corresponding to Clifford rotation of some Rz gate
    :rtype: float
    """
    if "seed" in kwargs:
        random.seed(kwargs.get("seed"))

    rz_angle = rz_angle % 2
    rz_angle_matrix = np.asarray(
        [
            [np.exp(-0.5 * np.pi * rz_angle * 1j), 0],
            [0, np.exp(0.5 * np.pi * rz_angle * 1j)],
        ]
    )
    weights = []
    for n in range(4):
        sn_matrix = np.asarray(
            [[np.exp(-0.25 * np.pi * n * 1j), 0], [0, np.exp(0.25 * np.pi * n * 1j)]]
        )
        d = np.linalg.norm(rz_angle_matrix - sn_matrix)
        weights.append(np.exp((-(d ** 2)) * 4))
    return 0.5 * random.choices(range(4), weights)[0]


def gen_state_circuits(
    c: Circuit, n_non_cliffords: int, n_pairs: int, total_state_circuits: int, **kwargs
) -> List[Circuit]:
    """
    For given circuit c, returns total_state_circuits number of circuits, where each circuit is
    run through some MitEx object to provide characterisation data for later correction.

    State circuit construction as in appendix B of arXiv:2005.10189.
    State circuits are generated via a Markov Chain Monte Carlo technique.
    Circuit c is first rebased into a basis set of CX, H and Rz gates.
    Circuit c is then modified to a near Clifford Circuit with only n_non_cliffords
    number of Rz gates with non-Clifford angles.
    The near Clifford circuit is generated by randomly choosing n_non_cliffords non-Clifford Rz
    gates in c to retain their angle, and then replacing all other non-Clifford
    Rz gates with some random Clifford gate.
    Then, for each update step to generate a new state circuit the following occurs:
    • n_pairs of pairs of gates in c are randomly chosen
    • each pair consists of one Rz gate with non-Clifford angle (amongst the n_non_cliffords)
    and one Rz gate with Clifford angle that was originally non-Clifford.
    • The Rz gate with Clifford angle has its angle replaced with its original non-Clifford angle
    • The non-Clifford Rz gate angle has its angle replaced with a Clifford angle
    • A Metropolis-Hastings rule is used to accept or reject this new state circuit late when results gathered


    :param c: Circuit for producing state circuits from.
    :type c: Circuit
    :param n_non_cliffords: Number of non-Clifford gates in resulting characterisation state circuits
    :type n_non_cliffords: int
    :param n_pairs: Pairs of Clifford, Non-Clifford gates in state circuit generated.
    :type n_pairs: int
    :param total_state_circuits: Total number of state circuits to be produced for characterisation
    :type total_state_circuits: int
    :key: seed for random methods

    :return: All generated state circuits
    :rtype: List[Circuit]
    """
    # set seed if given
    if "seed" in kwargs:
        random.seed(kwargs.get("seed"))
        np.random.seed(kwargs.get("seed"))

    # Work in CX, H, Rz basis for ease
    DecomposeBoxes().apply(c)
    RebaseUFR().apply(c)
    c.flatten_registers()
    all_coms = c.get_commands()

    #  angles that make Clifford gates for S^n
    clifford_angles = set({0, 0.5, 1.0, 1.5, 2, 2.5, 3, 3.5})

    if n_pairs > n_non_cliffords:
        raise ValueError(
            "More pairs {} than total non-clifford gates {}. Number of pairs must be less than or equal to.".format(
                n_pairs, n_non_cliffords
            )
        )
    # Admin for circuit modifications
    # Create a register of ints corresponding to indices of commands list with non-Clifford Rz gates
    rz_ops = set()
    for i in range(len(all_coms)):
        if all_coms[i].op.type == OpType.Rz:
            if all_coms[i].op.params[0] not in clifford_angles:
                rz_ops.add(i)
    if len(rz_ops) == 0:
        return [c] * total_state_circuits

    state_circuits: List[Circuit] = []
    if len(rz_ops) == 1:
        # make special case, just constantly
        # keep on producing state circuits until limit reached
        while len(state_circuits) < total_state_circuits:
            new_circuit = Circuit(c.n_qubits, len(c.bits))
            for i in range(len(all_coms)):
                com = all_coms[i]
                if com.op.type == OpType.Rz:
                    new_circuit.add_barrier(com.qubits)
                    angle = sample_weighted_clifford_angle(com.op.params[0])
                    new_circuit.add_gate(com.op.type, [angle], com.qubits)
                    new_circuit.add_barrier(com.qubits)
                # Measure gate has special case, but can assume 1 qubit to 1 bit
                elif com.op.type is OpType.Measure:
                    new_circuit.Measure(com.qubits[0], com.bits[0])
                # A special case for Barrier metaop
                elif com.op.type == OpType.Barrier:
                    new_circuit.add_barrier(com.args)
                # CX or H gate, add as is
                else:
                    new_circuit.add_gate(com.op.type, com.qubits)

            # all circuits accepted and run, some results later discarded if not accepted by Metropolis-Hastings rule
            state_circuits.append(new_circuit)
        return state_circuits

    n_non_cliffords = min(n_non_cliffords, len(rz_ops) - 1)
    n_cliffords = len(rz_ops) - n_non_cliffords

    n_pairs = min(n_cliffords, n_non_cliffords, n_pairs)

    # non_cliffords are indices for gates to be left non Clifford
    non_cliffords = np.random.choice(list(rz_ops), n_non_cliffords, replace=False)

    # rz_ops then only contains rz gates in c to be substituted for Clifford angles
    rz_ops.difference_update(non_cliffords)
    # Power of random Clifford gates to be substituted
    cliffords = {num: random.randint(0, 4) for num in rz_ops}

    # keep on producing state circuits until limit reached
    while len(state_circuits) < total_state_circuits:
        # cliffords.keys() are integers for now Clifford gates
        # sample some set of these to be subbed for original non-Clifford angle
        clifford_pair_elements = random.sample(list(cliffords.keys()), n_pairs)
        # from remaining non-Clifford Rz gates, sample some to have random Clifford gate
        non_clifford_pair_elements = random.sample(list(non_cliffords), n_pairs)

        # create new Circuit from scratch
        new_circuit = Circuit(c.n_qubits, len(c.bits))
        for i in range(len(all_coms)):
            com = all_coms[i]
            if com.op.type == OpType.Rz:
                new_circuit.add_barrier(com.qubits)
                # 3 sets of gates int must be in
                # in clifford_pair_elements means gate has been denominated as Clifford,
                # but is in some sampled pair so add original angle
                if i in clifford_pair_elements:
                    new_circuit.add_gate(com.op.type, com.op.params, com.qubits)
                    new_circuit.add_barrier(com.qubits)
                # in non_clifford_pair_elements mean gate was denominated to be left non-Clifford,
                # but its value has been sampled in a pair to now be Clifford
                # random angle is sampled and returned
                elif i in non_clifford_pair_elements:
                    angle = sample_weighted_clifford_angle(com.op.params[0])
                    new_circuit.add_gate(com.op.type, [angle], com.qubits)
                    new_circuit.add_barrier(com.qubits)
                # in cliffords mean it is denominated as Clifford, and hasn't been sampled for a pair
                # as clifford_pair_elements has already been checked
                # in this case, cliffords is a dict between Rz index and substitution S power
                # get power from dict, multiply by 0.5 to get angle, add to circuit
                elif i in cliffords:
                    new_circuit.add_gate(com.op.type, [0.5 * cliffords[i]], com.qubits)
                    new_circuit.add_barrier(com.qubits)
                # final case means gate was chosen to retain non-Clifford, and has not been
                # sampled in any pair, so add original angle.
                else:
                    new_circuit.add_gate(com.op.type, com.op.params, com.qubits)
                    new_circuit.add_barrier(com.qubits)
            # Measure gate has special case, but can assume 1 qubit to 1 bit
            elif com.op.type is OpType.Measure:
                new_circuit.Measure(com.qubits[0], com.bits[0])
            # A special case for Barrier metaop
            elif com.op.type == OpType.Barrier:
                new_circuit.add_barrier(com.args)
            # CX or H gate, add as is
            else:
                new_circuit.add_gate(com.op.type, com.qubits)

        # all circuits accepted and run, some results later discarded if not accepted by Metropolis-Hastings rule
        state_circuits.append(new_circuit)

    return state_circuits


def ccl_state_task_gen(
    n_non_cliffords: int,
    n_pairs: int,
    total_state_circuits: int,
    simulator_backend: Backend,
    tolerance: float,
    max_state_circuits_attempts: int,
) -> MitTask:
    """
    Returns a MitTask object for which given some set of experiments,
    for each experiment prepares a set of state circuits for Clifford Circuit Learning characterisation.
    The original experiment is returned on the first wire, state circuits for running on backend on second wire,
    and state circuits for noiseless simulation on the third wire.

    :param n_non_cliffords: Number of remaining non-Clifford gates in generated State Circuits.
    :type n_non_cliffords: int
    :param n_pairs: Parameter used for guiding properties of State Circuits generated.
    :type n_pairs:
    :param total_state_circuits: Number of state circuits prepared for characterisation.
    :type total_state_circuits: int
    :param tolerance: Model can be perturbed when calibration circuits have by
        exact expectation values too close to each other. This parameter
        sets a distance between exact expectation values which at least some
        calibration circuits should have.
    :type tolerance: float
    :param simulator_backend: Backend object simulated characterisation experiments are
        default run through.
    :type simulator_backend: Backend
    :param max_state_circuits_attempts: The maximum number of times to attempt to generate a
        list of calibrations circuit with significantly different expectation
        values, before resorting to a list with similar expectation values.
    :type max_state_circuits_attempts: int

    :return: MitTask object for preparing and returning state circuits for characterisation.
    :rtype: MitTask
    """

    def task(
        obj,
        experiment_wires: List[ObservableExperiment],
    ) -> Tuple[
        List[ObservableExperiment],
        List[ObservableExperiment],
        List[ObservableExperiment],
    ]:
        """
        :param experiment_wires: Information used to define generic experiments in MitEx objects.
        :type experiment_wires: List[ObservableExperiment]

        :return: Original experiment for running on experiment backend, state circuits for running on characterisation backend,
        state circuits for running on noiseless backend.
        :rtype: Tuple[List[ObservableExperiment], List[ObservableExperiment], List[ObservableExperiment],]
        """
        simulator_wires = []
        device_wires = []
        for measurement_wire in experiment_wires:
            ansatz_circuit = measurement_wire.AnsatzCircuit
            shots = ansatz_circuit.Shots
            qubit_pauli_operator = (
                measurement_wire.ObservableTracker.qubit_pauli_operator
            )
            # generate all state circuits
            c_copy = ansatz_circuit.Circuit.copy()
            c_copy.symbol_substitution(ansatz_circuit.SymbolsDict._symbolic_map)

            all_close = True
            attempt = 0
            while all_close and attempt < max_state_circuits_attempts:

                state_circuits = gen_state_circuits(
                    c_copy,
                    n_non_cliffords,
                    n_pairs,
                    total_state_circuits,
                )

                pauli_expectation_list = [
                    get_operator_expectation_value(
                        c, qubit_pauli_operator, simulator_backend
                    )
                    for c in state_circuits
                ]
                all_close = all(
                    abs(pauli_expectation - pauli_expectation_list[0]) <= tolerance
                    for pauli_expectation in pauli_expectation_list
                )

                attempt += 1

            if all_close:
                warnings.warn(
                    "Clifford Data Regression performs best when the exact expectation values of all calibration circuits are not the same. However, the generated calibration circuits have similar exact expectation values. Fit of the extrapolation function may be poor as a result."
                )

            # for each state circuit, create a new wire of for each state circuit
            # one for simulator, one for device
            for c in state_circuits:
                wire_sim = ObservableExperiment(
                    AnsatzCircuit=AnsatzCircuit(
                        Circuit=c, Shots=shots, SymbolsDict=SymbolsDict()
                    ),
                    ObservableTracker=ObservableTracker(
                        copy.copy(qubit_pauli_operator)
                    ),
                )
                wire_device = ObservableExperiment(
                    AnsatzCircuit=AnsatzCircuit(
                        Circuit=c.copy(),
                        Shots=copy.copy(shots),
                        SymbolsDict=SymbolsDict(),
                    ),
                    ObservableTracker=ObservableTracker(
                        copy.copy(qubit_pauli_operator)
                    ),
                )
                simulator_wires.append(wire_sim)
                device_wires.append(wire_device)
        return (experiment_wires, simulator_wires, device_wires)

    return MitTask(
        _label="CCL_State_Circuits",
        _n_in_wires=1,
        _n_out_wires=3,
        _method=task,
    )


def ccl_result_batching_task_gen(n_state_circuits: int) -> MitTask:
    """
    For each experiment run through MitEx, pairs up noisy and noiseless expectation values
    from state circuits for that experiments CCL calibration and then returns results for a single
    calibration in a single list.

    :param n_state_circuits: Number of state circuits initially prepared for each
        experiment characterisation.
    :type n_state_circuits: int

    :return: MitTask object that organises QubitPauliOperator objects required for
        characterisation.
    :rtype: MitTask.
    """

    def task(
        obj, exact_exp: List[QubitPauliOperator], noisy_exp: List[QubitPauliOperator]
    ) -> Tuple[List[List[Tuple[QubitPauliOperator, QubitPauliOperator]]]]:
        """
        :param noisy_exp: All QubitPauliOperators returned from running state circuit calibrations for all experiments through device.
        :type noisy_exp: List[QubitPauliOperator]
        :param exact_exp: All QubitPauliOperators returned from running state circuit calibrations for all experiments through noiseless simulator.
        :type exact_exp: List[QubitPauliOperator]

        :return: State circuit results split into separate lists for each experiment, with noisy and noiseless expectations paired together.
        :rtype: Tuple[List[List[Tuple[QubitPauliOperator, QubitPauliOperator]]]]
        """
        if len(noisy_exp) != len(exact_exp):
            raise RuntimeError(
                "Batching task should receive identical number of Simulated and Device run results."
            )

        zipped = list(zip(noisy_exp, exact_exp))
        chunked_zipped = [
            zipped[i : i + n_state_circuits]
            for i in range(0, len(zipped), n_state_circuits)
        ]
        return (chunked_zipped,)

    return MitTask(
        _label="CCLBatchResults", _n_in_wires=2, _n_out_wires=1, _method=task
    )


def ccl_likelihood_filtering_task_gen(
    likelihood_function: LikelihoodFunction, **kwargs
) -> MitTask:
    """
    :param likelihood_function: LikelihoodFunction enum used to accept or reject some pair of noisy and noiseless expectation.
        Function must take two QubitPauliOperator as parameter, and return a single float between 0 and 1 as answer.
    :type likelihood_function: LikelihoodFunction
    :key seed: Seed value for sampling probability for likelihood function

    :return: MitTask object that removes some characterisation results under some
        condition set by the likelihood_function option.
    :rtype: MitTask
    """

    def task(
        obj,
        state_circuit_exp: List[List[Tuple[QubitPauliOperator, QubitPauliOperator]]],
    ) -> Tuple[List[List[Tuple[QubitPauliOperator, QubitPauliOperator]]]]:
        """
        For each combination of noisy and noiseless expectation value
        for some state circuit, use a Metropolis-Hastings rule with
        given likelihood function to accept or reject the result.
        In this manner, this task filters unwanted results, returning only
        accepted expectations for calibrating from.

        :param state_circuit_exp: Noisy and Noiseless Expectation results for calibration.
        :type state_circuit_exp: List[List[Tuple[QubitPauliOperator, QubitPauliOperator]]]

        :return: Filtered calibration results.
        :rtype: Tuple[List[List[Tuple[QubitPauliOperator, QubitPauliOperator]]]]
        """
        if likelihood_function == LikelihoodFunction.none:
            return (state_circuit_exp,)
        else:
            if "seed" in kwargs:
                random.seed(kwargs.get("seed"))
            filtered_results = []
            for exp in state_circuit_exp:
                filtered_experiment = []
                for noisy, exact in exp:
                    likelihood_res = likelihood_function(noisy, exact)  # type: ignore
                    if random.uniform(0, 1) < likelihood_res:
                        filtered_experiment.append((noisy, exact))
                filtered_results.append(filtered_experiment)
            return (filtered_results,)

    return MitTask(
        _label="CCLLikelihoodFilterResults", _n_in_wires=1, _n_out_wires=1, _method=task
    )


def gen_CDR_MitEx(
    device_backend: Backend,
    simulator_backend: Backend,
    n_non_cliffords: int,
    n_pairs: int,
    total_state_circuits: int,
    **kwargs
) -> MitEx:
    """
    Produces a MitEx object for applying Clifford Circuit Learning & Clifford Data Regression
    mitigation methods when calculating expectation values of observables. Implementation as
    in arXiv:2005.10189.

    :param device_backend: Backend object device experiments are default run through.
    :type device_backend: Backend
    :param simulator_backend: Backend object simulated characterisation experiments are
        default run through.
    :type simulator_backend: Backend
    :param n_non_cliffords: Number of gates in Ansatz Circuit left as non-Clifford gates when
        producing characterisation circuits.
    :type n_non_cliffords: int
    :param n_pairs: Number of non-Clifford gates sampled to become Clifford and vice versa
        each time a new state circuit is generated.
    :type n_pairs: int
    :param total_state_circuits: Total number of state circuits produced for characterisation.
    :type total_state_circuits: int

    :key StatesSimulatorMitex: MitEx object noiseless characterisation simulations are executed on, default
        simulator_backend with basic compilation of circuit.
    :key StatesDeviceMitex: MitEx object noisy characterisation circuit are executed on, default
        device_backend with basic compilation of circuit.
    :key ExperimentMitex: MitEx object that actual experiment circuits are executed on, default
        backend with some compilation of circuit.
    :key model: Model characterised by state circuits, default _PolyCDRCorrect(1) (see cdr_post.py for other options).
    :key likelihood_function: LikelihoodFunction used to filter state circuit results, given by a LikelihoodFunction Enum,
        default set to none.
    :key tolerance: Model can be perturbed when calibration circuits have by
        exact expectation values too close to each other. This parameter
        sets a distance between exact expectation values which at least some
        calibration circuits should have.
    :key distance_tolerance: The absolute tolerance on the distance between
        expectation values of the calibration and original circuit.
    :key calibration_fraction: The upper bound on the fraction of calibration
        circuits which have noisy expectation values far from that of the
        original circuit.
    """
    
    _optimisation_level = kwargs.get("optimisation_level", 0)

    _states_sim_mitex = copy.copy(
        kwargs.get(
            "states_simluator_mitex",
            MitEx(
                simulator_backend,
                _label="StatesSimMitex",
                mitres=gen_compiled_MitRes(simulator_backend, _optimisation_level),
            ),
        )
    )
    _states_device_mitex = copy.copy(
        kwargs.get(
            "states_device_mitex",
            MitEx(
                device_backend,
                _label="StatesDeviceMitex",
                mitres=gen_compiled_MitRes(device_backend, _optimisation_level),
            ),
        )
    )
    _experiment_mitex = copy.copy(
        kwargs.get(
            "experiment_mitex",
            MitEx(
                device_backend,
                _label="ExperimentMitex",
                mitres=gen_compiled_MitRes(device_backend, _optimisation_level),
            ),
        )
    )

    _states_sim_taskgraph = TaskGraph().from_TaskGraph(_states_sim_mitex)
    _states_sim_taskgraph.parallel(_states_device_mitex)
    _states_sim_taskgraph.append(ccl_result_batching_task_gen(total_state_circuits))

    likelihood_function = kwargs.get("likelihood_function", LikelihoodFunction.none)

    _experiment_taskgraph = TaskGraph().from_TaskGraph(_experiment_mitex)
    _experiment_taskgraph.parallel(_states_sim_taskgraph)

    _post_calibrate_task_graph = TaskGraph(_label="FitCalibrate")
    _post_calibrate_task_graph.append(
        ccl_likelihood_filtering_task_gen(likelihood_function)
    )
    _post_calibrate_task_graph.append(
        cdr_calibration_task_gen(
            device_backend,
            kwargs.get("model", _PolyCDRCorrect(1)),
        )
    )

    _post_task_graph = TaskGraph(_label="QualityCheckCorrect")
    _post_task_graph.parallel(_post_calibrate_task_graph)
    _post_task_graph.prepend(
        cdr_quality_check_task_gen(
            distance_tolerance=kwargs.get("distance_tolerance", 0.1),
            calibration_fraction=kwargs.get("calibration_fraction", 0.5),
        )
    )

    _experiment_taskgraph.prepend(
        ccl_state_task_gen(
            n_non_cliffords,
            n_pairs,
            total_state_circuits,
            simulator_backend=simulator_backend,
            tolerance=kwargs.get("tolerance", 0.01),
            max_state_circuits_attempts=kwargs.get("max_state_circuits_attempts", 10),
        )
    )
    _experiment_taskgraph.append(_post_task_graph)
    _experiment_taskgraph.append(cdr_correction_task_gen(device_backend))

    return MitEx(device_backend).from_TaskGraph(_experiment_taskgraph)
