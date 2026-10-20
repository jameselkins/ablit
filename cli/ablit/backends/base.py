"""The driver interface and the records it passes around.

A capability counts as present only after a test shows that it works. Drivers
refuse response shapes and units they do not recognize.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ablit.select.types import HardwareRequest, Offer


@dataclass(frozen=True)
class Capabilities:
    """What a driver can do. Each flag stays False until a test shows it works."""

    search: bool = False
    rent: bool = False
    scoped_watchdog_credential: bool = False
    marketplace_deadline: bool = False
    spot: bool = False


@dataclass(frozen=True)
class Creds:
    """A marketplace API key. Its text form hides the key."""

    api_key: str = field(repr=False)


@dataclass(frozen=True)
class OfferSearch:
    """Offers from one backend, and whether the search saw all of them."""

    backend: str
    offers: list[Offer]
    complete: bool
    note: str | None = None


@dataclass(frozen=True)
class BootSpec:
    """What the machine runs when it starts."""

    image: str
    onstart: str
    disk_gb: int
    env: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Allocation:
    run_id: str
    backend: str
    resource_ids: list[str]
    hourly_rate: float
    ssh_host: str | None = None
    ssh_port: int | None = None


@dataclass(frozen=True)
class Inventory:
    """Every billable resource found for a run, including volumes."""

    run_id: str
    backend: str
    resource_ids: list[str]


@dataclass(frozen=True)
class Observation:
    checked_at: float
    state: str
    spend_usd: float | None = None


@dataclass(frozen=True)
class TerminationResult:
    requested: bool
    resource_ids_terminated: list[str]


@dataclass(frozen=True)
class BillingEvidence:
    """The check time, what still bills, and the final cost if reported."""

    checked_at: float
    still_billable: list[str]
    final_cost_usd: float | None = None

    @property
    def stopped(self) -> bool:
        return not self.still_billable


@dataclass(frozen=True)
class WatchdogCred:
    """A credential limited to destroying one instance. Its text form hides the key."""

    instance_id: str
    token: str = field(repr=False)


class Backend(Protocol):
    name: str

    def capabilities(self) -> Capabilities: ...

    def search(self, req: HardwareRequest, creds: Creds | None) -> OfferSearch: ...

    def requote(self, offer: Offer, creds: Creds) -> Offer | None: ...

    def create(self, offer: Offer, boot: BootSpec, run_id: str, creds: Creds) -> Allocation: ...

    def reconcile(self, run_id: str, creds: Creds) -> Inventory: ...

    def observe(self, alloc: Allocation, creds: Creds) -> Observation: ...

    def destroy(self, inv: Inventory, creds: Creds) -> TerminationResult: ...

    def verify_billing_stopped(self, inv: Inventory, creds: Creds) -> BillingEvidence: ...

    def watchdog_credential(self, alloc: Allocation, creds: Creds) -> WatchdogCred | None: ...


class UnwrittenBackend:
    """Base for driver stubs. Every call except `capabilities` raises NotImplementedError."""

    name = "unwritten"

    def capabilities(self) -> Capabilities:
        return Capabilities()

    def search(self, req: HardwareRequest, creds: Creds | None) -> OfferSearch:
        raise NotImplementedError(f"{self.name}: search is not written yet")

    def requote(self, offer: Offer, creds: Creds) -> Offer | None:
        raise NotImplementedError(f"{self.name}: requote is not written yet")

    def create(self, offer: Offer, boot: BootSpec, run_id: str, creds: Creds) -> Allocation:
        raise NotImplementedError(f"{self.name}: create is not written yet")

    def reconcile(self, run_id: str, creds: Creds) -> Inventory:
        raise NotImplementedError(f"{self.name}: reconcile is not written yet")

    def observe(self, alloc: Allocation, creds: Creds) -> Observation:
        raise NotImplementedError(f"{self.name}: observe is not written yet")

    def destroy(self, inv: Inventory, creds: Creds) -> TerminationResult:
        raise NotImplementedError(f"{self.name}: destroy is not written yet")

    def verify_billing_stopped(self, inv: Inventory, creds: Creds) -> BillingEvidence:
        raise NotImplementedError(f"{self.name}: verify_billing_stopped is not written yet")

    def watchdog_credential(self, alloc: Allocation, creds: Creds) -> WatchdogCred | None:
        raise NotImplementedError(f"{self.name}: watchdog_credential is not written yet")
