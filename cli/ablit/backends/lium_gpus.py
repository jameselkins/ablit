"""Compute capability of the GPU models that Lium lists.

The public nodes feed gives a short GPU name such as "RTX 4090" in `gpu_model`
and no compute capability, so this table supplies it. Each value is the
capability that NVIDIA publishes for the product or its chip family:

- https://developer.nvidia.com/cuda-gpus (current GPUs)
- https://developer.nvidia.com/cuda-legacy-gpus (older GPUs)

Each key is a name exactly as the feed spells it. The original names were read
on 2026-10-23. Additional names were read from the feed on 2026-11-07 and checked
against NVIDIA's current list on the same date. NVIDIA lists B300 and H200 by
family, which covers the feed's B300 SXM6 AC and H200 NVL variants, as in the
RunPod table. A name that is not in the table keeps `compute_capability = None`,
and the filter chain then removes it with the reason "compute capability
unknown" when a profile sets a minimum.
"""

from __future__ import annotations

_BLACKWELL = (
    "RTX 5070 Ti", "RTX 5080", "RTX 5090", "RTX PRO 6000 Blackwell Server Edition",
    "RTX PRO 6000 Blackwell Max-Q Workstation Edition", "RTX PRO 6000 Blackwell Workstation Edition",
)
_HOPPER = ("H100 80GB HBM3", "H200", "H200 NVL")
_ADA = ("RTX 4090", "RTX 6000 Ada Generation")
_AMPERE_GA10X = ("RTX 3090", "RTX A4000", "RTX A6000")

COMPUTE_CAPABILITY_BY_LIUM_NAME: dict[str, float] = {
    "B200": 10.0,
    "B300 SXM6 AC": 10.3,
    **dict.fromkeys(_BLACKWELL, 12.0),
    **dict.fromkeys(_HOPPER, 9.0),
    **dict.fromkeys(_ADA, 8.9),
    **dict.fromkeys(_AMPERE_GA10X, 8.6),
}


def compute_capability_for(gpu_name: str) -> float | None:
    """Return the compute capability of a GPU name as Lium spells it, or None when the table lacks it."""
    return COMPUTE_CAPABILITY_BY_LIUM_NAME.get(gpu_name)
