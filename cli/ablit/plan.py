"""`ablit plan`: search each backend, filter, rank and describe the result.

The ranked list is a function only of the offers, the catalog profile and the
user's flags. Nothing here reads referral data, and this module does not import
ablit.referral.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import yaml

from ablit.backends.base import Backend, creds_from_env
from ablit.backends.errors import BackendError, CredentialsRejected
from ablit.catalog.loader import repo_root
from ablit.catalog.models import Entry
from ablit.notices import SPEND_CAP_NOTICE, UNTESTED_NOTICE, spend_cap_notice
from ablit.select import (
    HardwareRequest,
    PlanOptions,
    RankedOffer,
    StepReport,
    apply_filter_chain,
    rank_offers,
)
from ablit.select.cost import cost_breakdown
from ablit.select.filters import floor_for
from ablit.select.types import Offer

JSON_SCHEMA_VERSION = 1

NEXT_OFFERS = 4

# The session lengths every quote shows, besides the --hours value.
FIXED_TOTALS = (("1 hour", 1.0), ("24 hours", 24.0), ("7 days", 168.0))

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_TIMED_OUT = "timed_out"
STATUS_LEFT_OUT = "left_out"


@dataclass(frozen=True)
class BackendReport:
    """What happened to one backend during the search."""

    name: str
    status: str
    reason: str | None = None
    checked: int = 0
    complete: bool | None = None
    note: str | None = None
    qualified: int = 0
    steps: list[StepReport] = field(default_factory=list)


@dataclass(frozen=True)
class PlanReport:
    entry: Entry
    req: HardwareRequest
    opts: PlanOptions
    max_spend: float | None
    ranked: list[RankedOffer]
    steps: list[StepReport]
    backends: list[BackendReport]
    checked: int
    complete: bool
    checked_at: str
    image_digest: str | None

    @property
    def label(self) -> str | None:
        """The wording for an incomplete search, or None when every searched backend was complete."""
        if self.complete:
            return None
        return f"cheapest of the {self.checked} offers checked"

    @property
    def failed(self) -> list[BackendReport]:
        return [b for b in self.backends if b.status in (STATUS_FAILED, STATUS_TIMED_OUT)]

    @property
    def left_out(self) -> list[BackendReport]:
        return [b for b in self.backends if b.status == STATUS_LEFT_OUT]


class UnknownBackend(ValueError):
    """The user named a backend that ablit does not have."""


def image_digest(entry: Entry) -> str | None:
    """Return the pinned image digest from the engine lock file, or None when none is pinned."""
    root = repo_root()
    if root is None:
        return None
    path = root / "engines" / entry.profile.engine / "lock.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    digest = data.get("digest") if isinstance(data, dict) else None
    return digest if isinstance(digest, str) and digest else None


def select_backends(available: Mapping[str, Backend], wanted: str | None) -> tuple[list[str], dict[str, str]]:
    """Return the backend names to search, and the reason for each one left out."""
    if wanted is None:
        return sorted(available), {}
    names = [n.strip() for n in wanted.split(",") if n.strip()]
    unknown = [n for n in names if n not in available]
    if unknown or not names:
        raise UnknownBackend(
            f"unknown backend {', '.join(unknown) or '(none named)'}. Known backends: {', '.join(sorted(available))}"
        )
    chosen = sorted(set(names))
    left_out = {n: "not named in --backends" for n in sorted(available) if n not in chosen}
    return chosen, left_out


def run_plan(
    entry: Entry,
    req: HardwareRequest,
    opts: PlanOptions,
    backends: Mapping[str, Backend],
    *,
    wanted: str | None = None,
    max_spend: float | None = None,
    environ: Mapping[str, str] | None = None,
    strict_credentials: bool = False,
) -> PlanReport:
    """Search each wanted backend and rank what they return.

    A backend that fails or times out is reported by name, and the others
    carry on. The comparison then leaves it out, and the report says so.
    """
    environ = os.environ if environ is None else environ
    chosen, left_out = select_backends(backends, wanted)
    reports: list[BackendReport] = []
    all_offers: list[Offer] = []
    per_backend: dict[str, list[Offer]] = {}
    complete = True

    for name in chosen:
        backend = backends[name]
        creds = creds_from_env(name, environ)
        try:
            found = backend.search(req, creds, opts)
        except httpx.TimeoutException as error:
            reports.append(BackendReport(name, STATUS_TIMED_OUT, f"timed out ({type(error).__name__})"))
            continue
        except httpx.HTTPError as error:
            reports.append(BackendReport(name, STATUS_FAILED, f"network error: {error}"))
            continue
        except CredentialsRejected as error:
            if strict_credentials:
                raise
            reports.append(BackendReport(name, STATUS_FAILED, str(error)))
            continue
        except BackendError as error:
            reports.append(BackendReport(name, STATUS_FAILED, str(error)))
            continue
        per_backend[name] = list(found.offers)
        all_offers.extend(found.offers)
        complete = complete and found.complete
        filtered = apply_filter_chain(found.offers, req, opts)
        reports.append(
            BackendReport(
                name, STATUS_OK, None, len(found.offers), found.complete, found.note,
                len(filtered.kept), filtered.steps,
            )
        )
    for name, reason in left_out.items():
        reports.append(BackendReport(name, STATUS_LEFT_OUT, reason))
    reports.sort(key=lambda r: r.name)

    result = rank_offers(all_offers, req, opts)
    return PlanReport(
        entry=entry,
        req=req,
        opts=opts,
        max_spend=max_spend,
        ranked=result.ranked,
        steps=result.steps,
        backends=reports,
        checked=len(all_offers),
        complete=complete,
        checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        image_digest=image_digest(entry),
    )


# --- totals ---------------------------------------------------------------


def totals_for(ranked: RankedOffer, report: PlanReport) -> list[dict[str, Any]]:
    """Totals for 1 hour, 24 hours, 7 days and --hours, each with download and startup time included."""
    floor = floor_for(report.req, report.opts)
    rows = [(label, hours) for label, hours in FIXED_TOTALS]
    rows.append((f"--hours ({report.opts.hours:g})", report.opts.hours))
    out = []
    for label, hours in rows:
        breakdown = cost_breakdown(ranked.offer, report.req, hours, floor)
        out.append(
            {
                "label": label,
                "hours": hours,
                "total": breakdown.total,
                "compute": breakdown.compute,
                "transfer": breakdown.transfer,
                "fixed": breakdown.fixed,
            }
        )
    return out


def cap_info(ranked: RankedOffer, report: PlanReport) -> dict[str, Any] | None:
    """Where the spend cap falls for this offer, or None when no cap was given."""
    cap = report.max_spend
    if cap is None:
        return None
    offer = ranked.offer
    floor = floor_for(report.req, report.opts)
    breakdown = cost_breakdown(offer, report.req, report.opts.hours, floor)
    one_time = breakdown.transfer + breakdown.fixed
    hours_to_cap: float | None = None
    if offer.hourly_all_in > 0 and cap > one_time:
        hours_to_cap = (cap - one_time) / offer.hourly_all_in
    return {
        "cap_usd": cap,
        "one_time_usd": one_time,
        "hours_until_cap": hours_to_cap,
        "covers_setup_and_ten_minutes": cap >= cost_breakdown(offer, report.req, 10 / 60, floor).total,
    }


# --- JSON -----------------------------------------------------------------


def _step_json(step: StepReport) -> dict[str, Any]:
    return {"name": step.name, "before": step.before, "removed": step.removed, "reasons": dict(step.reasons)}


def offer_json(ranked: RankedOffer, report: PlanReport, rank: int) -> dict[str, Any]:
    o = ranked.offer
    floor = floor_for(report.req, report.opts)
    breakdown = cost_breakdown(o, report.req, report.opts.hours, floor)
    return {
        "rank": rank,
        "backend": o.backend,
        "offer_id": o.offer_id,
        "gpu_model": o.gpu_model,
        "gpu_count": o.gpu_count,
        "gpu_vram_gb_each": o.gpu_vram_gb_each,
        "host_ram_gb": o.host_ram_gb,
        "disk_free_gb": o.disk_gb,
        "disk_allocated_gb": o.allocated_disk_gb,
        "download_mbps": o.download_mbps,
        "download_speed": ranked.speed_label,
        "ranked_at_mbps": breakdown.speed_mbps,
        "verified": o.verified,
        "reliability": o.reliability,
        "region": o.region,
        "remaining_hours": o.remaining_hours,
        "interruptible": o.interruptible,
        "hourly_all_in_usd": o.hourly_all_in,
        "gpu_only_hourly_usd": o.sticker_hourly,
        "transfer_usd_per_gb": o.bandwidth_cost_per_gb,
        "one_time_fees_usd": o.fixed_fees,
        "download_hours": breakdown.download_hours,
        "startup_hours": breakdown.startup_hours,
        "ranking_total_usd": ranked.total,
        "totals": totals_for(ranked, report),
        "cap": cap_info(ranked, report),
    }


def report_json(report: PlanReport) -> dict[str, Any]:
    entry = report.entry
    profile = entry.profile
    chosen = report.ranked[0] if report.ranked else None
    others = report.ranked[1 : 1 + NEXT_OFFERS]
    req = report.req
    opts = report.opts
    return {
        "schema": JSON_SCHEMA_VERSION,
        "command": "plan",
        "checked_at": report.checked_at,
        "result": "ok" if chosen else "no_offer",
        "complete": report.complete,
        "incomplete_label": report.label,
        "model": {
            "id": entry.id,
            "display_name": entry.display_name,
            "repo": entry.source.repo,
            "revision": entry.source.revision,
            "engine": profile.engine,
            "engine_lock": profile.engine_lock,
            "image_digest": report.image_digest,
            "status": entry.status,
        },
        "request": {
            "gpu_vendor": req.gpu_vendor,
            "gpu_count": req.gpu_count,
            "vram_gb_each": req.vram_gb_each,
            "vram_gb_total": req.vram_gb_total,
            "compute_capability_min": req.compute_capability_min,
            "host_ram_gb": req.host_ram_gb,
            "disk_gb": req.disk_gb,
            "download_gb": req.download_gb,
            "image_gb": req.image_gb,
            "startup_hours": req.startup_hours,
            "min_download_mbps": floor_for(req, opts),
        },
        "options": {
            "hours": opts.hours,
            "spot": opts.spot,
            "trust": opts.trust,
            "min_reliability": opts.min_reliability,
            "region": opts.region,
            "max_price_hr": opts.max_price_hr,
            "min_download_mbps": opts.min_download_mbps,
            "max_spend": report.max_spend,
        },
        "chosen": offer_json(chosen, report, 1) if chosen else None,
        "next": [offer_json(r, report, i) for i, r in enumerate(others, start=2)],
        "qualified_count": len(report.ranked),
        "checked_count": report.checked,
        "filter_steps": [_step_json(s) for s in report.steps],
        "backends": [
            {
                "name": b.name,
                "status": b.status,
                "reason": b.reason,
                "checked": b.checked,
                "complete": b.complete,
                "note": b.note,
                "qualified": b.qualified,
                "filter_steps": [_step_json(s) for s in b.steps],
            }
            for b in report.backends
        ],
        "notices": {
            "spend_cap": cap_notice(report).strip(),
            "untested": UNTESTED_NOTICE if entry.status == "unverified" else None,
        },
    }


def cap_notice(report: PlanReport) -> str:
    if report.max_spend is not None:
        return spend_cap_notice(report.max_spend)
    return SPEND_CAP_NOTICE.replace("$<cap>", "your --max-spend cap")
