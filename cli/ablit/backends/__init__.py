"""Backend drivers.

Drivers are modules in this package, with no plugin discovery. Offers and
drivers have no referral fields, and this package cannot import
ablit.referral.
"""

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
)

__all__ = [
    "Allocation",
    "Backend",
    "BillingEvidence",
    "BootSpec",
    "Capabilities",
    "Creds",
    "Inventory",
    "Observation",
    "OfferSearch",
    "TerminationResult",
    "WatchdogCred",
]
