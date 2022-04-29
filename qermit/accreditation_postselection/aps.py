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

def gen_rebase_task() -> MitTask:

    def task(obj, exp_wire):

        return (exp_wire,)

    return MitTask(
        _label="RebaseCircuit",
        _n_out_wires=1,
        _n_in_wires=1,
        _method=task,
    )

def gen_clifford_circ_task() -> MitTask:

    def task(obj, exp_wire):
        
        return (exp_wire, exp_wire, )

    return MitTask(
        _label="GenerateClifford",
        _n_out_wires=2,
        _n_in_wires=1,
        _method=task,
    )

def gen_accreditation_task() -> MitTask:

    def task(obj, exp_wire, exp_type_labels) -> Tuple[List[BackendResult]]:

        return (exp_wire,)

    return MitTask(
        _label="Accreditation",
        _n_out_wires=1,
        _n_in_wires=2,
        _method=task,
    )

# Would it be crazy if we just had Mitres objects as requited inputs,
# rather than taking Backends as inputs and then turning them into MitRes
# objects. This might make the compositional nature of Qermit more apparent.
def gen_APS_MitRes(backend: Backend, **kwargs) -> MitRes:

    _experiment_mitres = copy(
        kwargs.get(
            "experiment_mitres",
            MitRes(backend),
        )
    )

    _experiment_taskgraph = TaskGraph().from_TaskGraph(_experiment_mitres)

    _experiment_taskgraph.add_wire()
    _experiment_taskgraph.append(gen_accreditation_task())
    _experiment_taskgraph.prepend(gen_clifford_circ_task())

    _experiment_taskgraph.prepend(gen_rebase_task())

    # This feels a little weird. We are passing a backend and then
    # immediately ignoring it? Can we initialise a 
    return MitRes(backend).from_TaskGraph(_experiment_taskgraph)