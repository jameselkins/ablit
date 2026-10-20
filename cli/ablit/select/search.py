"""The search rule for marketplaces that return a limited number of rows.

Vast returns at most 64 rows per query, so the first 64 rows by hourly price can
miss the lowest total. The rule is:

1. Query with all filters on the server, ordered by hourly price, ascending.
2. Compute the total for each row and keep the lowest.
3. No unseen offer can cost less than `last_price * (H + S)`, where
   `last_price` is the hourly price of the last row.
4. If the lowest total is at or below that bound, stop.
5. Otherwise query again for rows priced above `last_price`. If the marketplace
   rejects that filter, or after 10 pages, stop and label the result
   "cheapest of the N offers checked".

The function takes the page fetcher as an argument, so it makes no network
calls. One edge case follows from the "above" filter in step 5. When more rows
share the last price than fit in one page, the rows beyond the page are never
seen. The search cannot detect this, so it only reports a page that is entirely
one price as incomplete.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ablit.select.rank import PRICE_RESOLUTION_DIGITS
from ablit.select.types import Offer

PAGE_SIZE = 64
MAX_PAGES = 10


class FilterRejected(Exception):
    """The marketplace refused the "price above" filter on a follow-up query."""


FetchPage = Callable[[float | None], list[Offer]]
"""Takes the exclusive lower price bound (None for the first page) and returns
rows ordered by hourly price, ascending, with the server-side filters applied."""

Qualify = Callable[[list[Offer]], list[Offer]]
"""Applies the client-side filter chain to one page and returns the survivors."""

TotalFn = Callable[[Offer], float]


@dataclass(frozen=True)
class SearchOutcome:
    best: Offer | None
    best_total: float | None
    checked: int
    pages: int
    complete: bool
    qualifying: list[Offer] = field(default_factory=list)

    @property
    def label(self) -> str | None:
        """Return the wording for an incomplete result, or None when complete."""
        if self.complete:
            return None
        return f"cheapest of the {self.checked} offers checked"


def lower_bound(last_price: float, hours: float, startup_hours: float) -> float:
    """No offer priced at or above `last_price` can total less than this."""
    return last_price * (hours + startup_hours)


def search(
    fetch_page: FetchPage,
    qualify: Qualify,
    total_of: TotalFn,
    hours: float,
    startup_hours: float,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES,
) -> SearchOutcome:
    qualifying: list[Offer] = []
    best: Offer | None = None
    best_total: float | None = None
    checked = 0
    after: float | None = None
    pages = 0

    def better(offer: Offer, total: float) -> bool:
        if best is None or best_total is None:
            return True
        key = (round(total, PRICE_RESOLUTION_DIGITS), offer.backend, offer.offer_id)
        return key < (round(best_total, PRICE_RESOLUTION_DIGITS), best.backend, best.offer_id)

    while True:
        try:
            page = fetch_page(after)
        except FilterRejected:
            return SearchOutcome(best, best_total, checked, pages, False, qualifying)
        pages += 1
        checked += len(page)
        for offer in qualify(page):
            qualifying.append(offer)
            total = total_of(offer)
            if better(offer, total):
                best, best_total = offer, total

        if len(page) < page_size:
            return SearchOutcome(best, best_total, checked, pages, True, qualifying)

        last_price = page[-1].hourly_all_in
        if best_total is not None and best_total <= lower_bound(last_price, hours, startup_hours):
            return SearchOutcome(best, best_total, checked, pages, True, qualifying)
        if page[0].hourly_all_in == last_price or pages >= max_pages:
            return SearchOutcome(best, best_total, checked, pages, False, qualifying)
        after = last_price
