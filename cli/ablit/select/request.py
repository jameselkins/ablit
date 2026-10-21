"""Build a hardware request from a catalog entry."""

from __future__ import annotations

from ablit.catalog.models import Entry
from ablit.catalog.staging import compute_staging
from ablit.select.types import HardwareRequest


def request_from_entry(entry: Entry, image_gb: float | None = None) -> HardwareRequest:
    """Turn an entry's profile and staging plan into the numbers the filters use.

    Raises StagingError when the entry's sizes are not filled in yet.
    """
    from ablit.catalog.loader import repo_root
    import yaml
    root = repo_root()
    path = root / "engines" / entry.profile.engine / "lock.yaml" if root else None
    lock = yaml.safe_load(path.read_text()) if path and path.exists() else {}
    if image_gb is None:
        image_gb = lock.get("image_gb", 0.0)
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
        cuda_version_min=lock.get("cuda_version_min"),
        startup_hours=entry.profile.startup_hours_estimate,
        min_download_mbps=entry.profile.min_download_mbps,
    )
