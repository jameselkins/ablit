"""Typed reads from a marketplace response.

A driver reads every field through these helpers, so a field with a type the
driver does not know raises `UnknownFieldType` with the field's name instead of
turning into a wrong number. Booleans are not accepted as numbers.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from ablit.backends.errors import UnknownFieldType


def _describe(backend: str, where: str, key: str) -> str:
    return f"{backend}: field {key!r} in {where}"


def number(
    row: Mapping[str, Any],
    key: str,
    *,
    backend: str,
    where: str = "a row",
    nullable: bool = False,
    minimum: float | None = None,
) -> float | None:
    """Read a finite number, or None when the field is null and `nullable` is set."""
    if key not in row:
        raise UnknownFieldType(f"{_describe(backend, where, key)} is missing")
    value = row[key]
    if value is None:
        if nullable:
            return None
        raise UnknownFieldType(f"{_describe(backend, where, key)} is null")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnknownFieldType(f"{_describe(backend, where, key)} has type {type(value).__name__}, not a number")
    if not math.isfinite(value):
        raise UnknownFieldType(f"{_describe(backend, where, key)} is not a finite number")
    if minimum is not None and value < minimum:
        raise UnknownFieldType(f"{_describe(backend, where, key)} is {value}, below the expected minimum {minimum}")
    return float(value)


def integer(row: Mapping[str, Any], key: str, *, backend: str, where: str = "a row", minimum: int | None = None) -> int:
    """Read a whole number. A float with a fraction is refused."""
    if key not in row:
        raise UnknownFieldType(f"{_describe(backend, where, key)} is missing")
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnknownFieldType(f"{_describe(backend, where, key)} has type {type(value).__name__}, not a whole number")
    if isinstance(value, float) and (not math.isfinite(value) or value != int(value)):
        raise UnknownFieldType(f"{_describe(backend, where, key)} is {value}, not a whole number")
    if minimum is not None and value < minimum:
        raise UnknownFieldType(f"{_describe(backend, where, key)} is {value}, below the expected minimum {minimum}")
    return int(value)


def text(
    row: Mapping[str, Any], key: str, *, backend: str, where: str = "a row", nullable: bool = False
) -> str | None:
    """Read a non-empty string, or None when the field is null and `nullable` is set."""
    if key not in row:
        raise UnknownFieldType(f"{_describe(backend, where, key)} is missing")
    value = row[key]
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise UnknownFieldType(f"{_describe(backend, where, key)} is not a non-empty string")
    return value


def boolean(row: Mapping[str, Any], key: str, *, backend: str, where: str = "a row") -> bool:
    if key not in row:
        raise UnknownFieldType(f"{_describe(backend, where, key)} is missing")
    value = row[key]
    if not isinstance(value, bool):
        raise UnknownFieldType(f"{_describe(backend, where, key)} has type {type(value).__name__}, not true or false")
    return value
