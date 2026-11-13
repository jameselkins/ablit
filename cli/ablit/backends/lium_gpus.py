"""Compute capability of the GPU models that Lium lists.

The public nodes feed gives a short GPU name such as "RTX 4090" in `gpu_model`
and no compute capability, so this table supplies it. Each value is the
capability that NVIDIA publishes for the product or its chip family:

- https://developer.nvidia.com/cuda/gpus (current GPUs)
- https://developer.nvidia.com/cuda/gpus/legacy (older GPUs)

Each key is a name exactly as the feed spells it. The original names were read
on 2026-10-23. Additional names were read from the feed on 2026-11-07,
2026-11-09 and 2026-11-12. Their capabilities come from NVIDIA's current list
or the architecture sources below. The current table lists RTX 5070 and
RTX 3070 Ti directly. NVIDIA lists A100, B300, H100 and H200 by
family, which covers the feed's A100-SXM4-80GB, B300 SXM6 AC, B300 SXM6 PC,
H100 PCIe and H200 NVL variants. The GTX 1660 Ti and RTX 4070
Ti SUPER values follow their Turing and Ada architectures in NVIDIA's product
comparison and CUDA compatibility guides:

- https://www.nvidia.com/en-us/geforce/graphics-cards/compare/
- https://docs.nvidia.com/cuda/turing-compatibility-guide/
- https://docs.nvidia.com/cuda/ada-compatibility-guide/

The RTX A4500 value follows NVIDIA's product page, which identifies its Ampere
architecture, and NVIDIA's PTX guide, which gives 8.6 for Ampere RTX GPUs:

- https://www.nvidia.com/en-us/products/workstations/rtx-a4500/
- https://developer.nvidia.com/blog/understanding-ptx-the-assembly-language-of-cuda-gpu-computing/

The A100 80GB PCIe, RTX 3070 and RTX 4080 SUPER names were read from the
feed on 2026-11-13. NVIDIA's current table gives 8.0 for the A100 family and
8.6 for RTX 3070. NVIDIA's A100 page names the 80GB PCIe variant. The RTX
4080 SUPER value follows its Ada architecture and the Ada compatibility guide,
which gives compute capability 8.9:

- https://www.nvidia.com/en-us/data-center/a100/
- https://www.nvidia.com/en-us/geforce/graphics-cards/40-series/rtx-4080-family/
- https://docs.nvidia.com/cuda/ada-compatibility-guide/

A name that is not in the table keeps `compute_capability = None`,
and the filter chain then removes it with the reason "compute capability
unknown" when a profile sets a minimum.
"""

from __future__ import annotations

_BLACKWELL = (
    "RTX 5060 Ti", "RTX 5070", "RTX 5070 Ti", "RTX 5080", "RTX 5090",
    "RTX PRO 4000 Blackwell", "RTX PRO 5000 Blackwell", "RTX PRO 6000 Blackwell Server Edition",
    "RTX PRO 6000 Blackwell Max-Q Workstation Edition", "RTX PRO 6000 Blackwell Workstation Edition",
)
_HOPPER = ("H100 80GB HBM3", "H100 PCIe", "H200", "H200 NVL")
_ADA = (
    "L40", "L40S", "RTX 4060 Ti", "RTX 4070 Ti SUPER", "RTX 4080", "RTX 4080 SUPER",
    "RTX 4090", "RTX 6000 Ada Generation",
)
_AMPERE_GA10X = ("RTX 3070", "RTX 3070 Ti", "RTX 3090", "RTX 3090 Ti", "RTX A4000", "RTX A4500", "RTX A6000")

COMPUTE_CAPABILITY_BY_LIUM_NAME: dict[str, float] = {
    "B200": 10.0,
    "B300 SXM6 AC": 10.3,
    "B300 SXM6 PC": 10.3,
    "A100-SXM4-80GB": 8.0,
    "A100 80GB PCIe": 8.0,
    "GTX 1660 Ti": 7.5,
    "T4 Tensor Core GPU": 7.5,
    **dict.fromkeys(_BLACKWELL, 12.0),
    **dict.fromkeys(_HOPPER, 9.0),
    **dict.fromkeys(_ADA, 8.9),
    **dict.fromkeys(_AMPERE_GA10X, 8.6),
}


def compute_capability_for(gpu_name: str) -> float | None:
    """Return the compute capability of a GPU name as Lium spells it, or None when the table lacks it."""
    return COMPUTE_CAPABILITY_BY_LIUM_NAME.get(gpu_name)
