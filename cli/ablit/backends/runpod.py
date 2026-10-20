"""RunPod driver. See backends/runpod.md at the top of the repository.

Search request: GraphQL `gpuTypes { id displayName memoryInGb lowestPrice(input:
{gpuCount, minMemoryInGb}) { uninterruptablePrice stockStatus minMemory } }`
against https://api.runpod.io/graphql. With `secureCloud` left out, the price
is the lowest one RunPod offers for that GPU type and count, which includes
community prices. A second query with `secureCloud: true` gives the Secure
Cloud price. `uninterruptablePrice` is the total for all GPUs.

TODO(step 4): write create, destroy and the speed check.
"""

from __future__ import annotations

import json
import math
from typing import Any

import httpx

from ablit import __version__
from ablit.backends import fields
from ablit.backends.base import Capabilities, Creds, OfferSearch, UnwrittenBackend
from ablit.backends.errors import (
    CredentialsRejected,
    RequestRejected,
    ResponseNotRecognized,
    UnknownFieldType,
)
from ablit.backends.runpod_gpus import compute_capability_for
from ablit.select.types import HardwareRequest, Offer, PlanOptions

GRAPHQL_URL = "https://api.runpod.io/graphql"

# Volume disk price: $0.10 per GB per month, from https://docs.runpod.io/pods/pricing
# (checked 2026-10-20). A month is counted as 730 hours.
VOLUME_DISK_USD_PER_GB_MONTH = 0.10
HOURS_PER_MONTH = 730.0

# The two price tiers. "any" leaves `secureCloud` out of the query, and "secure" sets it to true.
TIER_ANY = "any"
TIER_SECURE = "secure"

VENDOR_BY_ID_PREFIX = (("NVIDIA", "nvidia"), ("Tesla", "nvidia"), ("AMD", "amd"))


def volume_disk_hourly(disk_gb: float) -> float:
    """Return the hourly price of a volume disk of `disk_gb` gigabytes."""
    return disk_gb * VOLUME_DISK_USD_PER_GB_MONTH / HOURS_PER_MONTH


def _whole_gb(value: float) -> int:
    return int(math.ceil(value - 1e-9))


def build_query(
    gpu_count: int, min_memory_gb: int, *, secure: bool, gpu_type_id: str | None = None
) -> str:
    """Return the GraphQL query text. It is a query, never a mutation."""
    price_args = f"gpuCount: {int(gpu_count)}, minMemoryInGb: {int(min_memory_gb)}"
    if secure:
        price_args += ", secureCloud: true"
    selector = f"(input: {{id: {json.dumps(gpu_type_id)}}})" if gpu_type_id is not None else ""
    return (
        f"query {{ gpuTypes{selector} {{ id displayName memoryInGb "
        f"lowestPrice(input: {{{price_args}}}) {{ uninterruptablePrice stockStatus minMemory }} }} }}"
    )


def offer_id_for(gpu_type_id: str, gpu_count: int, tier: str) -> str:
    return f"{gpu_type_id}|{gpu_count}|{tier}"


def parse_offer_id(offer_id: str) -> tuple[str, int, str]:
    parts = offer_id.rsplit("|", 2)
    if len(parts) != 3 or parts[2] not in (TIER_ANY, TIER_SECURE):
        raise ValueError(f"runpod: {offer_id!r} is not a RunPod offer ID")
    try:
        count = int(parts[1])
    except ValueError as error:
        raise ValueError(f"runpod: {offer_id!r} is not a RunPod offer ID") from error
    return parts[0], count, parts[2]


def offers_from_data(
    data: Any, *, gpu_count: int, disk_gb: int, tier: str
) -> tuple[list[Offer], int]:
    """Turn the `data` object of a response into offers.

    Returns the offers and the number of GPU types that had no price for this
    arrangement. A type with no price is not an offer, because there is nothing
    to rank.
    """
    name = "runpod"
    if not isinstance(data, dict) or not isinstance(data.get("gpuTypes"), list):
        raise ResponseNotRecognized("runpod: the response has no 'gpuTypes' list")
    offers: list[Offer] = []
    unpriced = 0
    for entry in data["gpuTypes"]:
        if not isinstance(entry, dict):
            raise UnknownFieldType(f"runpod: a GPU type is a {type(entry).__name__}, not an object")
        gpu_id = fields.text(entry, "id", backend=name, where="a GPU type")
        assert gpu_id is not None
        where = f"GPU type {gpu_id!r}"
        if "lowestPrice" not in entry:
            raise UnknownFieldType(f"runpod: field 'lowestPrice' in {where} is missing")
        lowest = entry["lowestPrice"]
        if lowest is None:
            unpriced += 1
            continue
        if not isinstance(lowest, dict):
            raise UnknownFieldType(f"runpod: field 'lowestPrice' in {where} is not an object")
        price = fields.number(lowest, "uninterruptablePrice", backend=name, where=where, nullable=True, minimum=0)
        if price is None:
            unpriced += 1
            continue
        display = fields.text(entry, "displayName", backend=name, where=where)
        vram = fields.number(entry, "memoryInGb", backend=name, where=where, minimum=1)
        stock = fields.text(lowest, "stockStatus", backend=name, where=where, nullable=True)
        pod_ram = fields.number(lowest, "minMemory", backend=name, where=where, minimum=0)
        assert display is not None and vram is not None and pod_ram is not None
        vendor = next((v for prefix, v in VENDOR_BY_ID_PREFIX if gpu_id.startswith(prefix)), None)
        if vendor is None:
            raise UnknownFieldType(f"runpod: {where} does not start with a vendor name the driver knows")
        offers.append(
            Offer(
                backend=name,
                offer_id=offer_id_for(gpu_id, gpu_count, tier),
                gpu_model=display,
                gpu_count=gpu_count,
                gpu_vram_gb_each=vram,
                # The price is the total for all GPUs. The volume disk is added once.
                hourly_all_in=price + volume_disk_hourly(disk_gb),
                host_ram_gb=pod_ram,
                disk_gb=float(disk_gb),
                gpu_vendor=vendor,
                compute_capability=compute_capability_for(gpu_id),
                sticker_hourly=price,
                bandwidth_cost_per_gb=0.0,
                download_mbps=None,
                rentable=stock is not None,
                verified=tier == TIER_SECURE,
                allocated_disk_gb=float(disk_gb),
            )
        )
    return offers, unpriced


class RunPodBackend(UnwrittenBackend):
    name = "runpod"

    def __init__(self, client: httpx.Client | None = None, url: str = GRAPHQL_URL) -> None:
        self._client = client
        self._owns_client = client is None
        self._url = url

    def close(self) -> None:
        """Close the HTTP client when this driver created it."""
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def capabilities(self) -> Capabilities:
        return Capabilities(search=True)

    def _run(self, query: str, creds: Creds | None) -> Any:
        headers = {"Content-Type": "application/json", "User-Agent": f"ablit/{__version__}"}
        if creds is not None:
            headers["Authorization"] = f"Bearer {creds.api_key}"
        if self._client is None:
            self._client = httpx.Client(timeout=httpx.Timeout(30.0))
        response = self._client.post(self._url, json={"query": query}, headers=headers)
        status = response.status_code
        if status in (401, 403):
            raise CredentialsRejected(self.name, status, "the API key was refused")
        if status == 429:
            raise RequestRejected(self.name, status, "rate limited, try again later")
        try:
            body = response.json()
        except ValueError as error:
            if status < 200 or status >= 300:
                raise RequestRejected(self.name, status, response.text[:200]) from error
            raise ResponseNotRecognized("runpod: the response is not JSON") from error
        if isinstance(body, dict) and body.get("errors"):
            first = body["errors"][0] if isinstance(body["errors"], list) and body["errors"] else body["errors"]
            message = first.get("message") if isinstance(first, dict) else str(first)
            raise RequestRejected(self.name, status, f"GraphQL error: {str(message)[:200]}")
        if status < 200 or status >= 300:
            raise RequestRejected(self.name, status, response.text[:200])
        if not isinstance(body, dict) or "data" not in body:
            raise ResponseNotRecognized("runpod: the response has no 'data' object")
        return body["data"]

    def search(
        self, req: HardwareRequest, creds: Creds | None, opts: PlanOptions | None = None
    ) -> OfferSearch:
        """Query every GPU type for the request's GPU count and host RAM, in both price tiers."""
        disk = _whole_gb(req.disk_gb)
        memory = _whole_gb(req.host_ram_gb)
        any_cloud, missing_any = offers_from_data(
            self._run(build_query(req.gpu_count, memory, secure=False), creds),
            gpu_count=req.gpu_count, disk_gb=disk, tier=TIER_ANY,
        )
        secure, missing_secure = offers_from_data(
            self._run(build_query(req.gpu_count, memory, secure=True), creds),
            gpu_count=req.gpu_count, disk_gb=disk, tier=TIER_SECURE,
        )
        # When both queries give the same price for a GPU type, the cheapest offer is a Secure Cloud
        # one, so the community-or-secure entry would only repeat it.
        secure_prices = {
            parse_offer_id(o.offer_id)[0]: (o.hourly_all_in, o.rentable) for o in secure
        }
        offers = [
            o
            for o in any_cloud
            if secure_prices.get(parse_offer_id(o.offer_id)[0]) != (o.hourly_all_in, o.rentable)
        ] + secure
        gpus = f"{req.gpu_count} GPU" if req.gpu_count == 1 else f"{req.gpu_count} GPUs"
        note = (
            f"GPU types with no price for {gpus} and {memory} GB of RAM: "
            f"{missing_any} in any cloud, {missing_secure} in Secure Cloud"
        )
        # RunPod gives one price per GPU type and count, so the search sees everything RunPod exposes.
        return OfferSearch(backend=self.name, offers=offers, complete=True, note=note)

    def requote(self, offer: Offer, creds: Creds) -> Offer | None:
        """Run the query again for the offer's GPU type and count. Return None when it has no price."""
        if offer.allocated_disk_gb is None:
            raise ValueError("runpod: the offer has no allocated disk, so it cannot be quoted again")
        gpu_id, count, tier = parse_offer_id(offer.offer_id)
        query = build_query(count, _whole_gb(offer.host_ram_gb), secure=tier == TIER_SECURE, gpu_type_id=gpu_id)
        offers, _ = offers_from_data(
            self._run(query, creds), gpu_count=count, disk_gb=_whole_gb(offer.allocated_disk_gb), tier=tier
        )
        for found in offers:
            if found.offer_id == offer.offer_id:
                return found
        return None
