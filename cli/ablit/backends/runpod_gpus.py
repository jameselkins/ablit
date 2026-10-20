"""Compute capability of the GPU types that RunPod lists.

RunPod reports no compute capability, so this table supplies it. It is built
from NVIDIA's own lists, which were read on 2026-10-20:

- https://developer.nvidia.com/cuda-gpus (current GPUs)
- https://developer.nvidia.com/cuda-legacy-gpus (older GPUs, used only for the V100)

Each key is a RunPod `gpuTypes.id` seen in live searches. A type is in the
table only when NVIDIA's list names that product, or names the product family
that the id belongs to (for example every H100 variant is listed as "NVIDIA
H100"). A type that is not in the table keeps `compute_capability = None`, and
the filter chain then removes it with the reason "compute capability unknown"
when a profile sets a minimum.

Types left out on purpose, because NVIDIA's lists do not name them:

- the MIG slices of the B300 and the RTX PRO 6000 Blackwell Server Edition
- GeForce RTX 4080 SUPER (the lists name the RTX 4080 but no SUPER variants)
- RTX A4500
- RTX PRO 4500 Blackwell (the lists name its Server and Workstation editions only)
- the AMD Instinct types, which have no CUDA compute capability
"""

from __future__ import annotations

NVIDIA_LIST_URLS = (
    "https://developer.nvidia.com/cuda-gpus",
    "https://developer.nvidia.com/cuda-legacy-gpus",
)
CHECKED_ON = "2026-10-20"

COMPUTE_CAPABILITY_BY_RUNPOD_ID: dict[str, float] = {
    # 12.0, Blackwell
    "NVIDIA GeForce RTX 5080": 12.0,
    "NVIDIA GeForce RTX 5090": 12.0,
    "NVIDIA RTX PRO 4000 Blackwell": 12.0,
    "NVIDIA RTX PRO 4500 Blackwell Server Edition": 12.0,
    "NVIDIA RTX PRO 5000 Blackwell": 12.0,
    "NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition": 12.0,
    "NVIDIA RTX PRO 6000 Blackwell Server Edition": 12.0,
    "NVIDIA RTX PRO 6000 Blackwell Workstation Edition": 12.0,
    # 10.3 and 10.0, data center Blackwell
    "NVIDIA B300 SXM6 AC": 10.3,
    "NVIDIA B200": 10.0,
    # 9.0, Hopper
    "NVIDIA H100 80GB HBM3": 9.0,
    "NVIDIA H100 NVL": 9.0,
    "NVIDIA H100 PCIe": 9.0,
    "NVIDIA H200": 9.0,
    "NVIDIA H200 NVL": 9.0,
    # 8.9, Ada Lovelace
    "NVIDIA GeForce RTX 4070 Ti": 8.9,
    "NVIDIA GeForce RTX 4080": 8.9,
    "NVIDIA GeForce RTX 4090": 8.9,
    "NVIDIA L4": 8.9,
    "NVIDIA L40": 8.9,
    "NVIDIA L40S": 8.9,
    "NVIDIA RTX 2000 Ada Generation": 8.9,
    "NVIDIA RTX 4000 Ada Generation": 8.9,
    "NVIDIA RTX 4000 SFF Ada Generation": 8.9,
    "NVIDIA RTX 5000 Ada Generation": 8.9,
    "NVIDIA RTX 6000 Ada Generation": 8.9,
    # 8.6, Ampere
    "NVIDIA A40": 8.6,
    "NVIDIA GeForce RTX 3070": 8.6,
    "NVIDIA GeForce RTX 3080": 8.6,
    "NVIDIA GeForce RTX 3080 Ti": 8.6,
    "NVIDIA GeForce RTX 3090": 8.6,
    "NVIDIA GeForce RTX 3090 Ti": 8.6,
    "NVIDIA RTX A2000": 8.6,
    "NVIDIA RTX A4000": 8.6,
    "NVIDIA RTX A5000": 8.6,
    "NVIDIA RTX A6000": 8.6,
    # 8.0, Ampere data center
    "NVIDIA A100 80GB PCIe": 8.0,
    "NVIDIA A100-SXM4-40GB": 8.0,
    "NVIDIA A100-SXM4-80GB": 8.0,
    # 7.0, Volta (from the legacy list)
    "Tesla V100-PCIE-16GB": 7.0,
    "Tesla V100-SXM2-16GB": 7.0,
}


def compute_capability_for(gpu_type_id: str) -> float | None:
    """Return the compute capability of a RunPod GPU type, or None when the table does not list it."""
    return COMPUTE_CAPABILITY_BY_RUNPOD_ID.get(gpu_type_id)
