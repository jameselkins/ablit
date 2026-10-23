"""Helpers shared by the commands that act on an existing run."""

from __future__ import annotations

from ablit.credentials import credentials
from ablit.runs import RunError


def pair(record, drivers, environ):
    """Return the driver and key for the marketplace that holds a run."""
    backend = record.get("backend", "vast")
    if backend not in drivers:
        raise RunError(f"No driver is available for {backend}.")
    return drivers[backend], credentials(backend, environ)
