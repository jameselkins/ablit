"""RunPod driver stub. See backends/runpod.md at the top of the repository.

Search request, as the notes describe it: GraphQL `gpuTypes { lowestPrice(input:
{gpuCount, minMemoryInGb, ...}) }`, with `secureCloud` left out to see community
prices. `uninterruptablePrice` is the total for all GPUs. Renting uses
`podFindAndDeployOnDemand` with `terminateAfter`.

TODO(step 2): write search against live data.
TODO(step 4): write create, destroy and the speed check.
"""

from __future__ import annotations

from ablit.backends.base import UnwrittenBackend


class RunPodBackend(UnwrittenBackend):
    name = "runpod"
