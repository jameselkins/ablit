"""The total-cost formula.

    speed_mbps = advertised download speed, or the floor when none is advertised
    D          = download_gb * 8000 / (speed_mbps * 3600)
    S          = startup_hours
    total(H)   = hourly_all_in * (D + S + H)
               + bandwidth_cost_per_gb * (download_gb + image_gb)
               + fixed_fees

`download_gb` is in decimal GB and speeds are in megabits per second.
"""

from __future__ import annotations

from dataclasses import dataclass

from ablit.select.types import HardwareRequest, Offer


@dataclass(frozen=True)
class CostBreakdown:
    speed_mbps: float
    download_hours: float
    startup_hours: float
    run_hours: float
    compute: float
    transfer: float
    fixed: float

    @property
    def total(self) -> float:
        return self.compute + self.transfer + self.fixed


def effective_speed(offer: Offer, floor_mbps: float) -> float:
    """Return the speed used for ranking. An offer with no advertised speed ranks at the floor."""
    return offer.download_mbps if offer.download_mbps is not None else floor_mbps


def download_hours(download_gb: float, speed_mbps: float) -> float:
    if speed_mbps <= 0:
        raise ValueError("download speed must be above zero")
    return download_gb * 8000.0 / (speed_mbps * 3600.0)


def cost_breakdown(
    offer: Offer, req: HardwareRequest, hours: float, floor_mbps: float | None = None
) -> CostBreakdown:
    floor = req.min_download_mbps if floor_mbps is None else floor_mbps
    speed = effective_speed(offer, floor)
    d = download_hours(req.download_gb, speed)
    s = req.startup_hours
    return CostBreakdown(
        speed_mbps=speed,
        download_hours=d,
        startup_hours=s,
        run_hours=hours,
        compute=offer.hourly_all_in * (d + s + hours),
        transfer=offer.bandwidth_cost_per_gb * (req.download_gb + req.image_gb),
        fixed=offer.fixed_fees,
    )


def total_cost(
    offer: Offer, req: HardwareRequest, hours: float, floor_mbps: float | None = None
) -> float:
    return cost_breakdown(offer, req, hours, floor_mbps).total
