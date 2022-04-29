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
)
from copy import copy

# Would it be crazy if we just had Mitres objects as requited inputs,
# rather than taking Backends as inputs and then turning them into MitRes
# objects. This might make the compositional nature of Qermit more apparent.
def gen_VPS_MitRes(backend: Backend, **kwargs) -> MitRes:

    _experiment_mitres = copy(
        kwargs.get(
            "experiment_mitres",
            MitRes(backend),
        )
    )

    _experiment_taskgraph = TaskGraph().from_TaskGraph(_experiment_mitres)

    # This feels a little weird. We are passing a backend and then
    # immediately ignoring it? Can we initialise a 
    return MitRes(backend).from_TaskGraph(_experiment_taskgraph)