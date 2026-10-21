"""The text form of a plan report."""

from __future__ import annotations

from ablit.notices import UNTESTED_NOTICE
from ablit.plan import (
    NEXT_OFFERS,
    STATUS_FAILED,
    STATUS_LEFT_OUT,
    STATUS_OK,
    STATUS_TIMED_OUT,
    PlanReport,
    cap_notice,
    cap_info,
    totals_for,
)
from ablit.select import RankedOffer, StepReport
from ablit.select.filters import floor_for


def _usd(value: float | None, digits: int = 2) -> str:
    return "unknown" if value is None else f"${value:,.{digits}f}"


def _hours(value: float, digits: int = 1) -> str:
    return f"{value:,.{digits}f} hours"


def _count(value: float, noun: str) -> str:
    return f"{value:g} {noun}" if value == 1 else f"{value:g} {noun}s"


def _offer_block(ranked: RankedOffer, report: PlanReport, title: str) -> list[str]:
    o = ranked.offer
    lines = [title]
    lines.append(f"  Offer:       {o.backend} {o.offer_id}")
    lines.append(f"  GPU:         {o.gpu_model} x{o.gpu_count}, {o.gpu_vram_gb_each:,.1f} GB VRAM each")
    lines.append(f"  Host RAM:    {o.host_ram_gb:,.1f} GB")
    allocated = "unknown" if o.allocated_disk_gb is None else f"{o.allocated_disk_gb:,.0f} GB"
    lines.append(f"  Disk:        {allocated} allocated and priced in, host has {o.disk_gb:,.1f} GB free")
    if o.download_mbps is None:
        lines.append(f"  Download:    speed not advertised, measured after boot (ranked as if it ran at {_floor(report):,.0f} Mbps)")
    else:
        lines.append(f"  Download:    {o.download_mbps:,.1f} Mbps advertised")
    details = ["verified" if o.verified else "not verified"]
    if o.reliability is not None:
        details.append(f"reliability {o.reliability:.3f}")
    if o.region:
        details.append(f"region {o.region}")
    if o.remaining_hours is not None:
        details.append(f"rental time left {_hours(o.remaining_hours)}")
    lines.append(f"  Host:        {', '.join(details)}")
    sticker = _usd(o.sticker_hourly, 4)
    lines.append(f"  Price:       {_usd(o.hourly_all_in, 4)} per hour all-in (GPU only: {sticker})")
    lines.append(f"  Transfer:    {_usd(o.bandwidth_cost_per_gb, 4)} per GB downloaded")
    lines.append(f"  One-time:    {_usd(o.fixed_fees)}")
    lines.append(
        f"  Time before use: download {_hours(ranked.download_hours, 2)}, startup {_hours(report.req.startup_hours, 2)} (an estimate)"
    )
    totals = totals_for(ranked, report)
    lines.append("  Totals, including download charges and the hours spent downloading and starting:")
    for row in totals:
        lines.append(f"    {row['label']:<14} {_usd(row['total'])}")
    cap = cap_info(ranked, report)
    if cap is not None:
        lines.append(f"  Cap:         {_usd(cap['cap_usd'])}")
        if cap["hours_until_cap"] is None:
            lines.append("               The cap is no higher than the one-time charges, so it is reached before use.")
        else:
            lines.append(
                f"               Spend reaches the cap about {_hours(cap['hours_until_cap'])} after the rental starts."
            )
        if not cap["covers_setup_and_ten_minutes"]:
            lines.append("               The cap does not cover the download, startup and 10 minutes of use.")
    return lines


def _floor(report: PlanReport) -> float:
    return floor_for(report.req, report.opts)


def _step_lines(steps: list[StepReport]) -> list[str]:
    lines = []
    for step in steps:
        kept = step.before - step.removed
        lines.append(f"  {step.name}: {step.before} in, {step.removed} removed, {kept} kept")
        for reason, count in sorted(step.reasons.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"      {count} removed because: {reason}")
    return lines


def render(report: PlanReport) -> str:
    entry = report.entry
    req = report.req
    opts = report.opts
    lines: list[str] = []
    lines.append(f"Plan for {entry.id} (status: {entry.status})")
    lines.append(f"  {entry.display_name}")
    lines.append(f"  Model:  {entry.source.repo} at revision {entry.source.revision}")
    digest = report.image_digest or "not pinned yet"
    lines.append(f"  Engine: {entry.profile.engine} (lock {entry.profile.engine_lock}), image digest {digest}")
    if entry.status == "unverified":
        lines.append(f"  {UNTESTED_NOTICE}")
    lines.append("")
    needs = [
        f"{_count(req.gpu_count, req.gpu_vendor + ' GPU')} with at least {req.vram_gb_each:g} GB VRAM each"
        + (f" and {req.vram_gb_total:g} GB in total" if req.vram_gb_total else ""),
    ]
    if req.compute_capability_min is not None:
        needs.append(f"compute capability {req.compute_capability_min:g} or higher")
    needs.append(f"at least {req.host_ram_gb:g} GB host RAM")
    needs.append(
        f"at least {req.disk_gb:,.1f} GB free disk (the staging peak plus the engine image allowance)"
    )
    needs.append(f"download of {req.download_gb:,.1f} GB at a speed floor of {_floor(report):g} Mbps")
    lines.append("What the machine needs")
    lines += [f"  {item}" for item in needs]
    lines.append(f"  Offers are ranked by total cost for a session of {_count(opts.hours, 'hour')}.")
    lines.append("")

    if report.ranked:
        if report.label:
            lines.append(f"Result: {report.label}.")
            lines.append("  At least one search stopped before it could prove that no cheaper offer exists.")
            lines.append("")
        lines += _offer_block(report.ranked[0], report, "Chosen offer")
        others = report.ranked[1 : 1 + NEXT_OFFERS]
        for index, ranked in enumerate(others, start=2):
            lines.append("")
            lines += _offer_block(ranked, report, f"Offer {index}")
        remaining = len(report.ranked) - 1 - len(others)
        lines.append("")
        lines.append(f"{len(report.ranked)} offers qualified" + (f", {remaining} more not shown." if remaining > 0 else "."))
    else:
        lines.append("No offer qualified.")
        if report.label:
            lines.append(f"  {report.label[0].upper()}{report.label[1:]}, so a cheaper or different offer may exist beyond them.")

    lines.append("")
    lines.append(f"Filter steps, over the {report.checked} offers checked")
    lines += _step_lines(report.steps) if report.checked else ["  No offers were returned, so no filter step ran."]

    lines.append("")
    lines.append("Backends")
    for b in report.backends:
        if b.status == STATUS_OK:
            state = "complete" if b.complete else "incomplete"
            text = f"  {b.name}: {b.checked} offers checked, {b.qualified} qualified, search {state}."
            if b.note:
                text += f" {b.note[0].upper()}{b.note[1:]}" + ("" if b.note.endswith(".") else ".")
            lines.append(text)
        elif b.status in (STATUS_FAILED, STATUS_TIMED_OUT):
            word = "timed out" if b.status == STATUS_TIMED_OUT else "failed"
            lines.append(f"  {b.name}: {word}. {b.reason}. The comparison did not include {b.name}.")
        elif b.status == STATUS_LEFT_OUT:
            lines.append(f"  {b.name}: left out because it was {b.reason}.")

    lines.append("")
    lines.append(cap_notice(report).rstrip())
    lines.append("")
    lines.append("ablit plan rents nothing.")
    return "\n".join(lines) + "\n"
