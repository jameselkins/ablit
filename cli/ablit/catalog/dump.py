"""Write a catalog entry in one fixed layout.

`tools/verify_entry.py --fill` uses this so that a filled file stays readable
and produces small diffs. Mappings inside the `artifacts` and `staging` lists
are written on one line each. Comments inside the file are not kept, except the
comment lines at the top.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

_ORDER = [
    "schema", "id", "display_name", "source", "artifacts", "staging",
    "profile", "sources", "tests", "status", "notes",
]
_FLOW_UNSAFE = re.compile(r"[,\[\]{}:#&*!|>'\"%@`]|^\s|\s$|^-|\s{2,}")


def _is_plain_safe(text: str) -> bool:
    if text == "" or _FLOW_UNSAFE.search(text):
        return False
    try:
        return yaml.safe_load(text) == text
    except yaml.YAMLError:
        return False


def _scalar(value: Any, flow: bool) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        if flow:
            return value if _is_plain_safe(value) else json.dumps(value, ensure_ascii=False)
        # Block context allows commas, colons inside words and parentheses.
        if re.search(r"^[\s\-?:,\[\]{}#&*!|>'\"%@`]|: | #|\s$", value) or yaml.safe_load(value) != value:
            return json.dumps(value, ensure_ascii=False)
        return value
    raise TypeError(f"cannot write {type(value).__name__} as a scalar")


def _flow(value: Any) -> str:
    if isinstance(value, dict):
        inner = ", ".join(f"{key}: {_flow(item)}" for key, item in value.items())
        return "{" + inner + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_flow(item) for item in value) + "]"
    return _scalar(value, flow=True)


def _block(value: Any, indent: int) -> list[str]:
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, dict) and item and key not in ("gpu", "storage"):
                lines.append(f"{pad}{key}:")
                lines.extend(_block(item, indent + 1))
            elif isinstance(item, (dict, list)):
                if isinstance(item, list) and item and any(isinstance(i, (dict, str)) for i in item):
                    lines.append(f"{pad}{key}:")
                    lines.extend(_block(item, indent + 1))
                else:
                    lines.append(f"{pad}{key}: {_flow(item)}")
            else:
                lines.append(f"{pad}{key}: {_scalar(item, flow=False)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}- {_flow(item)}")
            else:
                lines.append(f"{pad}- {_scalar(item, flow=False)}")
    return lines


def dump_entry(data: dict[str, Any], header_comments: list[str] | None = None) -> str:
    """Return the canonical YAML text for an entry."""
    ordered = {key: data[key] for key in _ORDER if key in data}
    for key in data:
        if key not in ordered:
            ordered[key] = data[key]
    lines = list(header_comments or [])
    lines.extend(_block(ordered, 0))
    text = "\n".join(lines) + "\n"
    if yaml.safe_load(text) != data:
        raise ValueError("the written entry does not read back to the same data")
    return text


def leading_comments(text: str) -> list[str]:
    """Return the comment lines at the top of a file."""
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            lines.append(line)
        else:
            break
    return lines
