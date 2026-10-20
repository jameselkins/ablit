"""Hand-written marketplace responses for tests.

Every ID and price here is invented. The shapes follow the fields the drivers
read, and no real marketplace data is copied into this repository.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx

VAST_URL = "https://console.vast.ai/api/v0/bundles/"
RUNPOD_URL = "https://api.runpod.io/graphql"

MONTH_HOURS = 720.0


def vast_row(offer_id: int = 9001, base: float = 0.50, allocated: float = 575, **overrides: Any) -> dict[str, Any]:
    """One invented Vast row. The all-in price is the GPU-only price plus the allocated disk."""
    storage_per_gb_month = overrides.pop("storage_cost", 0.1)
    storage_total = storage_per_gb_month * allocated / MONTH_HOURS
    row: dict[str, Any] = {
        "id": offer_id,
        "ask_contract_id": offer_id,
        "num_gpus": 1,
        "gpu_name": "RTX 4090",
        "gpu_arch": "nvidia",
        "gpu_ram": 24564,
        "gpu_total_ram": 24564,
        "gpu_frac": 1.0,
        "cpu_ram": 128000,
        "disk_space": 900.5,
        "dph_base": base,
        "dph_total": base + storage_total,
        "storage_cost": storage_per_gb_month,
        "storage_total_cost": storage_total,
        "inet_down": 1200.0,
        "inet_down_cost": 0.01,
        "internet_down_cost_per_tb": 10.24,
        "compute_cap": 890,
        "rentable": True,
        "is_bid": False,
        "verification": "verified",
        "reliability": 0.99,
        "duration": 500 * 3600.0,
        "geolocation": "Texas, US",
    }
    row.update(overrides)
    return row


def vast_response(rows: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"offers": rows, "truncated": False})


class FakeVast:
    """A stand-in for the bundles endpoint that applies the filters the drivers rely on.

    It returns at most 64 rows, ordered by the GPU-only price, and it applies a
    `dph_base` range filter. Set `ignore_cursor` to make it ignore that filter,
    or `reject_cursor` to make it answer later pages with HTTP 400.
    """

    def __init__(self, rows: list[dict[str, Any]], ignore_cursor: bool = False, reject_cursor: bool = False) -> None:
        self.rows = rows
        self.ignore_cursor = ignore_cursor
        self.reject_cursor = reject_cursor
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        self.headers.append(request.headers)
        rows = list(self.rows)
        base_filter = body.get("dph_base")
        if base_filter and "gte" in base_filter:
            if self.reject_cursor:
                return httpx.Response(400, json={"error": "bad_request", "msg": "dph_base: not allowed"})
            if not self.ignore_cursor:
                rows = [r for r in rows if r["dph_base"] >= base_filter["gte"]]
        if body.get("order") == [["dph_base", "asc"]]:
            rows.sort(key=lambda r: (r["dph_base"], r["id"]))
        return vast_response(rows[:64])


def runpod_type(
    type_id: str = "NVIDIA GeForce RTX 4090",
    name: str = "RTX 4090",
    vram: float = 24,
    price: float | None = 0.50,
    stock: str | None = "High",
    pod_ram: float | None = 100,
) -> dict[str, Any]:
    if price is None:
        lowest = {"uninterruptablePrice": None, "stockStatus": None, "minMemory": None}
    else:
        lowest = {"uninterruptablePrice": price, "stockStatus": stock, "minMemory": pod_ram}
    return {"id": type_id, "displayName": name, "memoryInGb": vram, "lowestPrice": lowest}


def runpod_response(types: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"data": {"gpuTypes": types}})


class FakeRunPod:
    """A stand-in for the GraphQL endpoint with one answer for each price tier."""

    def __init__(self, any_cloud: list[dict[str, Any]], secure: list[dict[str, Any]] | None = None) -> None:
        self.any_cloud = any_cloud
        self.secure = any_cloud if secure is None else secure
        self.queries: list[str] = []
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        self.queries.append(query)
        self.headers.append(request.headers)
        return runpod_response(self.secure if "secureCloud: true" in query else self.any_cloud)


def install(router: Any, vast: Callable | None = None, runpod: Callable | None = None) -> None:
    """Route the two endpoints on a respx router to the given callables."""
    if vast is not None:
        router.post(VAST_URL).mock(side_effect=vast)
    if runpod is not None:
        router.post(RUNPOD_URL).mock(side_effect=runpod)
