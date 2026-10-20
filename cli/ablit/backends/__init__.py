"""Backend drivers.

Drivers are modules in this package, with no plugin discovery. Offers and
drivers have no referral fields, and this package cannot import
ablit.referral.
"""

from __future__ import annotations

import httpx

from ablit.backends.base import (
    Allocation,
    Backend,
    BillingEvidence,
    BootSpec,
    Capabilities,
    Creds,
    Inventory,
    Observation,
    OfferSearch,
    TerminationResult,
    WatchdogCred,
    creds_from_env,
)
from ablit.backends.errors import BackendError
from ablit.backends.runpod import RunPodBackend
from ablit.backends.vast import VastBackend


def default_backends(client: httpx.Client | None = None) -> dict[str, Backend]:
    """Return the backends that can search, by name. Tests pass a client to control the HTTP calls."""
    return {"runpod": RunPodBackend(client), "vast": VastBackend(client)}


__all__ = [
    "Allocation",
    "Backend",
    "BackendError",
    "BillingEvidence",
    "BootSpec",
    "Capabilities",
    "Creds",
    "Inventory",
    "Observation",
    "OfferSearch",
    "TerminationResult",
    "WatchdogCred",
    "creds_from_env",
    "default_backends",
]
