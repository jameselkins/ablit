"""Compute peak disk use from an entry's `staging` list.

Peak disk is never written by hand. The numbers here use decimal bytes, the
same unit the Hugging Face API reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ablit.catalog.models import Artifact, Entry, StagingStep


class StagingError(ValueError):
    """The staging list cannot be evaluated, for example because a size is unknown."""


@dataclass(frozen=True)
class StagingReport:
    download_bytes: int
    peak_bytes: int
    final_bytes: int
    lines: list[str] = field(default_factory=list)

    @property
    def peak_gb(self) -> float:
        return self.peak_bytes / 1e9

    @property
    def download_gb(self) -> float:
        return self.download_bytes / 1e9


def _select(entry: Entry, selectors: list[str], where: str) -> list[int]:
    """Return artifact indexes that match each selector, which is a path or a role."""
    chosen: list[int] = []
    for selector in selectors:
        by_path = [i for i, a in enumerate(entry.artifacts) if a.path == selector]
        matches = by_path or [i for i, a in enumerate(entry.artifacts) if a.role == selector]
        if not matches:
            raise StagingError(f"{where}: {selector!r} matches no artifact path or role")
        for index in matches:
            if index not in chosen:
                chosen.append(index)
    return chosen


def _size(artifact: Artifact, where: str) -> int:
    if artifact.bytes is None:
        raise StagingError(f"{where}: the size of {artifact.path} is not known yet")
    return artifact.bytes


def compute_staging(entry: Entry) -> StagingReport:
    present: set[int] = set()
    on_disk = 0
    peak = 0
    downloaded = 0
    lines: list[str] = []

    for number, step in enumerate(entry.staging, start=1):
        where = f"staging step {number} ({step.step})"
        if step.step == "download":
            selectors = ["__all__"] if step.artifacts in (None, "all") else list(step.artifacts)  # type: ignore[arg-type]
            if selectors == ["__all__"]:
                indexes = list(range(len(entry.artifacts)))
            else:
                indexes = _select(entry, selectors, where)
            added = 0
            for index in indexes:
                if index in present:
                    continue
                added += _size(entry.artifacts[index], where)
                present.add(index)
            on_disk += added
            downloaded += added
            peak = max(peak, on_disk)
            lines.append(f"{where}: downloads {added / 1e9:.1f} GB, disk now {on_disk / 1e9:.1f} GB")
        else:
            step_inputs = _transform_inputs(entry, step, where)
            if step.output_bytes is None:
                raise StagingError(f"{where}: output_bytes is not known yet")
            input_bytes = sum(_size(entry.artifacts[i], where) for i in step_inputs if i in present)
            if step.keeps_inputs_until_done:
                during = on_disk + step.output_bytes
            else:
                during = on_disk + max(step.output_bytes - input_bytes, 0)
            peak = max(peak, during)
            on_disk += step.output_bytes
            if step.delete_inputs_after:
                on_disk -= input_bytes
                present.difference_update(step_inputs)
            lines.append(
                f"{where}: peak {during / 1e9:.1f} GB while running, disk after {on_disk / 1e9:.1f} GB"
            )
            peak = max(peak, on_disk)

    return StagingReport(download_bytes=downloaded, peak_bytes=peak, final_bytes=on_disk, lines=lines)


def _transform_inputs(entry: Entry, step: StagingStep, where: str) -> list[int]:
    if not step.inputs:
        raise StagingError(f"{where}: needs a non-empty inputs list")
    return _select(entry, list(step.inputs), where)


def check_claim(entry: Entry, report: StagingReport) -> list[str]:
    """Compare the computed peak with the entry's own claim, if it makes one."""
    claimed = entry.profile.storage.disk_gb_claimed
    if claimed is None:
        return []
    if report.peak_gb > claimed:
        return [
            f"computed peak disk {report.peak_gb:.1f} GB is above the claimed {claimed:.1f} GB "
            "(profile.storage.disk_gb_claimed)"
        ]
    return []
