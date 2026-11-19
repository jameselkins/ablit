"""Helpers shared by the commands that act on an existing run."""

from __future__ import annotations

from typing import NoReturn

from ablit.backends.errors import CredentialsRejected, WorkloadRejected
from ablit.credentials import CredentialError, credentials
from ablit.redact import RedactedError, Redactor
from ablit.runs import RunError


def pair(record, drivers, environ):
    """Return the driver and key for the marketplace that holds a run."""
    backend = record.get("backend", "vast")
    if backend not in drivers:
        raise RunError(f"No driver is available for {backend}.")
    return drivers[backend], credentials(backend, environ)


def raise_redacted(error: Exception, redactor: Redactor) -> NoReturn:
    """Redact diagnostics while preserving the CLI's error classification."""
    if isinstance(error, (EOFError, WorkloadRejected)):
        raise error
    if isinstance(error, (CredentialError, CredentialsRejected, RunError)):
        error.args = (redactor.remote_text(str(error)),)
        raise error
    detail = redactor.remote_text(f"{type(error).__name__}: {error}")
    raise RedactedError(detail) from None
