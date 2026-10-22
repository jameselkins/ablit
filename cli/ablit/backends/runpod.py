"""RunPod driver. See backends/runpod.md at the top of the repository.

Search request: GraphQL `gpuTypes { id displayName memoryInGb lowestPrice(input:
{gpuCount, minMemoryInGb}) { uninterruptablePrice stockStatus minMemory } }`
against https://api.runpod.io/graphql. With `secureCloud` left out, the price
is the lowest one RunPod offers for that GPU type and count, which includes
community prices. A second query with `secureCloud: true` gives the Secure
Cloud price. `uninterruptablePrice` is the total for all GPUs.

Rental requests use RunPod REST API v2 at https://api.runpod.io/v2, because
v1 is deprecated and will be retired on 2026-11-15
(https://docs.runpod.io/api-reference-v2/migrate-from-v1). The operations are
createPod, listPods, getPod and deletePod, from the official OpenAPI schema at
https://api.runpod.io/v2/openapi.json. backends/runpod.md lists each fact with
its source and marks what is UNVERIFIED.

Renting is untested on a rented machine. The lifecycle has synthetic tests only.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from typing import Any

import httpx

from ablit import __version__
from ablit.backends import fields
from ablit.backends.base import (
    Allocation,
    BillingEvidence,
    BootSpec,
    Capabilities,
    Creds,
    Inventory,
    Observation,
    OfferSearch,
    TerminationResult,
    UnwrittenBackend,
    WatchdogCred,
)
from ablit.backends.errors import (
    CreateUncertain,
    CredentialsRejected,
    RequestRejected,
    ResponseNotRecognized,
    UnknownFieldType,
    UnknownUnit,
)
from ablit.backends.runpod_gpus import compute_capability_for
from ablit.select.types import HardwareRequest, Offer, PlanOptions

GRAPHQL_URL = "https://api.runpod.io/graphql"

# REST API v2. https://docs.runpod.io/api-reference-v2/overview
REST_ROOT = "https://api.runpod.io"
PODS_PATH = "/v2/pods"

# Pods that are still billing. RunPod documents `cost` as 0.0 when a pod is EXITED or
# TERMINATED, but a stopped pod still bills for its volume disk, so ablit treats every
# listed pod that is not TERMINATED as billing.
# https://docs.runpod.io/pods/pricing
POD_STATUSES = ("PROVISIONING", "STARTING", "RUNNING", "EXITED", "ERROR", "TERMINATED")

# The public key travels in this environment variable, which RunPod's own images and
# the start script below read. https://docs.runpod.io/pods/configuration/use-ssh
PUBLIC_KEY_VARIABLE = "PUBLIC_KEY"
SSH_KEY_PATTERN = re.compile(r"^[A-Za-z0-9+/=@._ -]{1,1024}$")

# RunPod does not add an SSH server to a custom image the way Vast does, so the start
# script writes the run key, installs sshd when it is missing and runs it.
# The pattern follows the custom template steps at https://docs.runpod.io/pods/configuration/use-ssh.
# It is UNVERIFIED on a rented pod.
RUNPOD_START = (
    "umask 077; mkdir -p /ablit/logs /ablit/stages /root/.ssh /run/sshd; "
    'printf "%s\\n" "$PUBLIC_KEY" > /root/.ssh/authorized_keys; '
    "chown -R root:root /root/.ssh; chmod 700 /root /root/.ssh; chmod 600 /root/.ssh/authorized_keys; "
    "if [ ! -x /usr/sbin/sshd ]; then "
    "(apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server) "
    ">/ablit/logs/sshd-install.log 2>&1; fi; "
    "ssh-keygen -A >/dev/null 2>&1; "
    "exec /usr/sbin/sshd -D -e -o PasswordAuthentication=no -o PermitRootLogin=prohibit-password"
)

# The price is the GPU price plus the container disk. RunPod charges the container disk at
# the same $0.10 per GB per month as a volume disk while the pod runs, so the quote does not
# change when ablit asks for a container disk and no volume. https://docs.runpod.io/pods/pricing
# The quote check allows a small margin before it calls a created pod dearer than the quote.
COST_MARGIN = 1.05
COST_MARGIN_ABS = 0.01

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
                # The v2 create request cannot ask for a machine with a public IP. See backends/runpod.md.
                ssh_reachable=tier == TIER_SECURE,
                allocated_disk_gb=float(disk_gb),
            )
        )
    return offers, unpriced


class RunPodBackend(UnwrittenBackend):
    name = "runpod"

    inventory_interval = 0.5
    _sleep = staticmethod(time.sleep)

    def __init__(
        self, client: httpx.Client | None = None, url: str = GRAPHQL_URL, *, rest_root: str = REST_ROOT, store=None
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._url = url
        self._rest_root = rest_root.rstrip("/")
        self.store = store
        self.denied_host_ids: set[str] = set()

    def close(self) -> None:
        """Close the HTTP client when this driver created it."""
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def capabilities(self) -> Capabilities:
        # Search works against live data. Renting stays False until a paid test shows it works.
        # RunPod documents no key limited to one pod, so scoped_watchdog_credential stays False.
        # RunPod's v2 create request has no deadline field, so marketplace_deadline stays False.
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

    # --- rental: REST API v2 ------------------------------------------------

    def _request(
        self, method: str, path: str, creds: Creds, *, body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None, missing_ok: bool = False,
    ) -> Any:
        """Send one REST request. Reads and deletes retry on 429 and 5xx. A create is never resent."""
        if self._client is None:
            self._client = httpx.Client(timeout=httpx.Timeout(30.0))
        headers = {"Authorization": f"Bearer {creds.api_key}", "User-Agent": f"ablit/{__version__}"}
        attempts = 5 if method in ("GET", "DELETE") else 1
        response: httpx.Response | None = None
        for attempt in range(attempts):
            try:
                response = self._client.request(
                    method, self._rest_root + path, headers=headers, json=body, params=params,
                    follow_redirects=False,
                )
            except httpx.TransportError:
                if attempt + 1 >= attempts:
                    raise
                self._sleep(min(2.0 * 2 ** attempt, 30.0))
                continue
            if response.status_code != 429 and response.status_code < 500:
                break
            if attempt + 1 < attempts:
                # 429 answers carry a Retry-After header. https://docs.runpod.io/api-reference-v2/migrate-from-v1
                wait = response.headers.get("Retry-After", "")
                self._sleep(min(float(wait), 60.0) if wait.isdigit() else min(2.0 * 2 ** attempt, 30.0))
        assert response is not None
        status = response.status_code
        if response.is_redirect:
            raise RequestRejected(self.name, status, "the API redirected the request; ablit does not follow it")
        if status == 404 and missing_ok:
            return None
        if status in (401, 403):
            raise CredentialsRejected(self.name, status, "the API key or its permissions were refused")
        if not response.is_success:
            raise RequestRejected(self.name, status, self._problem_detail(response))
        if status == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as error:
            raise ResponseNotRecognized("runpod: expected JSON") from error

    @staticmethod
    def _problem_detail(response: httpx.Response) -> str:
        """Read the `detail` of an RFC 9457 error body. Errors have title, status and detail."""
        try:
            data = response.json()
        except ValueError:
            return "the request failed"
        detail = data.get("detail") if isinstance(data, dict) else None
        return detail[:200] if isinstance(detail, str) and detail else "the request failed"

    @staticmethod
    def label(run_id: str) -> str:
        """The pod name. The create request has no tag field, so the name is how a run finds its pod."""
        return f"ablit-{run_id}"

    def test_credentials(self, creds: Creds) -> None:
        """One read-only call. It needs the pods:read scope, so a key without it is refused."""
        # https://api.runpod.io/v2/openapi.json (listPods)
        data = self._request("GET", PODS_PATH, creds, params={"limit": 1})
        if not isinstance(data, dict) or not isinstance(data.get("pods"), list):
            raise ResponseNotRecognized("runpod: the pod list answer has no 'pods' list")

    def pods(self, creds: Creds) -> list[dict[str, Any]]:
        """Read every page of the account's pods. A partial list cannot prove that billing stopped."""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        params: dict[str, Any] = {"limit": 1000}
        while True:
            # https://api.runpod.io/v2/openapi.json (listPods, cursor pagination)
            data = self._request("GET", PODS_PATH, creds, params=params)
            if not isinstance(data, dict) or not isinstance(data.get("pods"), list):
                raise ResponseNotRecognized("runpod: the pod list answer has no 'pods' list")
            page = data.get("pagination")
            if not isinstance(page, dict) or "nextCursor" not in page or not isinstance(page.get("hasNextPage"), bool):
                raise ResponseNotRecognized("runpod: the pod list answer has no pagination block")
            for row in data["pods"]:
                out.append(self._pod(row))
            cursor = page["nextCursor"]
            if not page["hasNextPage"]:
                return out
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ResponseNotRecognized("runpod: invalid or repeated pagination cursor")
            seen.add(cursor)
            params["cursor"] = cursor

    @staticmethod
    def _pod(row: Any) -> dict[str, Any]:
        if not isinstance(row, dict):
            raise UnknownFieldType(f"runpod: a pod is a {type(row).__name__}, not an object")
        fields.text(row, "id", backend="runpod", where="a pod")
        name = row.get("name")
        if name is not None and not isinstance(name, str):
            raise UnknownFieldType("runpod: a pod name is not text")
        status = fields.text(row, "status", backend="runpod", where=f"pod {row['id']}")
        if status not in POD_STATUSES:
            raise UnknownFieldType(f"runpod: pod {row['id']} has status {status!r}, which the driver does not know")
        return row

    # --- create -------------------------------------------------------------

    def create(self, offer: Offer, boot: BootSpec, run_id: str, creds: Creds) -> Allocation:
        """Journal the attempt before sending it. A run gets at most one create."""
        from ablit.runs import RunStore

        store = self.store or RunStore()
        with store.lock(run_id + ".create"):
            record = store.load(run_id)
            if record.get("create_sent"):
                found = self.reconcile(run_id, creds)
                if len(found.resource_ids) == 1:
                    return Allocation(run_id, self.name, found.resource_ids, offer.hourly_all_in)
                raise CreateUncertain("Create was already sent. Re-run down to reconcile; no second create is allowed.")
            if "@sha256:" not in boot.image:
                raise ResponseNotRecognized("runpod: an image digest is required")
            if boot.env:
                raise ResponseNotRecognized("runpod: credentials and engine config must travel over SSH")
            key = boot.ssh_public_key
            if key is None or not SSH_KEY_PATTERN.match(key) or "\n" in key:
                raise ValueError("runpod: the SSH public key is missing, too long, or has characters the driver refuses")
            gpu_id, count, tier = parse_offer_id(offer.offer_id)
            body = {
                "name": self.label(run_id),
                "image": boot.image,
                # The offer ID names one GPU type and count. The create call places that GPU type only.
                "gpu": {"id": gpu_id, "count": count, "minRamPerGpu": max(1, int(offer.host_ram_gb // count))},
                # An offer that is not from the Secure Cloud query beat the Secure Cloud price, so it is a community offer.
                "cloud": "SECURE" if tier == TIER_SECURE else "COMMUNITY",
                # Container disk, with no persistent volume. The quote prices the same GB at the same rate.
                "disk": boot.disk_gb,
                "ports": ["22/tcp"],
                "env": {PUBLIC_KEY_VARIABLE: key},
                "entrypoint": ["/bin/bash", "-c"],
                "cmd": [boot.onstart],
            }
            record["create_sent"] = True
            record["started_at"] = time.time()
            store.save(record)
            try:
                # https://api.runpod.io/v2/openapi.json (createPod)
                data = self._request("POST", PODS_PATH, creds, body=body)
            except RequestRejected as error:
                # 400 (rule or no capacity), 401, 402, 403, 404 and 422 prove nothing was created.
                # 429 and 5xx do not prove it.
                if error.status in (400, 401, 402, 403, 404, 422):
                    record["create_rejected"] = True
                    store.save(record)
                raise
            if os.environ.get("ABLIT_FAULT") == "drop-create-response":
                raise CreateUncertain("The create response was dropped by ABLIT_FAULT. Re-run up or down to find the pod name.")
            pod = self._pod(data)
            record["resource_ids"] = [str(pod["id"])]
            store.save(record)
            self.check_against_quote(pod, offer, boot)
            return Allocation(run_id, self.name, [str(pod["id"])], offer.hourly_all_in)

    def check_against_quote(self, pod: dict[str, Any], offer: Offer, boot: BootSpec) -> None:
        """Refuse a created pod that differs from the approved quote. The caller then destroys the run."""
        gpu_id, count, tier = parse_offer_id(offer.offer_id)
        where = f"pod {pod['id']}"
        problems: list[str] = []
        gpu = pod.get("gpu")
        if not isinstance(gpu, dict):
            raise UnknownFieldType(f"runpod: field 'gpu' in {where} is not an object")
        if gpu.get("id") != gpu_id or gpu.get("count") != count:
            problems.append("the GPU type or count")
        memory = fields.number(gpu, "memory", backend=self.name, where=where, minimum=0)
        assert memory is not None
        if memory + 1e-6 < offer.host_ram_gb * 0.99:
            problems.append(f"the RAM ({memory:g} GB, quoted {offer.host_ram_gb:g} GB)")
        disk = fields.number(pod, "disk", backend=self.name, where=where, minimum=0)
        if disk is not None and disk + 1e-6 < boot.disk_gb:
            problems.append(f"the container disk ({disk:g} GB, quoted {boot.disk_gb} GB)")
        cloud = fields.text(pod, "cloud", backend=self.name, where=where)
        if tier == TIER_SECURE and cloud != "SECURE":
            problems.append("the cloud (the quote was Secure Cloud)")
        cost = fields.number(pod, "cost", backend=self.name, where=where, nullable=True, minimum=0)
        if cost is not None and cost > offer.hourly_all_in * COST_MARGIN + COST_MARGIN_ABS:
            problems.append(f"the hourly cost (${cost:.4f}, quoted ${offer.hourly_all_in:.4f})")
        if problems:
            raise ResponseNotRecognized(f"runpod: the created {where} differs from the approved quote in " + ", ".join(problems))

    # --- find, watch, destroy -----------------------------------------------

    def reconcile(self, run_id: str, creds: Creds) -> Inventory:
        """Find the run's pods by name. The pod name is the only tag a create request can carry."""
        rows = [r for r in self.pods(creds) if r.get("name") == self.label(run_id) and r["status"] != "TERMINATED"]
        return Inventory(run_id, self.name, [str(r["id"]) for r in rows])

    def observe(self, alloc: Allocation, creds: Creds) -> Observation:
        rows = [r for r in self.pods(creds) if str(r["id"]) in alloc.resource_ids]
        rows = [r for r in rows if r["status"] != "TERMINATED"]
        if not rows:
            return Observation(time.time(), "absent")
        if len(rows) != 1:
            raise ResponseNotRecognized("runpod: expected exactly one pod")
        row = rows[0]
        where = f"pod {row['id']}"
        status = row["status"]
        cost = fields.number(row, "cost", backend=self.name, where=where, nullable=True, minimum=0)
        host, port = self._ssh_endpoint(row, where)
        # The Pod schema lists a status of PROVISIONING, STARTING or RUNNING while the pod
        # is coming up or up. A pod that is RUNNING with no direct SSH mapping yet is still starting.
        # https://api.runpod.io/v2/openapi.json (PodStatus, PodSsh)
        if status == "RUNNING" and not (host and port):
            state = "starting"
        else:
            state = {"PROVISIONING": "starting", "STARTING": "starting", "RUNNING": "running",
                     "EXITED": "exited", "ERROR": "exited"}[status]
        rate = cost if cost is not None else alloc.hourly_rate
        found = Allocation(alloc.run_id, self.name, alloc.resource_ids, rate, host, port)
        return Observation(time.time(), state, hourly_rate=cost, allocation=found)

    @staticmethod
    def _ssh_endpoint(row: dict[str, Any], where: str) -> tuple[str | None, int | None]:
        """Read the direct SSH host and port. The proxy cannot forward ports, so it is never used."""
        ssh = row.get("ssh")
        if not isinstance(ssh, dict):
            raise UnknownFieldType(f"runpod: field 'ssh' in {where} is not an object")
        direct = ssh.get("direct")
        if direct is None:
            return None, None
        if not isinstance(direct, dict):
            raise UnknownFieldType(f"runpod: field 'ssh.direct' in {where} is not an object")
        host = fields.text(direct, "host", backend="runpod", where=f"{where} ssh.direct")
        port = fields.integer(direct, "port", backend="runpod", where=f"{where} ssh.direct", minimum=1)
        if port > 65535 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host or ""):
            raise UnknownFieldType(f"runpod: invalid SSH host or port in {where}")
        return host, port

    def destroy(self, inv: Inventory, creds: Creds) -> TerminationResult:
        """Terminate each pod. Terminate deletes the pod and its disks. A stopped pod still bills for its volume."""
        terminated = []
        for pod in inv.resource_ids:
            # https://api.runpod.io/v2/openapi.json (deletePod). 204 means deleted, 404 means already gone.
            self._request("DELETE", f"{PODS_PATH}/{self._pod_path(pod)}", creds, missing_ok=True)
            terminated.append(pod)
        return TerminationResult(True, terminated)

    @staticmethod
    def _pod_path(pod: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", pod):
            raise ValueError(f"runpod: {pod!r} is not a pod ID")
        return pod

    def verify_billing_stopped(self, inv: Inventory, creds: Creds) -> BillingEvidence:
        # A pod that is absent from the list, or listed as TERMINATED, no longer bills. An EXITED pod
        # still bills for its volume disk, so it counts as billing. https://docs.runpod.io/pods/pricing
        rows = self.pods(creds)
        remaining = [
            str(r["id"]) for r in rows
            if r["status"] != "TERMINATED" and (str(r["id"]) in inv.resource_ids or r.get("name") == self.label(inv.run_id))
        ]
        return BillingEvidence(time.time(), remaining)

    def watchdog_credential(self, alloc: Allocation, creds: Creds) -> WatchdogCred | None:
        # RunPod documents no API key limited to one pod. Restricted keys are set per resource family
        # (the pods:write scope covers every pod), and the API has no call that creates a key.
        # The key that can delete a pod can also delete every other pod, so it never goes on the machine.
        # RunPod puts its own "pod-scoped" key in the pod environment as RUNPOD_API_KEY. What it may do is not documented,
        # so the watchdog only tries it, and the local check carries the cap.
        # https://docs.runpod.io/get-started/api-keys  https://docs.runpod.io/pods/templates/environment-variables
        return None
