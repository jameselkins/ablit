"""Compute capability of the GPU models that Lium lists.

The public nodes feed gives a short GPU name such as "RTX 4090" in `gpu_model`
and no compute capability, so this table supplies it. Each value is the
capability that NVIDIA publishes for the chip family behind the product, the
same values `clore_gpus.py` uses:

- https://developer.nvidia.com/cuda-gpus (current GPUs)
- https://developer.nvidia.com/cuda-legacy-gpus (older GPUs)

Each key is a name exactly as the feed spells it, from the feed read on
2026-10-23, and it holds only the names seen there. A name that is not in the table keeps `compute_capability = None`,
and the filter chain then removes it with the reason "compute capability
unknown" when a profile sets a minimum. The Blackwell data center models (B200
and B300) are left out on purpose, because their values were not read from
NVIDIA's page.
"""

from __future__ import annotations

_BLACKWELL = ("RTX 5070 Ti", "RTX 5090", "RTX PRO 6000 Blackwell Server Edition")
_HOPPER = ("H100 80GB HBM3", "H200")
_ADA = ("RTX 4090", "RTX 6000 Ada Generation")
_AMPERE_GA10X = ("RTX 3090", "RTX A6000")

COMPUTE_CAPABILITY_BY_LIUM_NAME: dict[str, float] = {
    **dict.fromkeys(_BLACKWELL, 12.0),
    **dict.fromkeys(_HOPPER, 9.0),
    **dict.fromkeys(_ADA, 8.9),
    **dict.fromkeys(_AMPERE_GA10X, 8.6),
}


def compute_capability_for(gpu_name: str) -> float | None:
    """Return the compute capability of a GPU name as Lium spells it, or None when the table lacks it."""
    return COMPUTE_CAPABILITY_BY_LIUM_NAME.get(gpu_name)
