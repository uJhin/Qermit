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
from pytket.backends import Backend
from qermit import (
    MitRes,
    TaskGraph,
    MitTask,
)
from copy import copy
from typing import List, Tuple
from pytket.backends.backendresult import BackendResult
from qermit.taskgraph import backend_compile_circuit_shots_task_gen
from qermit import CircuitShots
from pytket.passes import auto_rebase_pass
from pytket import OpType

APS_rebase = auto_rebase_pass({OpType.CZ, OpType.TK1})

def gen_rebase_task() -> MitTask:

    def task(obj, exp_wire:List[CircuitShots]) -> Tuple[list[CircuitShots]]:

        print("=== gen_rebase_task ===")

        print("exp_wire:", *exp_wire, sep='\n')

        rebased_exp_wire = []

        for circ_shot in exp_wire:

            rebased_circ = circ_shot.Circuit
            shots = circ_shot.Shots

            APS_rebase.apply(rebased_circ)
            rebased_exp_wire.append(CircuitShots(Circuit=rebased_circ, Shots=shots))

        return (exp_wire,)

    return MitTask(
        _label="RebaseCircuit",
        _n_out_wires=1,
        _n_in_wires=1,
        _method=task,
    )

def gen_trap_circ_task(num_traps:int) -> MitTask:

    def task(obj, exp_wire:list[CircuitShots]) -> Tuple[List[CircuitShots], List]:

        print("=== gen_trap_circ_task ===")

        print("num_traps", num_traps)
        print("exp_wire:", *exp_wire, sep='\n')
        
        return (exp_wire, exp_wire, )

    return MitTask(
        _label="GenerateClifford",
        _n_out_wires=2,
        _n_in_wires=1,
        _method=task,
    )

def gen_accreditation_task() -> MitTask:

    def task(obj, results_wire:List[BackendResult], exp_type_labels:list) -> Tuple[List[BackendResult]]:

        print("=== gen_accreditation_task ===")

        print("results_wire:", *[result.get_counts() for result in results_wire], sep='\n')
        print("exp_type_labels:", *exp_type_labels, sep='\n')

        return (results_wire,)

    return MitTask(
        _label="Accreditation",
        _n_out_wires=1,
        _n_in_wires=2,
        _method=task,
    )

# Would it be crazy if we just had Mitres objects as requited inputs,
# rather than taking Backends as inputs and then turning them into MitRes
# objects. This might make the compositional nature of Qermit more apparent.
def gen_APS_MitRes(backend: Backend, num_traps: int, **kwargs) -> MitRes:
    """Accreditation and post selection based error mitigation, based on
    the work of https://arxiv.org/abs/2109.14329#

    :param backend: [description]
    :type backend: Backend
    :return: [description]
    :rtype: MitRes
    """

    default_mitres = MitRes(backend)
    default_mitres.prepend(backend_compile_circuit_shots_task_gen(backend))

    _experiment_mitres = copy(
        kwargs.get(
            "experiment_mitres",
            default_mitres,
        )
    )

    _experiment_taskgraph = TaskGraph().from_TaskGraph(_experiment_mitres)

    _experiment_taskgraph.add_wire()
    _experiment_taskgraph.append(gen_accreditation_task())
    _experiment_taskgraph.prepend(gen_trap_circ_task(num_traps))

    _experiment_taskgraph.prepend(gen_rebase_task())

    # This feels a little weird. We are passing a backend and then
    # immediately ignoring it? Can we initialise a 
    return MitRes(backend).from_TaskGraph(_experiment_taskgraph)