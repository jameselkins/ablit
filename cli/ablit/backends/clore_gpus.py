"""Compute capability of the GPU models that Clore lists.

Clore reports a GPU name such as "NVIDIA GeForce RTX 4090" in `specs.gpu` and no
compute capability, so this table supplies it. Each value is the capability that
NVIDIA publishes for the chip family behind the product, as listed on:

- https://developer.nvidia.com/cuda-gpus (current GPUs)
- https://developer.nvidia.com/cuda-legacy-gpus (older GPUs)

The entries for GPU types that `runpod_gpus.py` also names were read from the
list on 2026-10-20. That page is built by script and could not be fetched as
plain text on 2026-10-21, so the other entries were not re-read from it and need
a check before a release.

Each key is a name exactly as Clore spells it. Clore cuts some long names short
("NVIDIA RTX PRO 6000 Blackwell Workstation") and removes spaces from others
("NVIDIA A100SXM440GB"), so the keys follow what the marketplace returns. A name
that is not in the table keeps `compute_capability = None`, and the filter chain
then removes it with the reason "compute capability unknown" when a profile sets a
minimum. The CMP mining cards and "NVIDIA Graphics Device" are left out on
purpose, because NVIDIA's list does not name them.
"""

from __future__ import annotations

NVIDIA_LIST_URLS = (
    "https://developer.nvidia.com/cuda-gpus",
    "https://developer.nvidia.com/cuda-legacy-gpus",
)

_BLACKWELL_DESKTOP = (
    "NVIDIA GeForce RTX 5050", "NVIDIA GeForce RTX 5060", "NVIDIA GeForce RTX 5060 Ti",
    "NVIDIA GeForce RTX 5070", "NVIDIA GeForce RTX 5070 Ti", "NVIDIA GeForce RTX 5080",
    "NVIDIA GeForce RTX 5090", "NVIDIA GeForce RTX 5090 D", "NVIDIA GeForce RTX 5090 D v2",
    "NVIDIA GeForce RTX 5070 Laptop GPU", "NVIDIA GeForce RTX 5090 Laptop GPU",
    "NVIDIA RTX PRO 2000 Blackwell", "NVIDIA RTX PRO 4000 Blackwell", "NVIDIA RTX PRO 4500 Blackwell",
    "NVIDIA RTX PRO 5000 Blackwell", "NVIDIA RTX PRO 6000 Blackwell MaxQ Works",
    "NVIDIA RTX PRO 6000 Blackwell Server Edit", "NVIDIA RTX PRO 6000 Blackwell Workstation",
)
_HOPPER = ("NVIDIA H100", "NVIDIA H100 NVL")
_ADA = (
    "NVIDIA GeForce RTX 4060", "NVIDIA GeForce RTX 4060 Ti", "NVIDIA GeForce RTX 4070",
    "NVIDIA GeForce RTX 4070 SUPER", "NVIDIA GeForce RTX 4070 Ti", "NVIDIA GeForce RTX 4070 Ti SUPER",
    "NVIDIA GeForce RTX 4080", "NVIDIA GeForce RTX 4080 SUPER", "NVIDIA GeForce RTX 4090",
    "NVIDIA GeForce RTX 4090 D", "NVIDIA GeForce RTX 4070 Laptop GPU", "NVIDIA GeForce RTX 4090 Laptop GPU",
    "NVIDIA L4", "NVIDIA L40S", "NVIDIA RTX 4000 Ada Generation",
)
_AMPERE_GA10X = (
    "NVIDIA GeForce RTX 3050", "NVIDIA GeForce RTX 3060", "NVIDIA GeForce RTX 3060 Ti",
    "NVIDIA GeForce RTX 3070", "NVIDIA GeForce RTX 3070 Ti", "NVIDIA GeForce RTX 3080",
    "NVIDIA GeForce RTX 3080 Ti", "NVIDIA GeForce RTX 3090", "NVIDIA GeForce RTX 3090 Ti",
    "NVIDIA GeForce RTX 3060 Laptop GPU", "NVIDIA GeForce RTX 3070 Laptop GPU",
    "NVIDIA GeForce RTX 3080 Laptop GPU",
    "NVIDIA A2", "NVIDIA RTX A2000", "NVIDIA RTX A4000", "NVIDIA RTX A6000",
)
_AMPERE_A100 = ("NVIDIA A100 80GB PCIe", "NVIDIA A100SXM440GB")
_TURING = (
    "NVIDIA GeForce RTX 2060", "NVIDIA GeForce RTX 2060 SUPER", "NVIDIA GeForce RTX 2070",
    "NVIDIA GeForce RTX 2070 SUPER", "NVIDIA GeForce RTX 2080", "NVIDIA GeForce RTX 2080 SUPER",
    "NVIDIA GeForce RTX 2080 Ti", "NVIDIA GeForce GTX 1650", "NVIDIA GeForce GTX 1660 SUPER",
    "NVIDIA GeForce GTX 1660 Ti", "Tesla T4", "Quadro RTX 6000", "Quadro RTX 8000",
)
_VOLTA = ("Tesla V100SXM216GB", "Tesla V100SXM232GB")
_PASCAL = (
    "NVIDIA GeForce GT 1030", "NVIDIA GeForce GTX 1050", "NVIDIA GeForce GTX 1050 Ti",
    "NVIDIA GeForce GTX 1060 6GB", "NVIDIA GeForce GTX 1070", "NVIDIA GeForce GTX 1070 Ti",
    "NVIDIA GeForce GTX 1080", "NVIDIA GeForce GTX 1080 Ti", "NVIDIA P102100", "NVIDIA P104100",
    "Tesla P4", "Quadro P2200", "Quadro P4000",
)

COMPUTE_CAPABILITY_BY_CLORE_NAME: dict[str, float] = {
    **dict.fromkeys(_BLACKWELL_DESKTOP, 12.0),
    **dict.fromkeys(_HOPPER, 9.0),
    **dict.fromkeys(_ADA, 8.9),
    **dict.fromkeys(_AMPERE_GA10X, 8.6),
    **dict.fromkeys(_AMPERE_A100, 8.0),
    **dict.fromkeys(_TURING, 7.5),
    **dict.fromkeys(_VOLTA, 7.0),
    **dict.fromkeys(_PASCAL, 6.1),
    "Tesla M40 24GB": 5.2,
    "NVIDIA GeForce GTX 745": 5.0,
}


def compute_capability_for(gpu_name: str) -> float | None:
    """Return the compute capability of a GPU name as Clore spells it, or None when the table lacks it."""
    return COMPUTE_CAPABILITY_BY_CLORE_NAME.get(gpu_name)
