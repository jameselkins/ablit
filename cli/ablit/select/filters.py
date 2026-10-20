"""The filter chain, in the order the specification gives.

Each step reports how many offers it removed and why. A step never looks at
anything except the offers, the request and the user's options.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

from ablit.select.types import FilterResult, HardwareRequest, Offer, PlanOptions, StepReport

Check = Callable[[Offer], str | None]
"""Returns None when the offer passes, or a short reason when it fails."""


def engine_floor(req: HardwareRequest) -> Check:
    """GPU vendor, count, VRAM per GPU and in total, compute capability, host RAM and OS."""

    def check(offer: Offer) -> str | None:
        if offer.gpu_vendor != req.gpu_vendor:
            return f"GPU vendor is not {req.gpu_vendor}"
        if offer.gpu_count != req.gpu_count:
            return f"needs exactly {req.gpu_count} GPUs"
        if offer.gpu_fraction != 1.0:
            return "GPU is shared with other renters"
        if offer.gpu_vram_gb_each < req.vram_gb_each:
            return "not enough VRAM per GPU"
        if req.vram_gb_total is not None and offer.gpu_vram_gb_each * offer.gpu_count < req.vram_gb_total:
            return "not enough VRAM in total"
        if req.compute_capability_min is not None:
            if offer.compute_capability is None:
                return "compute capability unknown"
            if offer.compute_capability < req.compute_capability_min:
                return "compute capability too low"
        if offer.host_ram_gb < req.host_ram_gb:
            return "not enough host RAM"
        if offer.os != req.os:
            return f"OS is not {req.os}"
        return None

    return check


def disk_floor(req: HardwareRequest) -> Check:
    """Free disk of at least the staging peak plus the engine image."""

    def check(offer: Offer) -> str | None:
        if offer.disk_gb < req.disk_gb:
            return "not enough disk"
        return None

    return check


def download_floor(floor_mbps: float) -> Check:
    """Advertised download speed must meet the floor. An unadvertised speed is kept."""

    def check(offer: Offer) -> str | None:
        if offer.download_mbps is not None and offer.download_mbps < floor_mbps:
            return "download speed below the floor"
        return None

    return check


def availability(req: HardwareRequest, opts: PlanOptions) -> Check:
    """On demand unless spot is allowed, rentable, and enough rental time left."""

    def check(offer: Offer) -> str | None:
        if offer.interruptible and not opts.spot:
            return "interruptible offer and --spot is off"
        if not offer.rentable:
            return "not rentable"
        if offer.remaining_hours is not None and offer.remaining_hours < req.startup_hours + opts.hours:
            return "rental ends too soon"
        return None

    return check


def user_filters(opts: PlanOptions) -> Check:
    """Filters the user set. A filter that is not set removes nothing."""

    def check(offer: Offer) -> str | None:
        if opts.trust == "verified" and not offer.verified:
            return "not a verified host"
        if opts.min_reliability is not None and (
            offer.reliability is None or offer.reliability < opts.min_reliability
        ):
            return "reliability below --min-reliability"
        if opts.region is not None and offer.region != opts.region:
            return "outside --region"
        if opts.max_price_hr is not None and offer.hourly_all_in > opts.max_price_hr:
            return "above --max-price-hr"
        return None

    return check


def floor_for(req: HardwareRequest, opts: PlanOptions) -> float:
    """The download floor in use: the user's flag when set, else the profile's."""
    return opts.min_download_mbps if opts.min_download_mbps is not None else req.min_download_mbps


def apply_filter_chain(offers: list[Offer], req: HardwareRequest, opts: PlanOptions) -> FilterResult:
    steps: list[tuple[str, Check]] = [
        ("engine floor", engine_floor(req)),
        ("disk floor", disk_floor(req)),
        ("download speed floor", download_floor(floor_for(req, opts))),
        ("rental type and availability", availability(req, opts)),
        ("user filters", user_filters(opts)),
    ]
    kept = list(offers)
    reports: list[StepReport] = []
    for name, check in steps:
        survivors: list[Offer] = []
        reasons: Counter[str] = Counter()
        for offer in kept:
            reason = check(offer)
            if reason is None:
                survivors.append(offer)
            else:
                reasons[reason] += 1
        reports.append(
            StepReport(name=name, before=len(kept), removed=len(kept) - len(survivors), reasons=dict(reasons))
        )
        kept = survivors
    return FilterResult(kept=kept, steps=reports)
