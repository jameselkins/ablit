"""Build a hardware request from a catalog entry."""

from __future__ import annotations

from ablit.catalog.models import Entry
from ablit.catalog.staging import compute_staging
from ablit.select.types import HardwareRequest


def request_from_entry(entry: Entry, image_gb: float = 0.0) -> HardwareRequest:
    """Turn an entry's profile and staging plan into the numbers the filters use.

    Raises StagingError when the entry's sizes are not filled in yet.
    """
    report = compute_staging(entry)
    gpu = entry.profile.gpu
    return HardwareRequest(
        gpu_vendor=gpu.vendor,
        gpu_count=gpu.count,
        vram_gb_each=gpu.vram_gb_each,
        vram_gb_total=gpu.vram_gb_total,
        compute_capability_min=gpu.compute_capability_min,
        host_ram_gb=entry.profile.host_ram_gb,
        disk_gb=report.peak_gb + image_gb,
        download_gb=report.download_gb,
        image_gb=image_gb,
        startup_hours=entry.profile.startup_hours_estimate,
        min_download_mbps=entry.profile.min_download_mbps,
    )
