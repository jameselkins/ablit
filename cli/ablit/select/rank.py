"""Rank the offers that pass the filter chain.

The ranking is a function only of the offers, the catalog profile and the
user's options. Ties break on backend name, then offer ID, alphabetically.
"""

from __future__ import annotations

from dataclasses import dataclass

from ablit.select.cost import cost_breakdown
from ablit.select.filters import apply_filter_chain, floor_for
from ablit.select.types import HardwareRequest, Offer, PlanOptions, RankedOffer, StepReport

# Totals closer than this many dollars count as equal, so that float rounding
# cannot override the tie-break order.
PRICE_RESOLUTION_DIGITS = 9


@dataclass(frozen=True)
class PlanResult:
    ranked: list[RankedOffer]
    steps: list[StepReport]


def rank_offers(offers: list[Offer], req: HardwareRequest, opts: PlanOptions) -> PlanResult:
    filtered = apply_filter_chain(offers, req, opts)
    floor = floor_for(req, opts)
    ranked: list[RankedOffer] = []
    for offer in filtered.kept:
        breakdown = cost_breakdown(offer, req, opts.hours, floor)
        ranked.append(
            RankedOffer(
                offer=offer,
                total=breakdown.total,
                download_hours=breakdown.download_hours,
                speed_label=(
                    "advertised"
                    if offer.download_mbps is not None
                    else "not advertised, measured after boot"
                ),
            )
        )
    ranked.sort(key=lambda r: (round(r.total, PRICE_RESOLUTION_DIGITS), r.offer.backend, r.offer.offer_id))
    return PlanResult(ranked=ranked, steps=filtered.steps)
