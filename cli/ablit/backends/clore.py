"""Clore driver. See backends/clore.md at the top of the repository.

Every request, field and unit below comes from Clore's own documentation. The
call that reads the marketplace is documented to need an API key, but live checks
on 2026-10-20 and 2026-10-21 showed that it also answers with no key, so a search
works without one. Four facts shape the code.

1. Clore returns every listed server in one answer and has no server-side filter.
   The driver turns each server into an offer and leaves the filtering to the
   planner, so the search always sees all of the marketplace.
2. Prices are US dollars per day for the whole server. The driver reads only the
   price in the "USD-Blockchain" currency, which Clore documents as USD per day,
   so it needs no exchange rate. Servers that do not take that currency are not
   offers, and the search note counts them.
3. The renter pays a base fee on top of the listed price, and a one-time order
   creation fee that Clore charges only when the order stays active for more than
   10 minutes. Billing is per minute, and the disk is part of the server price.
4. The create call returns no label and, in the reference, no order ID. The driver
   journals what it sent (server, image and time) before sending it, and finds
   the order afterwards in the order list by those three values. A create is
   never sent twice for one run.

There is no API key limited to one order or one server, so `watchdog_credential`
returns None and the machine cannot cancel its own order. See the notes file.

The lifecycle has synthetic tests and is untested on a rented machine.
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from collections.abc import Mapping
from typing import Any

import httpx

from ablit import __version__
from ablit.backends import fields
from ablit.backends.base import (
    Allocation,
    BootSpec,
    Capabilities,
    Creds,
    Inventory,
    Observation,
    OfferSearch,
    WatchdogCred,
)
from ablit.backends.clore_gpus import compute_capability_for
from ablit.backends.errors import (
    CreateUncertain,
    CredentialsRejected,
    RequestRejected,
    ResponseNotRecognized,
    UnknownFieldType,
    UnknownUnit,
)
from ablit.backends.rental import RentalBackend
from ablit.backends.transport import pace, refuse_redirect, send_with_retry
from ablit.select.types import HardwareRequest, Offer, PlanOptions

# Base URL for every endpoint. https://docs.clore.ai/for-hosts/api
API_ROOT = "https://api.clore.ai/v1"

# The only payment currency the driver prices. Clore documents the price in this
# currency as USD per day (the USD-Blockchain_on_demand field of set_server_settings
# and the currency_rates_in_usd example), so no exchange rate is needed.
# https://docs.clore.ai/for-hosts/api
CURRENCY = "USD-Blockchain"

# The renter's share of the base fee for an on-demand order. The base fee is 10%,
# split 50/50 between renter and host, so the renter pays 5%. The renter_fees
# example in the API reference shows the same 5. A holder of CLORE in Proof of
# Holding can pay less, and the driver quotes the higher documented figure.
# https://docs.clore.ai/for-renters/fee-structure
RENTER_BASE_FEE_PERCENT = 5.0

# The one-time fee for an on-demand order paid in USD (USDT or USDC), in dollars.
# Clore charges it only when the order stays active for more than 10 minutes. The
# planner counts it in every total, which can only overstate the cost.
# https://docs.clore.ai/for-renters/fee-structure
# https://docs.clore.ai/for-renters/order-management
CREATION_FEE_USD = 0.10

# Hosts may list a USD price per day only from 0.1 to 10,000, and a rental length
# of 6 to 3000 hours. A value outside these ranges means the unit is not the one
# the driver expects, so the driver refuses it.
# https://docs.clore.ai/for-hosts/api (set_server_settings)
USD_PRICE_PER_DAY_RANGE = (0.1, 10000.0)
MAX_RENTAL_HOURS = 3000

HOURS_PER_DAY = 24.0

# The API allows 1 request per second per key, and answers code 5 above that.
# https://docs.clore.ai/for-hosts/api
REQUEST_INTERVAL_SECONDS = 1.1

# The order list is matched to a create request by the server, the image and the
# creation time. Clore's clock and this computer's clock can differ, so a time
# slightly before the journal entry still counts.
CLOCK_SLACK_SECONDS = 300

# The key Clore accepts in the create call. Its length limit and character set are
# documented, so the driver checks them before it sends anything.
# https://docs.clore.ai/for-hosts/api (create_order)
SSH_KEY_MAX_CHARS = 3072
SSH_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9\s\-=.@+/]+$")

_GPU_PATTERN = re.compile(r"^(\d+)x (.+)$")
_DISK_PATTERN = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*GB\s*$")
_CUDA_PATTERN = re.compile(r"^(\d+)\.(\d)$")
_ERROR_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HOST_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")

NVIDIA_PREFIXES = ("NVIDIA ", "Tesla ", "Quadro ")
AMD_PREFIXES = ("AMD ",)


class CloreRejected(RequestRejected):
    """Clore answered with an error code. `code` is the code field and `error` its name, when it has one."""

    def __init__(self, status: int | None, code: int, error: str | None) -> None:
        detail = f"code {code}" + (f", {error}" if error else "")
        super().__init__("clore", status, detail)
        self.code = code
        self.error = error

    @property
    def definitive(self) -> bool:
        """True when the answer proves the request did nothing.

        Code 2 (invalid input), 3 (invalid key), 4 (invalid endpoint) and 6 (an
        error named in the error field) are refusals. Code 1 (database error) and
        code 5 (rate limit) say nothing certain about whether the request ran.
        https://docs.clore.ai/for-hosts/api
        """
        return self.code in (2, 3, 4, 6)


class SkipServer(Exception):
    """A listed server is not an offer. `reason` names the group it is counted in."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _cuda(value: Any, where: str) -> float | None:
    """Read a CUDA version such as "13.2" as a number. Two-digit minors are refused."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise UnknownFieldType(f"clore: field 'cuda_version' in {where} is not text")
    match = _CUDA_PATTERN.match(value)
    if match is None:
        raise UnknownUnit(f"clore: field 'cuda_version' in {where} is {value!r}, not a major.minor version")
    return int(match.group(1)) + int(match.group(2)) / 10.0


def offer_from_row(
    row: Any,
    *,
    fee_percent: float = RENTER_BASE_FEE_PERCENT,
    creation_fee_usd: float = CREATION_FEE_USD,
    cuda_min: float | None = None,
) -> Offer:
    """Turn one server of a marketplace answer into an Offer.

    Raises SkipServer for a server that is not an offer, and a typed error for a
    shape or unit the driver does not know. Nothing is guessed.
    """
    name = "clore"
    if not isinstance(row, dict):
        raise UnknownFieldType(f"clore: a server is a {type(row).__name__}, not an object")
    server_id = fields.integer(row, "id", backend=name, where="a server", minimum=1)
    where = f"server {server_id}"
    specs = row.get("specs")
    if not isinstance(specs, dict):
        raise UnknownFieldType(f"clore: field 'specs' in {where} is not an object")
    gpu_text = fields.text(specs, "gpu", backend=name, where=where)
    assert gpu_text is not None
    match = _GPU_PATTERN.match(gpu_text)
    if match is None:
        raise UnknownFieldType(f"clore: field 'gpu' in {where} is {gpu_text!r}, not a count and a name")
    gpu_count, gpu_name = int(match.group(1)), match.group(2)
    if gpu_count < 1:
        raise UnknownFieldType(f"clore: field 'gpu' in {where} counts no GPUs")
    if gpu_name == "Mixed GPUs":
        raise SkipServer("mixed-GPU rigs")

    mrl = fields.integer(row, "mrl", backend=name, where=where, minimum=1)
    rented = fields.boolean(row, "rented", backend=name, where=where)
    price = row.get("price")
    if not isinstance(price, dict) or not isinstance(price.get("on_demand"), dict):
        raise UnknownFieldType(f"clore: field 'price.on_demand' in {where} is not an object")
    on_demand = price["on_demand"]
    if CURRENCY not in on_demand:
        raise SkipServer("servers with no price in USD")
    usd_per_day = fields.number(on_demand, CURRENCY, backend=name, where=f"{where} price.on_demand", minimum=0)
    assert usd_per_day is not None
    low, high = USD_PRICE_PER_DAY_RANGE
    if not low <= usd_per_day <= high:
        raise UnknownUnit(
            f"clore: the USD price of {where} is {usd_per_day}, outside the {low:g} to {high:g} dollars per day "
            f"that Clore allows, so the unit is not the one the driver expects"
        )
    # The marketplace also carries a block of prices converted to USD. The API
    # reference does not describe it, so the driver uses it only as a cross-check
    # on the unit: where the block names the USD price, it must agree.
    converted = price.get("usd")
    if isinstance(converted, dict) and "on_demand_usd" in converted:
        other = fields.number(converted, "on_demand_usd", backend=name, where=f"{where} price.usd")
        assert other is not None
        if not math.isclose(other, usd_per_day, rel_tol=1e-6, abs_tol=1e-9):
            raise UnknownUnit(f"clore: the two USD prices of {where} disagree ({usd_per_day} and {other})")

    if mrl > MAX_RENTAL_HOURS:
        raise UnknownUnit(f"clore: field 'mrl' in {where} is {mrl}, above the {MAX_RENTAL_HOURS} hours Clore allows")
    gpu_vram = fields.number(specs, "gpuram", backend=name, where=where, minimum=0)
    host_ram = fields.number(specs, "ram", backend=name, where=where, minimum=0)
    disk_text = fields.text(specs, "disk", backend=name, where=where)
    assert gpu_vram is not None and host_ram is not None and disk_text is not None
    disk_match = _DISK_PATTERN.search(disk_text)
    if disk_match is None:
        raise UnknownFieldType(f"clore: field 'disk' in {where} does not end with a size in GB")
    disk_gb = float(disk_match.group(1))

    net = specs.get("net")
    if not isinstance(net, dict):
        raise UnknownFieldType(f"clore: field 'net' in {where} is not an object")
    download = fields.number(net, "down", backend=name, where=f"{where} net", nullable=True, minimum=0) if "down" in net else None
    # The country code is optional, and hosts that set none send an empty text.
    country = net.get("cc")
    if country is not None and not isinstance(country, str):
        raise UnknownFieldType(f"clore: field 'cc' in {where} net is not text")
    country = (country or "").strip()
    region = country.upper() if len(country) == 2 and country.isalpha() else None

    # Partial rentals share the machine's RAM and disk with other renters, and
    # the driver rents whole servers only. A server with GPUs rented to someone
    # else is therefore not available as a whole.
    # https://docs.clore.ai/for-renters/partial-gpu-rental
    partial = row.get("partial_gpu_rental", False)
    whole_server_free = True
    if isinstance(partial, dict):
        total = fields.integer(partial, "total_gpus", backend=name, where=f"{where} partial_gpu_rental", minimum=0)
        free = fields.integer(partial, "available_gpus", backend=name, where=f"{where} partial_gpu_rental", minimum=0)
        if total != gpu_count:
            raise UnknownFieldType(f"clore: 'total_gpus' in {where} is {total}, but the server lists {gpu_count} GPUs")
        whole_server_free = free == total
    elif not isinstance(partial, bool):
        raise UnknownFieldType(f"clore: field 'partial_gpu_rental' in {where} is not false or an object")

    # These two fields are in the live answer but not in the API reference, so
    # a missing value is allowed and a value of an unexpected type is refused.
    reliability = None
    if row.get("reliability") is not None:
        reliability = fields.number(row, "reliability", backend=name, where=where, minimum=0)
        assert reliability is not None
        if reliability > 1:
            raise UnknownUnit(f"clore: field 'reliability' in {where} is {reliability}, but the driver expects 0 to 1")
    cuda = _cuda(row.get("cuda_version"), where)
    if cuda_min is not None and (cuda is None or cuda < cuda_min):
        raise SkipServer(f"servers below CUDA {cuda_min:g} or with no CUDA version")

    if gpu_name.startswith(NVIDIA_PREFIXES):
        vendor = "nvidia"
    elif gpu_name.startswith(AMD_PREFIXES):
        vendor = "amd"
    else:
        raise SkipServer("servers whose GPU vendor the driver does not know")
    display = gpu_name
    for prefix in ("NVIDIA GeForce ", "NVIDIA "):
        if display.startswith(prefix):
            display = display[len(prefix):]
            break

    return Offer(
        backend=name,
        offer_id=str(server_id),
        gpu_model=display,
        gpu_count=gpu_count,
        gpu_vram_gb_each=gpu_vram,
        # The listed price plus the renter's base fee, per hour. The price covers the whole server, disk included.
        hourly_all_in=usd_per_day * (1.0 + fee_percent / 100.0) / HOURS_PER_DAY,
        host_ram_gb=host_ram,
        disk_gb=disk_gb,
        gpu_vendor=vendor,
        compute_capability=compute_capability_for(gpu_name) if vendor == "nvidia" else None,
        sticker_hourly=usd_per_day / HOURS_PER_DAY,
        bandwidth_cost_per_gb=0.0,
        fixed_fees=creation_fee_usd,
        download_mbps=download,
        rentable=not rented and whole_server_free,
        interruptible=False,
        remaining_hours=float(mrl),
        verified=False,
        reliability=reliability,
        region=region,
        allocated_disk_gb=disk_gb,
        gpu_fraction=1.0,
        host_id=str(server_id),
    )


def _plural(count: int, reason: str) -> str:
    return f"{count} {reason}"


class CloreBackend(RentalBackend):
    name = "clore"
    request_interval = REQUEST_INTERVAL_SECONDS
    fault_find = "order"
    _sleep = staticmethod(time.sleep)

    def __init__(self, client: httpx.Client | None = None, root: str = API_ROOT, *, store=None) -> None:
        super().__init__(client, store=store)
        self._root = root.rstrip("/")
        self._last_request_at: float | None = None

    def capabilities(self) -> Capabilities:
        # Search works against live data. Renting is connected to `ablit up`, but the flag stays False
        # until a paid test on a rented machine shows it works.
        # There is no scoped watchdog credential on Clore, so that flag stays False for good.
        return Capabilities(search=True)

    # --- HTTP -------------------------------------------------------------

    def _pace(self) -> None:
        self._last_request_at = pace(self._last_request_at, self.request_interval, self._sleep)

    @staticmethod
    def _code_is_rate_limit(response: httpx.Response) -> bool:
        """Clore answers code 5 above its request limit, and 429 or a server error also mean wait."""
        if response.status_code == 429 or response.status_code >= 500:
            return True
        try:
            return response.json().get("code") == 5
        except (ValueError, AttributeError):
            return False

    def _call(
        self,
        method: str,
        endpoint: str,
        creds: Creds | None,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        retry: bool = False,
        timeout: float = 30.0,
        tolerate: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Send one request and return the answer once its code field is 0.

        Reads and cancels wait and retry when Clore rate-limits them or has a server
        error. A create is never resent, because a lost answer to a create is
        handled by looking for the order in the list.
        """
        client = self._http(timeout)
        headers = {"User-Agent": f"ablit/{__version__}"}
        if creds is not None:
            # The API reads the key from a header named "auth", not from Authorization.
            # https://docs.clore.ai/for-hosts/api
            headers["auth"] = creds.api_key
        response = send_with_retry(
            lambda: client.request(
                method, f"{self._root}/{endpoint}", headers=headers, json=body, params=params,
                follow_redirects=False, timeout=timeout,
            ),
            attempts=5 if retry else 1, sleep=self._sleep, limited=self._code_is_rate_limit, before=self._pace)
        status = response.status_code
        refuse_redirect(self.name, response)
        try:
            data = response.json()
        except ValueError:
            data = None
        code = data.get("code") if isinstance(data, dict) else None
        if isinstance(code, bool) or not isinstance(code, int):
            code = None
        if status in (401, 403) or code == 3:
            raise CredentialsRejected(self.name, status, "the API key was refused")
        if code is not None and code != 0:
            error = data.get("error")
            error = error if isinstance(error, str) and _ERROR_NAME.match(error) else None
            if error is not None and error in tolerate:
                return data
            raise CloreRejected(status, code, error)
        if status < 200 or status >= 300:
            raise RequestRejected(self.name, status, "the request failed")
        if not isinstance(data, dict):
            raise ResponseNotRecognized("clore: expected a JSON object")
        if code is None:
            raise ResponseNotRecognized("clore: the answer has no 'code' field")
        return data

    # --- search -----------------------------------------------------------

    def _marketplace(self, creds: Creds | None) -> list[Any]:
        # https://docs.clore.ai/for-hosts/api (marketplace)
        data = self._call("GET", "marketplace", creds, retry=True, timeout=60.0)
        servers = data.get("servers")
        if not isinstance(servers, list):
            raise ResponseNotRecognized("clore: the marketplace answer has no 'servers' list")
        return servers

    def search(
        self, req: HardwareRequest, creds: Creds | None, opts: PlanOptions | None = None
    ) -> OfferSearch:
        offers: list[Offer] = []
        skipped: Counter[str] = Counter()
        for row in self._marketplace(creds):
            try:
                offer = offer_from_row(row, cuda_min=req.cuda_version_min)
            except SkipServer as skip:
                skipped[skip.reason] += 1
                continue
            except UnknownFieldType as error:
                # A server that lacks a field ablit needs is not an offer. A field of the wrong type
                # or unit still stops the search, because that means the answer changed shape.
                if not str(error).endswith((" is missing", " is null")):
                    raise
                skipped["servers missing a field ablit needs"] += 1
                continue
            if offer.host_id in self.denied_host_ids:
                skipped["servers on the local deny list"] += 1
                continue
            offers.append(offer)
        parts = ["prices are for payment from the USD balance (USDT or USDC), with Clore's 5% renter fee and its "
                 "one-time $0.10 order fee counted"]
        if skipped:
            parts.append("left out before the filters: " + ", ".join(_plural(n, r) for r, n in sorted(skipped.items())))
        # Clore returns every server in one answer, so the search sees all of the marketplace.
        return OfferSearch(backend=self.name, offers=offers, complete=True, note="; ".join(parts))

    def renter_fees(self, creds: Creds) -> tuple[float, float]:
        """Return the account's base fee (percent) and creation fee (dollars) for on-demand USD orders."""
        # https://docs.clore.ai/for-hosts/api (renter_fees)
        data = self._call("GET", "renter_fees", creds, retry=True)
        base = self._section(data, "renter_fees", "on_demand", "base")
        creation = self._section(data, "creation_fees", "on_demand")
        fee = fields.number(base, CURRENCY, backend=self.name, where="renter_fees.on_demand.base", minimum=0)
        one_time = fields.number(creation, CURRENCY, backend=self.name, where="creation_fees.on_demand", minimum=0)
        assert fee is not None and one_time is not None
        return fee, one_time

    @staticmethod
    def _section(data: Mapping[str, Any], *path: str) -> dict[str, Any]:
        node: Any = data
        for step in path:
            node = node.get(step) if isinstance(node, dict) else None
            if not isinstance(node, dict):
                raise UnknownFieldType(f"clore: field {'.'.join(path)!r} is missing or not an object")
        return node

    def requote(self, offer: Offer, creds: Creds) -> Offer | None:
        """Read the server again with the account's fees. Return None when it is gone or no longer free."""
        try:
            wanted = int(offer.offer_id)
        except ValueError as error:
            raise ValueError(f"clore: {offer.offer_id!r} is not a Clore server ID") from error
        found = [row for row in self._marketplace(creds) if isinstance(row, dict) and row.get("id") == wanted]
        if not found:
            return None
        account_fee, account_creation = self.renter_fees(creds)
        # Quote the higher of the documented figure and the account's figure, so a
        # holder with a lower fee is never quoted less than the documented price.
        try:
            again = offer_from_row(
                found[0],
                fee_percent=max(RENTER_BASE_FEE_PERCENT, account_fee),
                creation_fee_usd=max(CREATION_FEE_USD, account_creation),
            )
        except SkipServer:
            return None
        return again if again.rentable else None

    # --- orders -----------------------------------------------------------

    def _orders(self, creds: Creds) -> list[dict[str, Any]]:
        """Read the account's active orders. Every row needs an ID, and other fields are read only where they are used."""
        # Without return_completed the list holds active orders. Expired orders are skipped by the callers.
        # https://docs.clore.ai/for-hosts/api (my_orders)
        data = self._call("GET", "my_orders", creds, retry=True)
        orders = data.get("orders")
        if not isinstance(orders, list):
            raise ResponseNotRecognized("clore: the order list answer has no 'orders' list")
        rows: list[dict[str, Any]] = []
        for row in orders:
            if not isinstance(row, dict):
                raise UnknownFieldType(f"clore: an order is a {type(row).__name__}, not an object")
            fields.integer(row, "id", backend=self.name, where="an order", minimum=1)
            rows.append(row)
        return rows

    def _record(self, run_id: str) -> dict[str, Any] | None:
        from ablit.runs import RunError, RunStore

        store = self.store or RunStore()
        try:
            return store.load(run_id)
        except RunError:
            return None

    @staticmethod
    def _created_by_this_run(row: Mapping[str, Any], sent: Mapping[str, Any]) -> bool:
        """True when an order has the server, image and creation time that the journal recorded."""
        server, created = row.get("si"), row.get("ct")
        if type(server) is not int or type(created) is not int or row.get("spot") is True:
            return False
        if server != sent["server_id"] or created < sent["sent_at"] - CLOCK_SLACK_SECONDS:
            return False
        if row.get("image") != sent["image"]:
            raise ResponseNotRecognized(
                f"clore: order {row['id']} on server {server} was created at the time of this run's create, "
                "but its image text differs from the one sent, so ablit will not adopt it. "
                "Review the order in the Clore console"
            )
        return True

    def _run_orders(self, record: Mapping[str, Any] | None, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Pick the orders that belong to a run: the saved IDs, and the order found by the create journal."""
        if record is None:
            return []
        known = {str(i) for i in record.get("resource_ids") or []}
        sent = record.get("clore_create")
        mine = [row for row in rows if str(row["id"]) in known]
        # The journal match is a way to find an order after a lost create answer. Once the
        # run has a saved order ID, the ID is enough, and a second look-alike order is left alone.
        if isinstance(sent, dict) and not mine:
            extra = [row for row in rows if str(row["id"]) not in known and self._created_by_this_run(row, sent)]
            if len(extra) > 1:
                raise ResponseNotRecognized(
                    "clore: more than one order matches this run's create (server, image and time). "
                    "Review the orders in the Clore console"
                )
            mine += extra
        return mine

    def reconcile(self, run_id: str, creds: Creds) -> Inventory:
        record = self._record(run_id)
        if record is None or not (record.get("create_sent") or record.get("resource_ids")):
            return Inventory(run_id, self.name, [])
        rows = self._run_orders(record, self._orders(creds))
        return self._inventory(run_id, rows)

    def _allocation(self, run_id: str, resource_ids: list[str], offer: Offer) -> Allocation:
        return Allocation(run_id, self.name, resource_ids, offer.hourly_all_in, host_id=offer.host_id)

    def _prepare_create(self, offer: Offer, boot: BootSpec, run_id: str) -> tuple[int, str]:
        key = boot.ssh_public_key
        if key is None or len(key) > SSH_KEY_MAX_CHARS or not SSH_KEY_PATTERN.match(key):
            raise ValueError("clore: the SSH public key is missing, too long, or has characters create_order refuses")
        if offer.sticker_hourly is None:
            raise ValueError("clore: the offer has no listed price, so it cannot be rented")
        return int(offer.offer_id), key

    def _journal_create(self, record: dict[str, Any], offer: Offer, boot: BootSpec, prepared: tuple[int, str]) -> None:
        record["clore_create"] = {"server_id": prepared[0], "image": boot.image, "sent_at": int(time.time())}

    def _send_create(self, offer: Offer, boot: BootSpec, run_id: str, creds: Creds, prepared: tuple[int, str]) -> Any:
        server, key = prepared
        assert offer.sticker_hourly is not None
        body = {
            "currency": CURRENCY,
            "image": boot.image,
            "renting_server": server,
            "type": "on-demand",
            # Only the SSH port is forwarded. The engine is reached through an SSH tunnel.
            "ports": {"22": "tcp"},
            "ssh_key": key,
            # Clore's own entrypoint starts an SSH server and runs a start script.
            "autossh_entrypoint": True,
            "command": boot.onstart,
            # The order does not start if the host raised the price after the quote.
            "required_price": round(offer.sticker_hourly * HOURS_PER_DAY, 8),
        }
        # https://docs.clore.ai/for-hosts/api (create_order)
        return self._call("POST", "create_order", creds, body=body)

    def _create_refused(self, error: RequestRejected) -> bool:
        return (
            isinstance(error, CredentialsRejected)
            or (isinstance(error, CloreRejected) and error.definitive)
            or error.status in (400, 401, 403, 404)
        )

    def _read_created(self, data: Any, record: dict[str, Any], creds: Creds) -> list[str]:
        # The reference documents only the code field in the answer, so the order is found in the list.
        return [str(self._wait_for_order(record, creds)["id"])]

    def _wait_for_order(self, record: Mapping[str, Any], creds: Creds, attempts: int = 3) -> dict[str, Any]:
        for _ in range(attempts):
            rows = self._run_orders(record, self._orders(creds))
            if len(rows) == 1:
                return rows[0]
            if len(rows) > 1:
                raise ResponseNotRecognized("clore: more than one order matches this run's create")
        raise CreateUncertain("The order is not in the order list yet. Re-run up or down; no second create is allowed.")

    def observe(self, alloc: Allocation, creds: Creds) -> Observation:
        row = self._one([r for r in self._orders(creds) if str(r["id"]) in alloc.resource_ids], "order")
        if row is None or row.get("expired") is True:
            return self._absent()
        where = f"order {row['id']}"
        currency = fields.text(row, "currency", backend=self.name, where=where)
        if currency != CURRENCY:
            raise UnknownUnit(f"clore: {where} is billed in {currency!r}, but the driver prices only {CURRENCY}")
        price = fields.number(row, "price", backend=self.name, where=where, minimum=0)
        fee = fields.number(row, "fee", backend=self.name, where=where, minimum=0)
        spend = fields.number(row, "spend", backend=self.name, where=where, nullable=True, minimum=0)
        assert price is not None and fee is not None
        # The order price is per day, and the fee is a percentage of it.
        rate = price * (1.0 + fee / 100.0) / HOURS_PER_DAY
        host, port = self._ssh_endpoint(row, where)
        server = row.get("si")
        host_id = str(server) if type(server) is int else alloc.host_id
        found = Allocation(alloc.run_id, self.name, alloc.resource_ids, rate, host, port, host_id)
        # The order list has no status field. An order counts as running once Clore lists
        # a public host and a forwarded SSH port for it. Until then it is starting.
        state = "running" if host and port else "starting"
        return Observation(time.time(), state, spend_usd=spend, hourly_rate=rate, allocation=found)

    @staticmethod
    def _ssh_endpoint(row: Mapping[str, Any], where: str) -> tuple[str | None, int | None]:
        """Read the public host and the port forwarded to the container's SSH port 22."""
        cluster, ports = row.get("pub_cluster"), row.get("tcp_ports")
        if cluster is None or ports is None:
            return None, None
        if not isinstance(cluster, list) or not isinstance(ports, list):
            raise UnknownFieldType(f"clore: 'pub_cluster' and 'tcp_ports' in {where} are not lists")
        host = None
        for item in cluster:
            if not isinstance(item, str) or not _HOST_PATTERN.match(item):
                raise UnknownFieldType(f"clore: 'pub_cluster' in {where} holds a value that is not a host name")
            host = host or item
        port = None
        for item in ports:
            # Each entry reads "<container port>:<public port>", for example "22:10000".
            match = re.fullmatch(r"(\d{1,5}):(\d{1,5})", item) if isinstance(item, str) else None
            if match is None:
                raise UnknownFieldType(f"clore: 'tcp_ports' in {where} holds an entry that is not 'container:public'")
            if match.group(1) == "22":
                public = int(match.group(2))
                if not 1 <= public <= 65535:
                    raise UnknownFieldType(f"clore: invalid SSH port in {where}")
                port = public
        return (host, port) if host and port else (None, None)

    def _terminate(self, resource_id: str, creds: Creds) -> None:
        # An order that already ended answers order_not_active, which is a finished cancel.
        # https://docs.clore.ai/for-hosts/api (cancel_order)
        # https://docs.clore.ai/dev/reference/error-codes
        self._call("POST", "cancel_order", creds, body={"id": int(resource_id)}, retry=True,
                   tolerate=("order_not_active",))

    def _still_billable(self, inv: Inventory, creds: Creds) -> list[str]:
        rows = self._orders(creds)
        record = self._record(inv.run_id)
        mine = {str(r["id"]) for r in self._run_orders(record, rows)} | set(inv.resource_ids)
        # Absence from the active list, or an expired flag, is the evidence. A cancel
        # acknowledgement alone is not. The list gives no final cost the driver can trust.
        return [str(r["id"]) for r in rows if str(r["id"]) in mine and r.get("expired") is not True]

    def watchdog_credential(self, alloc: Allocation, creds: Creds) -> WatchdogCred | None:
        # Clore documents no API key limited to one order or one server. The key that
        # can cancel an order can also spend the whole account, so it never goes on the
        # rented machine. Without a credential the watchdog can only stop the engine.
        return None

    # --- account ----------------------------------------------------------

    def test_credentials(self, creds: Creds) -> None:
        """One read-only call. An invalid key answers HTTP 401 with code 3."""
        # https://docs.clore.ai/for-hosts/api (wallets)
        data = self._call("GET", "wallets", creds, retry=True)
        if not isinstance(data.get("wallets"), list):
            raise ResponseNotRecognized("clore: the wallets answer has no 'wallets' list")
