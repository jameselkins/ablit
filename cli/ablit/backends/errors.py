"""Errors that a driver raises.

Every error here means the backend's answer cannot be used as it stands. The
planner catches `BackendError`, names the backend in its output, and says that
the comparison did not include it.
"""

from __future__ import annotations


class BackendError(Exception):
    """A backend failed in a way the planner reports and carries on from."""


class RequestRejected(BackendError):
    """The marketplace answered with an error status or an error body."""

    def __init__(self, backend: str, status: int | None, detail: str) -> None:
        where = f"HTTP {status}" if status is not None else "an error"
        super().__init__(f"{backend} answered with {where}: {detail}")
        self.backend = backend
        self.status = status
        self.detail = detail


class CredentialsRejected(RequestRejected):
    """The marketplace refused the API key."""


class ResponseNotRecognized(BackendError):
    """The answer has a shape, type or unit that the driver does not know.

    The driver refuses it instead of guessing what it means.
    """


class UnknownFieldType(ResponseNotRecognized):
    """A field is missing, or its value has a type the driver does not accept."""


class UnknownUnit(ResponseNotRecognized):
    """A value is in a unit the driver cannot confirm, or two fields disagree about it."""


class CreateUncertain(BackendError):
    """Create may have succeeded. Only label reconciliation is allowed next."""


class WorkloadRejected(RequestRejected):
    """The marketplace explicitly rejected the workload."""
