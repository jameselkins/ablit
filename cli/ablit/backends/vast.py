"""Vast driver. See backends/vast.md at the top of the repository.

Search request: POST https://console.vast.ai/api/v0/bundles/ with `gpu_ram` and
`cpu_ram` in MB, `disk_space` in GB, `compute_cap` multiplied by 100,
`rentable: {eq: true}`, `type`, `allocated_storage`, and an `order`.

Two facts about the live service shape the paging code below.

1. Vast returns at most 64 rows per query.
2. The server-side filter on `dph_total` compares the GPU-only price, which Vast
   also reports as `dph_base`, and not the all-in price that includes the
   allocated disk. Rows are ordered by the all-in price when `order` names
   `dph_total`. A "price above the last row" filter on `dph_total` would then skip
   every row whose GPU-only price is at or below the last all-in price while its
   all-in price is above it.

For that reason the driver pages by `dph_base`, which the server both orders and
filters on, and builds pages ordered by the all-in price from those. See
`_BundleStream`.

The lifecycle has synthetic tests and is untested on a rented machine.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import httpx

from ablit import __version__
from ablit.backends.base import (Allocation, BootSpec, Capabilities, Creds,
                                 Inventory, Observation, OfferSearch, WatchdogCred)
from ablit.backends.errors import (
    CredentialsRejected,
    RequestRejected,
    ResponseNotRecognized,
    UnknownFieldType,
    UnknownUnit,
)
from ablit.backends import fields
from ablit.backends.rental import RentalBackend
from ablit.backends.transport import backoff, pace, refuse_redirect, send_with_retry
from ablit.select.cost import total_cost
from ablit.select.filters import apply_filter_chain, floor_for
from ablit.select.search import PAGE_SIZE, FilterRejected, search
from ablit.select.types import HardwareRequest, Offer, PlanOptions

BUNDLES_URL = "https://console.vast.ai/api/v0/bundles/"

# Vast reports RAM in MB. A GPU sold as "24 GB" reports 24564 MB, so dividing by
# 1024 would put every 24 GB card below a 24 GB floor. The driver uses decimal
# gigabytes, the same unit the catalog and the disk figures use.
MB_PER_GB = 1000.0

VERIFICATION_VALUES = {"verified": True, "unverified": False, "deverified": False}
GPU_VENDORS = {"nvidia", "amd"}

# The most server queries one search may send. Each query returns up to 64 rows.
MAX_SERVER_PAGES = 30

# Vast gives the transfer price per GB in `inet_down_cost` and per TB (1024 GB)
# in `internet_down_cost_per_tb`. The driver checks that the two agree, so a
# change of unit in either field is refused.
GB_PER_TB = 1024.0


class PagingStopped(FilterRejected):
    """The driver cannot get further pages, so the search is labeled incomplete.

    `rows` holds the final rows that were ready when the scan stopped, so that
    the caller can still count and rank them.
    """

    def __init__(self, message: str, rows: list[Offer] | None = None) -> None:
        super().__init__(message)
        self.rows = rows or []


def _gb_from_mb(mb: float) -> float:
    return mb / MB_PER_GB


def _mb_from_gb(gb: float) -> int:
    return int(round(gb * MB_PER_GB))


def _region(geolocation: str | None) -> str | None:
    """Return the two-letter country code at the end of a location such as "Texas, US"."""
    if not geolocation or "," not in geolocation:
        return None
    code = geolocation.rsplit(",", 1)[1].strip().upper()
    return code if len(code) == 2 and code.isalpha() else None


def offer_from_row(row: Any, allocated_gb: float) -> Offer:
    """Turn one row of a Vast response into an Offer, or raise a typed error.

    The row must have the fields and units that the driver knows. Nothing is
    guessed. `allocated_gb` is the disk the price in the row was requested for.
    """
    name = "vast"
    if not isinstance(row, dict):
        raise UnknownFieldType(f"vast: a row is a {type(row).__name__}, not an object")
    where = "a row"
    offer_id = fields.integer(row, "id", backend=name, where=where, minimum=1)
    where = f"offer {offer_id}"
    num_gpus = fields.integer(row, "num_gpus", backend=name, where=where, minimum=1)
    gpu_ram_mb = fields.number(row, "gpu_ram", backend=name, where=where, minimum=1)
    cpu_ram_mb = fields.number(row, "cpu_ram", backend=name, where=where, minimum=0)
    disk_gb = fields.number(row, "disk_space", backend=name, where=where, minimum=0)
    all_in = fields.number(row, "dph_total", backend=name, where=where, minimum=0)
    base = fields.number(row, "dph_base", backend=name, where=where, minimum=0)
    transfer_per_gb = fields.number(row, "inet_down_cost", backend=name, where=where, minimum=0)
    download = fields.number(row, "inet_down", backend=name, where=where, nullable=True, minimum=0)
    cap = fields.number(row, "compute_cap", backend=name, where=where, nullable=True, minimum=0)
    if cap == 0:
        cap = None  # a host with no CUDA capability, such as an AMD host, reports 0
    duration = fields.number(row, "duration", backend=name, where=where, nullable=True, minimum=0)
    reliability = fields.number(row, "reliability", backend=name, where=where, nullable=True, minimum=0)
    fraction = fields.number(row, "gpu_frac", backend=name, where=where, minimum=0)
    gpu_name = fields.text(row, "gpu_name", backend=name, where=where)
    arch = fields.text(row, "gpu_arch", backend=name, where=where)
    verification = fields.text(row, "verification", backend=name, where=where)
    rentable = fields.boolean(row, "rentable", backend=name, where=where)
    geolocation = fields.text(row, "geolocation", backend=name, where=where, nullable=True)
    is_bid = row.get("is_bid", False)
    if not isinstance(is_bid, bool):
        raise UnknownFieldType(f"vast: field 'is_bid' in {where} is not true or false")
    assert gpu_name is not None and arch is not None and verification is not None
    assert gpu_ram_mb is not None and cpu_ram_mb is not None and disk_gb is not None
    assert all_in is not None and base is not None and transfer_per_gb is not None and fraction is not None

    if "cpu_arch" in row and fields.text(row, "cpu_arch", backend=name) != "amd64":
        raise UnknownFieldType("vast: only amd64 engine images are supported")
    if arch not in GPU_VENDORS:
        raise UnknownFieldType(f"vast: field 'gpu_arch' in {where} is {arch!r}, which the driver does not know")
    if verification not in VERIFICATION_VALUES:
        raise UnknownFieldType(
            f"vast: field 'verification' in {where} is {verification!r}, which the driver does not know"
        )
    if fraction <= 0 or fraction > 1:
        raise UnknownUnit(f"vast: field 'gpu_frac' in {where} is {fraction}, outside the range above 0 and up to 1")
    if reliability is not None and reliability > 1:
        raise UnknownUnit(f"vast: field 'reliability' in {where} is {reliability}, but the driver expects 0 to 1")
    if cap is not None and not 100 <= cap <= 2000:
        raise UnknownUnit(f"vast: field 'compute_cap' in {where} is {cap}, but the driver expects the capability times 100")
    if all_in + 1e-9 < base:
        raise UnknownUnit(f"vast: in {where} the all-in price {all_in} is below the GPU-only price {base}")

    # Cross-checks between fields that must agree if the units are the ones the driver expects.
    total_ram_mb = row.get("gpu_total_ram")
    if total_ram_mb is not None:
        total_ram_mb = fields.number(row, "gpu_total_ram", backend=name, where=where, minimum=1)
        assert total_ram_mb is not None
        if not math.isclose(total_ram_mb, gpu_ram_mb * num_gpus, rel_tol=0.01):
            raise UnknownUnit(
                f"vast: in {where} 'gpu_total_ram' ({total_ram_mb}) is not 'gpu_ram' ({gpu_ram_mb}) times 'num_gpus' ({num_gpus})"
            )
    per_tb = row.get("internet_down_cost_per_tb")
    if per_tb is not None:
        per_tb = fields.number(row, "internet_down_cost_per_tb", backend=name, where=where, minimum=0)
        assert per_tb is not None
        if not math.isclose(transfer_per_gb * GB_PER_TB, per_tb, rel_tol=0.02, abs_tol=0.01):
            raise UnknownUnit(
                f"vast: in {where} 'inet_down_cost' ({transfer_per_gb}) is not 'internet_down_cost_per_tb' "
                f"({per_tb}) divided by {GB_PER_TB:g}, so its unit is not the per-GB price the driver expects"
            )

    return Offer(
        backend=name,
        offer_id=str(offer_id),
        gpu_model=gpu_name,
        gpu_count=num_gpus,
        gpu_vram_gb_each=_gb_from_mb(gpu_ram_mb),
        hourly_all_in=all_in,
        host_ram_gb=_gb_from_mb(cpu_ram_mb),
        disk_gb=disk_gb,
        gpu_vendor=arch,
        compute_capability=None if cap is None else cap / 100.0,
        sticker_hourly=base,
        bandwidth_cost_per_gb=transfer_per_gb,
        download_mbps=download,
        rentable=rentable,
        interruptible=is_bid,
        remaining_hours=None if duration is None else duration / 3600.0,
        verified=VERIFICATION_VALUES[verification],
        reliability=reliability,
        region=_region(geolocation),
        allocated_disk_gb=allocated_gb,
        gpu_fraction=fraction,
        host_id=str(fields.integer(row, "host_id", backend=name, minimum=1)) if "host_id" in row else None,
    )


def _disk_request_gb(disk_gb: float) -> int:
    """Round the disk up to a whole GB, so the price covers at least what the profile needs."""
    return int(math.ceil(disk_gb - 1e-9))


def retry_delay(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before a retry. Honor Retry-After when Vast sends seconds."""
    value = response.headers.get("Retry-After", "")
    if value.isdigit():
        return min(float(value), 60.0)
    # Vast sends the end of its rate-limit window as seconds since the epoch.
    reset = response.headers.get("x-ratelimit-reset", "")
    try:
        wait = float(reset) - time.time()
    except ValueError:
        wait = -1.0
    if 0 < wait <= 60:
        return wait + 0.5
    return backoff(attempt)


def build_body(
    req: HardwareRequest,
    opts: PlanOptions,
    *,
    base_cursor: float | None = None,
) -> dict[str, Any]:
    """Return the request body for one search query.

    Every filter is sent to the server. `base_cursor` is the GPU-only price at
    which a later page starts, inclusive.
    """
    disk = _disk_request_gb(req.disk_gb)
    body: dict[str, Any] = {
        "rentable": {"eq": True},
        "type": "on-demand",
        "gpu_frac": {"eq": 1},
        "gpu_arch": {"eq": req.gpu_vendor},
        "cpu_arch": {"eq": "amd64"},
        "num_gpus": {"eq": req.gpu_count},
        "gpu_ram": {"gte": _mb_from_gb(req.vram_gb_each)},
        "cpu_ram": {"gte": _mb_from_gb(req.host_ram_gb)},
        "disk_space": {"gte": disk},
        "allocated_storage": disk,
        "inet_down": {"gte": floor_for(req, opts)},
        "duration": {"gte": int(math.ceil((req.startup_hours + opts.hours) * 3600))},
        "order": [["dph_base", "asc"]],
    }
    if req.cuda_version_min is not None:
        body["cuda_max_good"] = {"gte": req.cuda_version_min}
    if req.vram_gb_total is not None:
        body["gpu_total_ram"] = {"gte": _mb_from_gb(req.vram_gb_total)}
    # The search can only bound the capability from below. The exact set of
    # generations in the engine image is checked on the offers that come back.
    floors = [req.compute_capability_min] if req.compute_capability_min is not None else []
    if req.cuda_architectures:
        floors.append(min(req.cuda_architectures) / 10)
    if floors:
        body["compute_cap"] = {"gte": int(round(max(floors) * 100))}
    if opts.trust == "verified":
        body["verified"] = {"eq": True}
    if opts.min_reliability is not None:
        body["reliability"] = {"gte": opts.min_reliability}
    if opts.region is not None:
        body["geolocation"] = {"in": [opts.region.upper()]}
    base_filter: dict[str, float] = {}
    if base_cursor is not None:
        base_filter["gte"] = base_cursor
    if opts.max_price_hr is not None:
        # The all-in price is never below the GPU-only price, so this keeps a
        # superset of the rows that pass the --max-price-hr filter.
        base_filter["lte"] = opts.max_price_hr
    if base_filter:
        body["dph_base"] = base_filter
    return body


class _BundleStream:
    """Pages of offers ordered by all-in price, built from queries ordered by GPU-only price.

    The search rule needs each page to hold the cheapest rows that remain, by
    all-in price. The server can only continue a scan by GPU-only price, so the
    stream reads server pages into a buffer. A row can leave the buffer once its
    all-in price is below the GPU-only price of the last row read, because every
    row not yet read has a GPU-only price at or above that, and an all-in price
    at least as high.
    """

    def __init__(
        self,
        query: Callable[[float | None], list[Offer]],
        page_size: int = PAGE_SIZE,
        max_server_pages: int = MAX_SERVER_PAGES,
    ) -> None:
        self._query = query
        self._page_size = page_size
        self._max_server_pages = max_server_pages
        self._buffer: dict[str, Offer] = {}
        self._cursor: float | None = None
        self._watermark = -math.inf  # rows below this all-in price are final
        self._exhausted = False
        self._stopped: Exception | None = None
        self.server_pages = 0

    def _pull(self) -> None:
        """Read one more server page, or record why no more pages can be read."""
        if self.server_pages >= self._max_server_pages:
            self._stopped = PagingStopped(f"stopped after {self._max_server_pages} server queries")
            return
        try:
            rows = self._query(self._cursor)
        except FilterRejected as error:
            self._stopped = error
            return
        self.server_pages += 1
        new_rows = 0
        for offer in rows:
            if offer.offer_id not in self._buffer:
                self._buffer[offer.offer_id] = offer
                new_rows += 1
        if len(rows) < self._page_size:
            self._exhausted = True
            return
        last_base = rows[-1].sticker_hourly
        assert last_base is not None
        if new_rows == 0:
            self._stopped = PagingStopped(
                f"{self._page_size} or more offers share the GPU-only price {last_base}, so the scan cannot move past them"
            )
            return
        self._cursor = last_base
        self._watermark = last_base

    def fetch(self, after: float | None) -> list[Offer]:
        """Return the next page of rows with an all-in price above `after`."""
        floor = -math.inf if after is None else after

        def ready() -> list[Offer]:
            rows = [
                o
                for o in self._buffer.values()
                if o.hourly_all_in > floor and (self._exhausted or o.hourly_all_in < self._watermark)
            ]
            rows.sort(key=lambda o: (o.hourly_all_in, o.offer_id))
            return rows

        rows = ready()
        while len(rows) < self._page_size and not self._exhausted and self._stopped is None:
            self._pull()
            rows = ready()
        if len(rows) < self._page_size and not self._exhausted and self._stopped is not None:
            # The scan cannot continue. A short page would read as "no more rows",
            # so report the stop and hand over the rows that are final with it.
            raise PagingStopped(str(self._stopped), rows)
        page = rows[: self._page_size]
        # Keep rows that share a price together, because the search rule continues from "above" that price.
        index = self._page_size
        while page and index < len(rows) and rows[index].hourly_all_in == page[-1].hourly_all_in:
            page.append(rows[index])
            index += 1
        return page


class VastBackend(RentalBackend):
    name = "vast"
    inventory_interval = 1.1
    refused_statuses = (400, 401, 403, 404, 410)
    _sleep = staticmethod(time.sleep)

    def __init__(self, client: httpx.Client | None = None, url: str = BUNDLES_URL, *, store=None) -> None:
        super().__init__(client, store=store)
        self._url = url
        self._inventory_at: float | None = None

    def capabilities(self) -> Capabilities:
        return Capabilities(search=True, rent=True, scoped_watchdog_credential=True)

    # --- HTTP -------------------------------------------------------------

    def _post(self, body: dict[str, Any], creds: Creds | None, *, later_page: bool = False) -> list[Any]:
        headers = {"Content-Type": "application/json", "User-Agent": f"ablit/{__version__}"}
        if creds is not None:
            headers["Authorization"] = f"Bearer {creds.api_key}"
        # https://docs.vast.ai/api-reference/search/search-offers
        response = self._http().post(self._url, json=body, headers=headers)
        status = response.status_code
        if status in (401, 403):
            raise CredentialsRejected(self.name, status, "the API key was refused")
        if later_page and status in (400, 422):
            raise FilterRejected(f"vast refused a later-page query with HTTP {status}")
        if status == 429:
            raise RequestRejected(self.name, status, "rate limited, try again later")
        if status < 200 or status >= 300:
            raise RequestRejected(self.name, status, "the request failed")
        try:
            data = response.json()
        except ValueError as error:
            raise ResponseNotRecognized("vast: the response is not JSON") from error
        if not isinstance(data, dict) or not isinstance(data.get("offers"), list):
            raise ResponseNotRecognized("vast: the response has no 'offers' list")
        return data["offers"]

    # --- search -----------------------------------------------------------

    def _query_page(
        self, req: HardwareRequest, creds: Creds | None, opts: PlanOptions
    ) -> Callable[[float | None], list[Offer]]:
        allocated = float(_disk_request_gb(req.disk_gb))

        def query(base_cursor: float | None) -> list[Offer]:
            body = build_body(req, opts, base_cursor=base_cursor)
            if self.denied_host_ids:
                body["host_id"] = {"notin": sorted(int(host) for host in self.denied_host_ids)}
            rows = self._post(body, creds, later_page=base_cursor is not None)
            offers = [offer_from_row(row, allocated) for row in rows]
            bases = [o.sticker_hourly for o in offers]
            if any(b is None for b in bases):  # pragma: no cover - offer_from_row always sets it
                raise UnknownFieldType("vast: a row has no GPU-only price")
            if bases != sorted(bases):  # type: ignore[type-var]
                raise ResponseNotRecognized("vast: rows are not ordered by GPU-only price, so paging would be wrong")
            if base_cursor is not None and any(b < base_cursor for b in bases):  # type: ignore[operator]
                raise FilterRejected("vast returned rows below the price the later page was asked to start at")
            return offers

        return query

    def page_fetcher(
        self, req: HardwareRequest, creds: Creds | None, opts: PlanOptions | None = None
    ) -> Callable[[float | None], list[Offer]]:
        """Return the page fetcher that the search rule in ablit.select.search takes.

        The fetcher takes the all-in price above which a page starts, or None
        for the first page. It raises FilterRejected when the scan cannot continue.
        """
        stream = _BundleStream(self._query_page(req, creds, opts or PlanOptions()))
        return stream.fetch

    def search(
        self, req: HardwareRequest, creds: Creds | None, opts: PlanOptions | None = None
    ) -> OfferSearch:
        opts = opts or PlanOptions()
        fetch = self.page_fetcher(req, creds, opts)
        seen: list[Offer] = []
        floor = floor_for(req, opts)

        def recording(after: float | None) -> list[Offer]:
            try:
                page = fetch(after)
            except PagingStopped as stopped:
                seen.extend(stopped.rows)
                raise
            seen.extend(page)
            return page

        outcome = search(
            fetch_page=recording,
            qualify=lambda page: apply_filter_chain(page, req, opts).kept,
            total_of=lambda offer: total_cost(offer, req, opts.hours, floor),
            hours=opts.hours,
            startup_hours=req.startup_hours,
        )
        # Count the rows the planner will rank, including any final rows handed over when paging stopped.
        label = None if outcome.complete else replace(outcome, checked=len(seen)).label
        return OfferSearch(backend=self.name, offers=seen, complete=outcome.complete, note=label)

    # --- requote ----------------------------------------------------------

    def requote(self, offer: Offer, creds: Creds) -> Offer | None:
        """Look the offer up again at its allocated disk. Return None when it is gone."""
        if offer.allocated_disk_gb is None:
            raise ValueError("vast: the offer has no allocated disk, so it cannot be quoted again")
        try:
            wanted = int(offer.offer_id)
        except ValueError as error:
            raise ValueError(f"vast: {offer.offer_id!r} is not a Vast offer ID") from error
        allocated = _disk_request_gb(offer.allocated_disk_gb)
        # The server's `id` filter matched nothing in live checks. `ask_contract_id` is the same number and works.
        body = {
            "ask_contract_id": {"eq": wanted},
            "rentable": {"eq": True},
            "type": "on-demand",
            "allocated_storage": allocated,
        }
        rows = self._post(body, creds)
        offers = [offer_from_row(row, float(allocated)) for row in rows]
        for found in offers:
            if found.offer_id == offer.offer_id:
                return found
        return None

    def _request(self, method: str, path: str, creds: Creds, *, body=None, params=None, missing_ok=False, missing_error=None):
        """Send one request without retrying a mutation or echoing an error body."""
        client = self._http()
        if method == "GET" and path == "/api/v1/instances/":
            self._inventory_at = pace(self._inventory_at, self.inventory_interval, time.sleep)
        # Vast answers a path without its trailing slash with a 301 redirect, and
        # a redirected DELETE or PUT is not resent. Every path ends with a slash,
        # and redirects are refused rather than followed.
        if not path.endswith("/"):
            raise ValueError("Vast API paths must end with a slash.")
        # Reads and deletes are safe to repeat, so they wait and retry when Vast
        # rate-limits (429) or has a server error. Creates are never resent.
        attempts = 5 if method in ("GET", "DELETE") else 1
        response = send_with_retry(
            lambda: client.request(method, "https://console.vast.ai" + path, follow_redirects=False,
                                   headers={"Authorization": f"Bearer {creds.api_key}"},
                                   json=body, params=params),
            attempts=attempts, sleep=self._sleep, delay=retry_delay)
        if response.status_code == 404 and missing_ok:
            return None
        if response.status_code == 400 and missing_error:
            try:
                failure = response.json()
            except ValueError:
                failure = None
            if isinstance(failure, dict) and failure.get("success") is False and failure.get("error") == missing_error:
                return None
        if response.status_code in (401, 403):
            raise CredentialsRejected("vast", response.status_code, "the API key or its permissions were refused")
        refuse_redirect(self.name, response)
        if not response.is_success:
            raise RequestRejected("vast", response.status_code, "the request failed; check the Vast console")
        try:
            data = response.json()
        except ValueError as error:
            raise ResponseNotRecognized("vast: expected JSON") from error
        if isinstance(data, dict) and data.get("success") is False:
            raise RequestRejected("vast", response.status_code, "success was false")
        return data

    @staticmethod
    def _object(data) -> dict:
        if not isinstance(data, dict):
            raise ResponseNotRecognized("vast: expected an object")
        return data

    @staticmethod
    def _success(data) -> dict:
        data = VastBackend._object(data)
        if fields.boolean(data, "success", backend="vast") is not True:
            raise ResponseNotRecognized("vast: success was not true")
        return data

    def test_credentials(self, creds: Creds) -> None:
        # https://docs.vast.ai/api-reference/instances/show-instances
        self._success(self._request("GET", "/api/v1/instances/", creds,
                                    params={"limit": 1, "select_cols": '["id"]'}))

    def instances(self, creds: Creds) -> list[dict]:
        """Read all pages. No filtered page can prove that billing has stopped."""
        out, seen = [], set()
        params = {"limit": 25, "select_cols": '["id","label","actual_status","intended_status","ssh_host","ssh_port","host_id","dph_total","volume_info"]'}
        while True:
            # https://docs.vast.ai/api-reference/instances/show-instances
            data = self._success(self._request("GET", "/api/v1/instances/", creds, params=params))
            if not isinstance(data.get("instances"), list) or "next_token" not in data:
                raise ResponseNotRecognized("vast: missing instances or pagination cursor")
            for row in data["instances"]:
                row = self._object(row)
                fields.integer(row, "id", backend="vast", minimum=1)
                label = row.get("label")
                if label is not None and not isinstance(label, str):
                    raise UnknownFieldType("vast: label must be text or null")
                out.append(row)
            token = data["next_token"]
            if token is None:
                return out
            if not isinstance(token, str) or not token or token in seen:
                raise ResponseNotRecognized("vast: invalid or repeated pagination cursor")
            seen.add(token)
            params["after_token"] = token

    def reconcile(self, run_id: str, creds: Creds) -> Inventory:
        rows = [r for r in self.instances(creds) if r.get("label") == self.label(run_id)]
        for row in rows:
            if row.get("volume_info"):
                raise ResponseNotRecognized("vast: this run never requests volumes; unexpected volume_info needs console review")
        return self._inventory(run_id, rows)

    def _send_create(self, offer: Offer, boot: BootSpec, run_id: str, creds: Creds, prepared: Any) -> Any:
        body = {"image": boot.image, "disk": boot.disk_gb, "label": self.label(run_id),
                "runtype": "ssh", "target_state": "running", "onstart": boot.onstart,
                "cancel_unavail": True}
        # The operation schema specifies PUT; its shared introduction says POST.
        # https://docs.vast.ai/api-reference/instances/create-instance
        return self._success(self._request("PUT", f"/api/v0/asks/{int(offer.offer_id)}/", creds, body=body))

    def _read_created(self, data: Any, record: dict[str, Any], creds: Creds) -> list[str]:
        return [str(fields.integer(data, "new_contract", backend="vast", minimum=1))]

    def observe(self, alloc: Allocation, creds: Creds) -> Observation:
        row = self._one([r for r in self.instances(creds) if str(r["id"]) in alloc.resource_ids], "instance")
        if row is None:
            return self._absent()
        status = fields.text(row, "actual_status", backend="vast", nullable=True)
        # ablit always asks for "running". When Vast itself gives up on starting
        # an instance, for example after a failed image pull, it sets the
        # intended status to "stopped" and the instance never starts.
        if row.get("intended_status") is not None and fields.text(row, "intended_status", backend="vast") == "stopped":
            status = "stopped_by_marketplace"
        rate = fields.number(row, "dph_total", backend="vast", minimum=0)
        host = row.get("ssh_host")
        port = row.get("ssh_port")
        if host is not None:
            host = fields.text(row, "ssh_host", backend="vast")
        if port is not None:
            port = fields.integer(row, "ssh_port", backend="vast", minimum=1)
            if port > 65535:
                raise UnknownFieldType("vast: invalid SSH port")
        host_id = str(fields.integer(row, "host_id", backend="vast", minimum=1)) if row.get("host_id") is not None else None
        found = Allocation(alloc.run_id, "vast", alloc.resource_ids, rate, host, port, host_id)
        # The list schema has no documented cumulative USD cost or transfer counter.
        # A new instance has no status while Vast prepares it. Vast's API notes
        # say a literal "unknown" status never reaches "running", so the two
        # are kept apart.
        return Observation(time.time(), status or "starting", hourly_rate=rate, allocation=found)

    def _terminate(self, resource_id: str, creds: Creds) -> None:
        # https://docs.vast.ai/api-reference/instances/destroy-instance
        data = self._request("DELETE", f"/api/v0/instances/{int(resource_id)}/", creds, missing_ok=True)
        if data is not None:
            self._success(data)

    def _still_billable(self, inv: Inventory, creds: Creds) -> list[str]:
        # https://docs.vast.ai/api-reference/instances/show-instances
        rows = self.instances(creds)
        return [str(r["id"]) for r in rows
                if str(r["id"]) in inv.resource_ids or r.get("label") == self.label(inv.run_id)]

    def watchdog_credential(self, alloc: Allocation, creds: Creds) -> WatchdogCred:
        if len(alloc.resource_ids) != 1:
            raise ResponseNotRecognized("vast: a watchdog needs exactly one instance")
        instance = int(alloc.resource_ids[0])
        # The permissions page shows the name "api.instance.destroy", but Vast
        # refuses that name, and any other name it does not know, with HTTP 401
        # (checked 2026-10-21). Vast's own built-in rights name the route
        # "api.instance" and nest the HTTP method, so the key gets DELETE on
        # that route for this one instance ID only.
        # https://docs.vast.ai/api-reference/accounts/create-api-key
        # https://docs.vast.ai/cli/permissions
        permissions = {"api": {"instance_write": {"api.instance": {"DELETE": {"constraints": {"id": {"eq": instance}}}}}}}
        data = self._object(self._request("POST", "/api/v0/auth/apikeys/", creds,
                                         body={"name": self.label(alloc.run_id), "permissions": permissions}))
        key_id = str(fields.integer(data, "id", backend="vast", minimum=1))
        token = fields.text(data, "key", backend="vast")
        assert token is not None
        return WatchdogCred(str(instance), token, key_id)

    def delete_watchdog_key(self, key_id: str, creds: Creds) -> None:
        # https://docs.vast.ai/api-reference/accounts/delete-api-key
        data = self._request("DELETE", f"/api/v0/auth/apikeys/{int(key_id)}/", creds, missing_ok=True)
        if data is not None and data != "Successfully Deleted API Key":
            raise ResponseNotRecognized("vast: unknown API key deletion response")

    def register_ssh_key(self, public_key: str, creds: Creds) -> str:
        # https://docs.vast.ai/api-reference/accounts/create-ssh-key
        data = self._success(self._request("POST", "/api/v0/ssh/", creds, body={"ssh_key": public_key}))
        key = self._object(data.get("key"))
        return str(fields.integer(key, "id", backend="vast", minimum=1))

    def delete_ssh_key(self, key_id: str, creds: Creds) -> None:
        # https://docs.vast.ai/api-reference/accounts/delete-ssh-key
        data = self._request("DELETE", f"/api/v0/ssh/{int(key_id)}/", creds, missing_error="no_ssh_key")
        if data is not None:
            self._success(data)
