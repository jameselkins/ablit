"""Plain data types for offer selection.

Offers carry no referral data. Nothing in this package imports ablit.referral,
and a contract in .importlinter enforces that.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Offer:
    """One rentable machine as a marketplace describes it.

    Prices are US dollars. `hourly_all_in` includes the disk the run needs,
    and `sticker_hourly` is the GPU-only price shown separately in the quote.
    `download_mbps` is None when the marketplace does not advertise a speed.
    `disk_gb` is the free disk the host offers, and `allocated_disk_gb` is the
    disk the price already includes, which is None when a driver does not know it.
    `gpu_fraction` is below 1 when the host shares each GPU with other renters.
    """

    backend: str
    offer_id: str
    gpu_model: str
    gpu_count: int
    gpu_vram_gb_each: float
    hourly_all_in: float
    host_ram_gb: float
    disk_gb: float
    gpu_vendor: str = "nvidia"
    compute_capability: float | None = None
    os: str = "linux"
    sticker_hourly: float | None = None
    bandwidth_cost_per_gb: float = 0.0
    fixed_fees: float = 0.0
    download_mbps: float | None = None
    rentable: bool = True
    interruptible: bool = False
    remaining_hours: float | None = None
    verified: bool = False
    reliability: float | None = None
    region: str | None = None
    allocated_disk_gb: float | None = None
    gpu_fraction: float = 1.0


@dataclass(frozen=True)
class HardwareRequest:
    """What a catalog profile needs, in the units the filters use.

    `disk_gb` is the free disk an offer must have, which is the staging peak
    plus the engine image. `download_gb` is in decimal GB.
    """

    gpu_vendor: str
    gpu_count: int
    vram_gb_each: float
    host_ram_gb: float
    disk_gb: float
    download_gb: float
    startup_hours: float
    min_download_mbps: float
    vram_gb_total: float | None = None
    compute_capability_min: float | None = None
    image_gb: float = 0.0
    os: str = "linux"


@dataclass(frozen=True)
class PlanOptions:
    """The user's flags that change which offers qualify and how they rank."""

    hours: float = 1.0
    spot: bool = False
    trust: str = "any"  # "any" or "verified"
    min_reliability: float | None = None
    region: str | None = None
    max_price_hr: float | None = None
    min_download_mbps: float | None = None  # overrides the profile floor when set


@dataclass(frozen=True)
class StepReport:
    """How many offers one filter step removed, and why."""

    name: str
    before: int
    removed: int
    reasons: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RankedOffer:
    offer: Offer
    total: float
    download_hours: float
    speed_label: str  # "advertised" or "not advertised, measured after boot"


@dataclass(frozen=True)
class FilterResult:
    kept: list[Offer]
    steps: list[StepReport]
