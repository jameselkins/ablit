"""Vast driver stub. See backends/vast.md at the top of the repository.

Search request, as the notes describe it: POST https://console.vast.ai/api/v0/bundles/
with `gpu_name` spelled with spaces, `gpu_ram` and `cpu_ram` in MB, `disk_space`
in GB, `compute_cap` multiplied by 100, `rentable: {eq: true}`, `type`,
`allocated_storage`, and `order: [["dph_total", "asc"]]`.

TODO(step 2): write search against live data.
TODO(step 3): write create, reconcile, destroy and the scoped watchdog key.
"""

from __future__ import annotations

from ablit.backends.base import UnwrittenBackend


class VastBackend(UnwrittenBackend):
    name = "vast"
