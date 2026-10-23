"""The lifecycle steps that every rental driver shares.

A driver subclasses `RentalBackend` and writes only its marketplace's calls and
field mapping. The base keeps the HTTP client, the create journal that allows
one create per run, the ABLIT_FAULT test hook, the "exactly one resource" read
in `observe`, the destroy loop and the billing check.

Hooks a driver must write: `search`, `requote`, `reconcile`, `observe`,
`_send_create`, `_read_created`, `_terminate` and `_still_billable`. Hooks it
may override: `_prepare_create`, `_journal_create`, `_create_refused`,
`_allocation` and `_verify_created`.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from ablit.backends.base import (
    Allocation,
    BillingEvidence,
    BootSpec,
    Creds,
    Inventory,
    Observation,
    TerminationResult,
    UnwrittenBackend,
)
from ablit.backends.errors import CreateUncertain, RequestRejected, ResponseNotRecognized
from ablit.select.types import Offer


class RentalBackend(UnwrittenBackend):
    """Base for drivers that rent machines. Subclasses set `name`."""

    # HTTP statuses on a create that prove nothing was created.
    refused_statuses: tuple[int, ...] = ()
    # What the user looks for after a dropped create answer, used in the ABLIT_FAULT message.
    fault_find = "label"

    def __init__(self, client: httpx.Client | None = None, *, store=None) -> None:
        self._client = client
        self._owns_client = client is None
        self.store = store
        self.denied_host_ids: set[str] = set()

    def close(self) -> None:
        """Close the HTTP client when this driver created it."""
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def _http(self, timeout: float = 30.0) -> httpx.Client:
        """Return the HTTP client, creating it on first use."""
        if self._client is None:
            self._client = httpx.Client(timeout=httpx.Timeout(timeout))
        return self._client

    @staticmethod
    def label(run_id: str) -> str:
        """The name that marks a run's resources, for marketplaces that let a create carry one."""
        return f"ablit-{run_id}"

    # --- create -----------------------------------------------------------

    def create(self, offer: Offer, boot: BootSpec, run_id: str, creds: Creds) -> Allocation:
        """Journal the attempt before sending it. A run gets at most one create."""
        from ablit.runs import RunStore

        store = self.store or RunStore()
        with store.lock(run_id + ".create"):
            record = store.load(run_id)
            if record.get("create_sent"):
                found = self.reconcile(run_id, creds)
                if len(found.resource_ids) == 1:
                    return self._allocation(run_id, found.resource_ids, offer)
                raise CreateUncertain("Create was already sent. Re-run down to reconcile; no second create is allowed.")
            if "@sha256:" not in boot.image:
                raise ResponseNotRecognized(f"{self.name}: an image digest is required")
            if boot.env:
                raise ResponseNotRecognized(f"{self.name}: credentials and engine config must travel over SSH")
            prepared = self._prepare_create(offer, boot, run_id)
            record["create_sent"] = True
            record["started_at"] = time.time()
            self._journal_create(record, offer, boot, prepared)
            store.save(record)
            try:
                data = self._send_create(offer, boot, run_id, creds, prepared)
            except RequestRejected as error:
                if self._create_refused(error):
                    record["create_rejected"] = True
                    store.save(record)
                raise
            if os.environ.get("ABLIT_FAULT") == "drop-create-response":
                raise CreateUncertain(
                    f"The create response was dropped by ABLIT_FAULT. Re-run up or down to find the {self.fault_find}."
                )
            resource_ids = self._read_created(data, record, creds)
            record["resource_ids"] = resource_ids
            store.save(record)
            self._verify_created(data, offer, boot)
            return self._allocation(run_id, resource_ids, offer)

    def _prepare_create(self, offer: Offer, boot: BootSpec, run_id: str) -> Any:
        """Check the offer and boot spec and build what the send step needs. Runs before the journal is written."""
        return None

    def _journal_create(self, record: dict[str, Any], offer: Offer, boot: BootSpec, prepared: Any) -> None:
        """Add driver facts to the run record, which is saved right after this call."""

    def _send_create(self, offer: Offer, boot: BootSpec, run_id: str, creds: Creds, prepared: Any) -> Any:
        """Send the one create request and return its answer."""
        raise NotImplementedError(f"{self.name}: _send_create is not written yet")

    def _create_refused(self, error: RequestRejected) -> bool:
        """True when the error proves that the marketplace created nothing."""
        return error.status in self.refused_statuses

    def _read_created(self, data: Any, record: dict[str, Any], creds: Creds) -> list[str]:
        """Return the IDs of the resources the create made."""
        raise NotImplementedError(f"{self.name}: _read_created is not written yet")

    def _verify_created(self, data: Any, offer: Offer, boot: BootSpec) -> None:
        """Refuse a created resource that differs from the quote. The IDs are already saved."""

    def _allocation(self, run_id: str, resource_ids: list[str], offer: Offer) -> Allocation:
        return Allocation(run_id, self.name, resource_ids, offer.hourly_all_in)

    # --- observe, destroy, billing ----------------------------------------

    def _inventory(self, run_id: str, rows: list[dict[str, Any]]) -> Inventory:
        return Inventory(run_id, self.name, [str(row["id"]) for row in rows])

    @staticmethod
    def _absent() -> Observation:
        return Observation(time.time(), "absent")

    def _one(self, rows: list[dict[str, Any]], noun: str) -> dict[str, Any] | None:
        """Return the only row, None when there is none, and refuse more than one."""
        if not rows:
            return None
        if len(rows) != 1:
            raise ResponseNotRecognized(f"{self.name}: expected exactly one {noun}")
        return rows[0]

    def destroy(self, inv: Inventory, creds: Creds) -> TerminationResult:
        terminated = []
        for resource_id in inv.resource_ids:
            self._terminate(resource_id, creds)
            terminated.append(resource_id)
        return TerminationResult(True, terminated)

    def _terminate(self, resource_id: str, creds: Creds) -> None:
        """End one resource. An already ended resource counts as done."""
        raise NotImplementedError(f"{self.name}: _terminate is not written yet")

    def verify_billing_stopped(self, inv: Inventory, creds: Creds) -> BillingEvidence:
        return BillingEvidence(time.time(), self._still_billable(inv, creds))

    def _still_billable(self, inv: Inventory, creds: Creds) -> list[str]:
        """Return the IDs of the run's resources that the marketplace still lists as billing."""
        raise NotImplementedError(f"{self.name}: _still_billable is not written yet")
