"""Lium driver. See backends/lium.md at the top of the repository.

Every request, field and unit below comes from Lium's public documentation
(https://docs.lium.io) or its OpenAPI schema (https://lium.io/api/openapi.json),
read on 2026-10-23. The driver is written from those pages only. backends/lium.md
lists each fact with its source and marks what is UNVERIFIED. Four facts shape the code.

1. The public nodes feed lists every rentable node with no key, so `plan` searches
   Lium keyless. Prices are US dollars per GPU per hour, billed per second, and the
   documentation names no other fee. A node rents by whole GPUs, from
   `min_rentable_gpu_count` up to the node's GPU count, so the driver builds one
   offer for the GPU count the request needs.
2. A rental needs a template, which holds the image, so each run first creates a
   private one-time template with the image digest. Then one rent call names the
   template, the pod name and the SSH key. The pod name carries the run label.
3. The rent call takes `termination_hours`, so Lium deletes the pod itself. The
   driver sets it from the run's spend cap. It is a whole number of hours, at least 1.
4. A key made with the user's API key cannot create another key, and keys with
   budgets are made from a signed-in session only, so `watchdog_credential` returns
   None and the machine cannot destroy its own pod.

The lifecycle has synthetic tests and one paid round trip on 2026-10-26
(catalog/boot-tests/llama31-8b-abliterated-q4km-lium-2026-10-26.md).
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
from ablit.backends.errors import (
    CredentialsRejected,
    RequestRejected,
    ResponseNotRecognized,
    UnknownFieldType,
    UnknownUnit,
)
from ablit.backends.lium_gpus import compute_capability_for
from ablit.backends.rental import RentalBackend
from ablit.backends.transport import backoff, refuse_redirect, send_with_retry
from ablit.select.types import HardwareRequest, Offer, PlanOptions

API_ROOT = "https://lium.io/api"
# The public feed needs no key. https://docs.lium.io/developers/public-nodes-feed
NODES_PATH = "/public/v1/nodes"

# Pod statuses from the PodStatus schema in https://lium.io/api/openapi.json.
POD_STATUSES = (
    "PENDING", "RUNNING", "STOPPED", "FAILED", "DELETING", "STOP_PENDING", "START_PENDING",
    "CREATION_FAILED", "BROKEN", "REBOOT_PENDING", "REBOOT_FAILED",
)
# How each status reads to the run loop. A BROKEN pod was closed by its provider.
# https://docs.lium.io/pod-users/broken-pods
STATE_BY_STATUS = {
    "PENDING": "starting", "START_PENDING": "starting", "REBOOT_PENDING": "starting", "RUNNING": "running",
    "STOPPED": "exited", "STOP_PENDING": "exited", "FAILED": "exited", "CREATION_FAILED": "exited",
    "REBOOT_FAILED": "exited", "DELETING": "exited", "BROKEN": "stopped_by_marketplace",
}
# Statuses that the billing page says are not charged: a pod that failed to start, a pod closed by its
# provider, and a pod being deleted, because charging stops at the delete request.
# https://docs.lium.io/pod-users/billing#when-a-pod-is-charged
NOT_BILLED = ("CREATION_FAILED", "BROKEN", "DELETING")

SSH_KEY_PATTERN = re.compile(r"^[A-Za-z0-9+/=@._ -]{1,1024}$")

# The template's start command must keep the container running.
# https://docs.lium.io/pod-users/templates#creating-a-custom-template
# Lium refuses shell metacharacters in the start command and in environment values (HTTP 400,
# "Malicious startup command detected", checked on 2026-10-23), so the command cannot add a key or
# start sshd. The rent call carries the run's public key, and ablit relies on Lium to install it.
# Whether that works on an image without sshd is UNVERIFIED.
# The engine images set their own entrypoint (llama.cpp runs /app/llama-server with no args, which
# exits at once), so the template also overrides the entrypoint. A paid rent on 2026-10-26 failed
# with "the image's default command exiting right after start" while startup_commands was
# "sleep infinity" with no entrypoint, because the image entrypoint consumed it as arguments.
# Both images are Ubuntu-based, so /bin/sleep exists.
LIUM_ENTRYPOINT = "/bin/sleep"
LIUM_START = "infinity"

# Lium accepts a termination time of 1 to 720 hours. https://lium.io/api/openapi.json (RentExecutorRequest)
MIN_TERMINATION_HOURS, MAX_TERMINATION_HOURS = 1, 720

# Error codes on a 409 that prove the rent created nothing. The idempotency codes are left out on
# purpose, because they say that an earlier call may have rented. https://docs.lium.io/developers/error-codes
DEFINITE_409_CODES = ("node_paused_by_provider", "provider_banned", "node_below_bandwidth_floor",
                      "node_rent_in_progress", "conflict", "no_executor_matches_spec")

_CODE = re.compile(r"[A-Za-z0-9_]{1,64}")
_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_SSH_COMMAND = re.compile(
    r"ssh(?: -p (\d{1,5}))? ([a-z_][a-z0-9_-]{0,31})@([A-Za-z0-9][A-Za-z0-9.-]*)(?: -p (\d{1,5}))?"
)


class SkipNode(Exception):
    """A node in the feed that is not an offer for this request. The text says why, for the search note."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LiumRejected(RequestRejected):
    """An error answer. Lium's body names a stable code, which the driver keeps."""

    def __init__(self, status: int | None, code: str | None, message: str) -> None:
        super().__init__("lium", status, f"{code}: {message}" if code else message)
        self.code = code


class TemplateNotCreated(RequestRejected):
    """The template step failed. The rent call was never sent, so nothing can be billing."""

    def __init__(self, detail: str) -> None:
        super().__init__("lium", None, detail)


def offer_id_for(node_id: str, gpu_count: int) -> str:
    return f"{node_id}|{gpu_count}"


def parse_offer_id(offer_id: str) -> tuple[str, int]:
    node, _, count = offer_id.rpartition("|")
    if not _ID.fullmatch(node) or not count.isdigit() or int(count) < 1:
        raise ValueError(f"lium: {offer_id!r} is not a Lium offer ID")
    return node, int(count)


def split_image(image: str) -> tuple[str, str]:
    """Split `repository@sha256:<hex>` into the repository and the digest."""
    repository, _, digest = image.partition("@")
    if not repository or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ResponseNotRecognized("lium: an image digest is required")
    return repository, digest


def offer_from_node(row: Any, gpu_count: int) -> Offer:
    """Turn one node of the public feed into an Offer for `gpu_count` GPUs.

    Raises SkipNode for a node that cannot be rented as that many GPUs, and a typed error for a
    shape or unit the driver does not know. Nothing is guessed.
    """
    name = "lium"
    if not isinstance(row, dict):
        raise UnknownFieldType(f"lium: a node is a {type(row).__name__}, not an object")
    node_id = fields.text(row, "id", backend=name, where="a node")
    assert node_id is not None
    where = f"node {node_id}"
    model = fields.text(row, "gpu_model", backend=name, where=where)
    total = fields.integer(row, "gpu_count", backend=name, where=where, minimum=1)
    free = fields.integer(row, "available_gpu_count", backend=name, where=where, minimum=0)
    available = fields.boolean(row, "available_now", backend=name, where=where)
    per_gpu = fields.number(row, "price_per_gpu_hour", backend=name, where=where, minimum=0)
    smallest = fields.integer(row, "min_rentable_gpu_count", backend=name, where=where, minimum=1)
    vram = fields.number(row, "gpu_memory_gb", backend=name, where=where, minimum=1)
    ram = fields.number(row, "ram_gb", backend=name, where=where, minimum=0)
    disk = fields.number(row, "disk_gb", backend=name, where=where, minimum=0)
    tier = fields.text(row, "tier", backend=name, where=where)
    assert model is not None and per_gpu is not None and vram is not None and ram is not None and disk is not None
    if tier not in ("secure", "spot"):
        raise UnknownFieldType(f"lium: field 'tier' in {where} is {tier!r}, not secure or spot")
    if smallest > total:
        raise UnknownUnit(f"lium: {where} has a smallest order of {smallest} GPUs but lists {total}")
    # The feed gives the node price too. Where it is there, it must agree with the per-GPU price.
    if row.get("price_per_node_hour") is not None:
        node_price = fields.number(row, "price_per_node_hour", backend=name, where=where, minimum=0)
        assert node_price is not None
        if not math.isclose(node_price, per_gpu * total, rel_tol=1e-2, abs_tol=0.01):
            raise UnknownUnit(f"lium: the two prices of {where} disagree ({per_gpu} per GPU, {node_price} per node)")
    # Whole GPUs only. A node that lists a smallest order of all its GPUs rents whole, and others rent by the GPU.
    if not smallest <= gpu_count <= total:
        raise SkipNode(f"nodes that do not rent as {gpu_count} GPUs")

    download = None
    if row.get("network_download_mbps") is not None:
        download = fields.number(row, "network_download_mbps", backend=name, where=where, minimum=0)
    reliability = None
    if row.get("reliability_score") is not None:
        score = fields.number(row, "reliability_score", backend=name, where=where, minimum=0)
        assert score is not None
        if score > 100:
            raise UnknownUnit(f"lium: field 'reliability_score' in {where} is {score}, but the driver expects 0 to 100")
        reliability = score / 100.0
    country = row.get("country_code")
    if country is not None and not isinstance(country, str):
        raise UnknownFieldType(f"lium: field 'country_code' in {where} is not text")
    region = country.upper() if isinstance(country, str) and len(country) == 2 and country.isalpha() else None

    # CPU, memory and disk are shared out in proportion to the GPUs taken.
    # https://docs.lium.io/pod-users/create-pod
    share = gpu_count / total
    price = per_gpu * gpu_count
    return Offer(
        backend=name,
        offer_id=offer_id_for(node_id, gpu_count),
        gpu_model=model,
        gpu_count=gpu_count,
        gpu_vram_gb_each=vram,
        # The listed price is the whole price. Lium documents no separate fee, so the price is also the sticker price.
        hourly_all_in=price,
        host_ram_gb=ram * share,
        disk_gb=disk * share,
        compute_capability=compute_capability_for(model),
        sticker_hourly=price,
        bandwidth_cost_per_gb=0.0,
        fixed_fees=0.0,
        download_mbps=download,
        rentable=available and free >= gpu_count,
        interruptible=tier == "spot",
        verified=tier == "secure",
        reliability=reliability,
        region=region,
        allocated_disk_gb=disk * share,
        gpu_fraction=1.0,
        host_id=node_id,
    )


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before a retry. A 429 answer carries a Retry-After header."""
    wait = response.headers.get("Retry-After", "")
    return min(float(wait), 60.0) if wait.isdigit() else backoff(attempt)


class LiumBackend(RentalBackend):
    name = "lium"

    # 400, 401, 402, 403, 404 and 422 prove nothing was created. Some 409 codes do, by name. 429 and 5xx do not.
    refused_statuses = (400, 401, 402, 403, 404, 422)
    fault_find = "pod name"
    _sleep = staticmethod(time.sleep)

    def __init__(self, client: httpx.Client | None = None, root: str = API_ROOT, *, store=None) -> None:
        super().__init__(client, store=store)
        self._root = root.rstrip("/")

    def capabilities(self) -> Capabilities:
        # Search works against live data. Renting passed one paid round trip on 2026-10-26.
        # A key made with the user's key cannot create another key, so scoped_watchdog_credential stays False.
        # The rent call takes termination_hours, but marketplace_deadline stays False until a test shows it ends a pod.
        return Capabilities(search=True, rent=True)

    # --- HTTP -------------------------------------------------------------

    def _request(
        self, method: str, path: str, creds: Creds | None, *, body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None, missing_ok: bool = False,
    ) -> Any:
        """Send one request. Reads and deletes retry on 429 and 5xx. A create is never resent."""
        client = self._http()
        sent = {"User-Agent": f"ablit/{__version__}", **(headers or {})}
        if creds is not None:
            # The key goes in X-API-Key, with no Bearer prefix. https://docs.lium.io/developers/authentication
            sent["X-API-Key"] = creds.api_key
        response = send_with_retry(
            lambda: client.request(
                method, self._root + path, headers=sent, json=body, follow_redirects=False,
            ),
            attempts=5 if method in ("GET", "DELETE") else 1, sleep=self._sleep, delay=_retry_delay)
        status = response.status_code
        refuse_redirect(self.name, response)
        if status == 404 and missing_ok:
            return None
        if not response.is_success:
            raise self._rejection(response)
        if status == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as error:
            raise ResponseNotRecognized("lium: expected JSON") from error

    def _rejection(self, response: httpx.Response) -> RequestRejected:
        """Read the error envelope: error.code, error.message, and the older flat message."""
        status = response.status_code
        code, message = None, "the request failed"
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                if isinstance(error.get("code"), str) and _CODE.fullmatch(error["code"]):
                    code = error["code"]
                if isinstance(error.get("message"), str) and error["message"]:
                    message = error["message"][:200]
            elif isinstance(data.get("message"), str) and data["message"]:
                message = data["message"][:200]
        if status == 401 or (status == 403 and code == "forbidden"):
            return CredentialsRejected(self.name, status, "the API key or its scopes were refused")
        return LiumRejected(status, code, message)

    def test_credentials(self, creds: Creds) -> None:
        """One read-only call. https://docs.lium.io/developers/authentication (verify a key)"""
        data = self._request("GET", "/users/me", creds)
        if not isinstance(data, dict):
            raise ResponseNotRecognized("lium: the account answer is not an object")

    # --- search -----------------------------------------------------------

    def _nodes(self) -> list[Any]:
        data = self._request("GET", NODES_PATH, None)
        nodes = data.get("nodes") if isinstance(data, dict) else None
        if not isinstance(nodes, list):
            raise ResponseNotRecognized("lium: the nodes feed has no 'nodes' list")
        return nodes

    def search(
        self, req: HardwareRequest, creds: Creds | None, opts: PlanOptions | None = None
    ) -> OfferSearch:
        """Read the public feed. It needs no key, so the key is not sent."""
        offers: list[Offer] = []
        skipped: Counter[str] = Counter()
        for row in self._nodes():
            try:
                offer = offer_from_node(row, req.gpu_count)
            except SkipNode as skip:
                skipped[skip.reason] += 1
                continue
            except UnknownFieldType as error:
                # The feed documents only id and rent_url as always set. A node that lacks a field ablit needs
                # is not an offer. A field of the wrong type or unit still stops the search.
                if not str(error).endswith((" is missing", " is null")):
                    raise
                skipped["nodes missing a field ablit needs"] += 1
                continue
            if offer.host_id in self.denied_host_ids:
                skipped["nodes on the local deny list"] += 1
                continue
            offers.append(offer)
        parts = ["prices are USD per hour for the pod, billed per second, and Lium documents no other fee"]
        if skipped:
            parts.append("left out before the filters: " + ", ".join(f"{n} {r}" for r, n in sorted(skipped.items())))
        # The feed lists every rentable node in one answer, so the search sees all of the marketplace.
        return OfferSearch(backend=self.name, offers=offers, complete=True, note="; ".join(parts))

    def requote(self, offer: Offer, creds: Creds) -> Offer | None:
        """Read the feed again. Return None when the node is gone or no longer free."""
        node_id, count = parse_offer_id(offer.offer_id)
        for row in self._nodes():
            if isinstance(row, dict) and row.get("id") == node_id:
                try:
                    again = offer_from_node(row, count)
                except SkipNode:
                    return None
                return again if again.rentable else None
        return None

    # --- pods -------------------------------------------------------------

    def pods(self, creds: Creds) -> list[dict[str, Any]]:
        """Read every pod the key can see. The list has no pages."""
        try:
            data = self._request("GET", "/pods", creds)
        except LiumRejected as error:
            # A key with no balance and no pods gets this answer, so the list is empty.
            # https://docs.lium.io/developers/error-codes
            if error.code == "BALANCE_REQUIRED":
                return []
            raise
        if not isinstance(data, list):
            raise ResponseNotRecognized("lium: the pod list answer is not a list")
        for row in data:
            if not isinstance(row, dict):
                raise UnknownFieldType(f"lium: a pod is a {type(row).__name__}, not an object")
            fields.text(row, "id", backend=self.name, where="a pod")
            fields.text(row, "pod_name", backend=self.name, where=f"pod {row['id']}", nullable=True)
        return data

    def _status(self, row: Mapping[str, Any]) -> str:
        status = fields.text(row, "status", backend=self.name, where=f"pod {row['id']}")
        if status not in POD_STATUSES:
            raise UnknownFieldType(f"lium: pod {row['id']} has status {status!r}, which the driver does not know")
        assert status is not None
        return status

    # --- create -----------------------------------------------------------

    def _record(self, run_id: str) -> dict[str, Any] | None:
        from ablit.runs import RunError, RunStore

        store = self.store or RunStore()
        try:
            return store.load(run_id)
        except RunError:
            return None

    def _prepare_create(self, offer: Offer, boot: BootSpec, run_id: str) -> dict[str, Any]:
        key = boot.ssh_public_key
        if key is None or not SSH_KEY_PATTERN.match(key) or "\n" in key:
            raise ValueError("lium: the SSH public key is missing, too long, or has characters the driver refuses")
        node, count = parse_offer_id(offer.offer_id)
        repository, digest = split_image(boot.image)
        # Lium deletes the pod after this many whole hours, which is the part of the cap that works
        # while this computer is off. A cap below one hour of rent still gets one hour, and the
        # local check ends the run sooner. The run record holds the cap.
        record = self._record(run_id) or {}
        cap = record.get("cap_usd")
        hours = None
        if isinstance(cap, (int, float)) and not isinstance(cap, bool) and offer.hourly_all_in > 0:
            hours = min(MAX_TERMINATION_HOURS, max(MIN_TERMINATION_HOURS, math.floor(cap / offer.hourly_all_in)))
        return {"node": node, "count": count, "repository": repository, "digest": digest, "key": key,
                "hours": hours, "template": {
                    "name": self.label(run_id),
                    "docker_image": repository,
                    "docker_image_digest": digest,
                    # Only the SSH port is exposed. The engine is reached through an SSH tunnel.
                    "internal_ports": [22],
                    "volumes": ["/root"],
                    "environment": {},
                    "entrypoint": LIUM_ENTRYPOINT,
                    "startup_commands": boot.onstart,
                    "is_private": True,
                    # Lium deletes a one-time template with the pod that used it.
                    "one_time_template": True,
                }}

    def _journal_create(self, record: dict[str, Any], offer: Offer, boot: BootSpec, prepared: dict[str, Any]) -> None:
        record["lium_create"] = {"node_id": prepared["node"], "pod_name": self.label(record["run_id"]),
                                 "termination_hours": prepared["hours"], "sent_at": int(time.time())}

    def _send_create(self, offer: Offer, boot: BootSpec, run_id: str, creds: Creds, prepared: dict[str, Any]) -> Any:
        template_id = self._make_template(prepared["template"], creds)
        body: dict[str, Any] = {
            "pod_name": self.label(run_id), "template_id": template_id, "user_public_key": prepared["key"],
            "gpu_count": prepared["count"],
        }
        if prepared["hours"] is not None:
            body["termination_hours"] = prepared["hours"]
        try:
            # https://lium.io/api/openapi.json (rent_executors__executor_uuid__rent_post). The Idempotency-Key header
            # makes Lium answer a repeated call with the first call's pod. ablit never repeats a create anyway.
            return self._request("POST", f"/executors/{prepared['node']}/rent", creds, body=body,
                                 headers={"Idempotency-Key": self.label(run_id)})
        except RequestRejected as error:
            if self._create_refused(error):
                self._drop_template(template_id, creds)
            raise

    def _make_template(self, template: dict[str, Any], creds: Creds) -> str:
        """Create the private one-time template. No pod exists yet, so any failure here proves nothing is billing."""
        try:
            data = self._request("POST", "/templates", creds, body=template)
            template_id = data.get("id") if isinstance(data, dict) else None
            if not isinstance(template_id, str) or not _ID.fullmatch(template_id):
                raise ResponseNotRecognized("lium: the template answer has no ID")
        except CredentialsRejected:
            raise
        except (RequestRejected, ResponseNotRecognized, httpx.HTTPError) as error:
            raise TemplateNotCreated(f"the template step failed, so no pod was rented: {error}") from error
        return template_id

    def _drop_template(self, template_id: str, creds: Creds) -> None:
        """Delete the template of a rent that was refused. A failure is ignored, because an unused template is free."""
        try:
            self._request("DELETE", f"/templates/{template_id}", creds, missing_ok=True)
        except (RequestRejected, ResponseNotRecognized, httpx.HTTPError):
            pass

    def _create_refused(self, error: RequestRejected) -> bool:
        if isinstance(error, TemplateNotCreated) or error.status in self.refused_statuses:
            return True
        return error.status == 409 and getattr(error, "code", None) in DEFINITE_409_CODES

    def _read_created(self, data: Any, record: dict[str, Any], creds: Creds) -> list[str]:
        # The documented answer is {"success": true, "pod_id": "<uuid>"}. A different shape is not guessed at.
        # The pod is then found by its name. https://docs.lium.io/developers/quickstart
        pod_id = data.get("pod_id") if isinstance(data, dict) else None
        if not isinstance(data, dict) or data.get("success") is not True or not isinstance(pod_id, str) \
                or not _ID.fullmatch(pod_id):
            raise ResponseNotRecognized("lium: the rent answer has no pod_id. Re-run down to find the pod by its name")
        return [pod_id]

    # --- find, watch, destroy ---------------------------------------------

    def reconcile(self, run_id: str, creds: Creds) -> Inventory:
        """Find the run's pods by name. The pod name is the only tag a rent call can carry."""
        rows = [r for r in self.pods(creds) if r.get("pod_name") == self.label(run_id)]
        return self._inventory(run_id, rows)

    def observe(self, alloc: Allocation, creds: Creds) -> Observation:
        row = self._one([r for r in self.pods(creds) if str(r["id"]) in alloc.resource_ids], "pod")
        if row is None:
            return self._absent()
        where = f"pod {row['id']}"
        status = self._status(row)
        price = fields.number(row, "price", backend=self.name, where=where, nullable=True, minimum=0)
        pending = fields.number(row, "pending_price", backend=self.name, where=where, nullable=True, minimum=0) \
            if "pending_price" in row else None
        spend = fields.number(row, "spend_to_date", backend=self.name, where=where, nullable=True, minimum=0) \
            if "spend_to_date" in row else None
        host, port = self._ssh_endpoint(row, where)
        state = STATE_BY_STATUS[status]
        # A pod that is RUNNING with no SSH command yet is still starting.
        if state == "running" and not (host and port):
            state = "starting"
        # A raise that is pending will apply, so the cap math uses the higher figure.
        rate = max(x for x in (price, pending) if x is not None) if price is not None or pending is not None else None
        executor = row.get("executor_id")
        host_id = executor if isinstance(executor, str) and _ID.fullmatch(executor) else alloc.host_id
        found = Allocation(alloc.run_id, self.name, alloc.resource_ids,
                           rate if rate is not None else alloc.hourly_rate, host, port, host_id)
        return Observation(time.time(), state, spend_usd=spend, hourly_rate=rate, allocation=found)

    @staticmethod
    def _ssh_endpoint(row: Mapping[str, Any], where: str) -> tuple[str | None, int | None]:
        """Read the SSH host and port from `ssh_connect_cmd`, which has the form `ssh <user>@<host> [-p <port>]`."""
        # https://docs.lium.io/developers/sdk/reference (pod_ssh_command)
        command = row.get("ssh_connect_cmd")
        if command is None or command == "":
            return None, None
        if not isinstance(command, str):
            raise UnknownFieldType(f"lium: field 'ssh_connect_cmd' in {where} is not text")
        match = _SSH_COMMAND.fullmatch(command.strip())
        if match is None or match.group(2) != "root" or (match.group(1) and match.group(4)):
            raise UnknownFieldType(f"lium: field 'ssh_connect_cmd' in {where} is not 'ssh root@<host> [-p <port>]'")
        port = int(match.group(1) or match.group(4) or 22)
        if not 1 <= port <= 65535:
            raise UnknownFieldType(f"lium: invalid SSH port in {where}")
        return match.group(3), port

    def _terminate(self, resource_id: str, creds: Creds) -> None:
        """Delete a pod. A 404 means it is already gone. Billing stops at the delete request."""
        # https://lium.io/api/openapi.json (DELETE /pods/{id}) and https://docs.lium.io/pod-users/billing
        if not _ID.fullmatch(resource_id):
            raise ValueError(f"lium: {resource_id!r} is not a pod ID")
        self._request("DELETE", f"/pods/{resource_id}", creds, missing_ok=True)

    def _still_billable(self, inv: Inventory, creds: Creds) -> list[str]:
        # A pod that is absent from the list, or that the billing page lists as not charged, no longer bills.
        return [
            str(r["id"]) for r in self.pods(creds)
            if (str(r["id"]) in inv.resource_ids or r.get("pod_name") == self.label(inv.run_id))
            and self._status(r) not in NOT_BILLED
        ]

    def watchdog_credential(self, alloc: Allocation, creds: Creds) -> WatchdogCred | None:
        # Lium keys can have scopes and budgets, but keys are made from a signed-in session, and a key made
        # with an API key "cannot create other keys". ablit cannot make a key limited to one pod, and the key
        # that can delete a pod can delete every pod it sees, so no key goes on the machine.
        # https://docs.lium.io/developers/claude-code-gpu  https://docs.lium.io/pod-users/api-keys
        return None
