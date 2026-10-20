"""A small JSON Schema checker.

It supports only the keywords that catalog/schema/v1.json uses, so contributors
can run the offline checks with PyYAML and nothing else. Each error is a plain
string that starts with the path of the value that failed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
}


def load_schema(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        schema = json.load(handle)
    if not isinstance(schema, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return schema


def validate(instance: Any, schema: dict[str, Any]) -> list[str]:
    """Return a list of error strings. An empty list means the value is valid."""
    return _check(instance, schema, schema, "$")


def _resolve(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"only local references are supported, got {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _type_name(value: Any) -> str:
    for name in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _TYPE_CHECKS[name](value):
            return name
    return type(value).__name__


def _check(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str) -> list[str]:
    if "$ref" in schema:
        return _check(value, _resolve(schema["$ref"], root), root, path)

    errors: list[str] = []

    if "type" in schema:
        wanted = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_TYPE_CHECKS[name](value) for name in wanted):
            return [f"{path}: expected {' or '.join(wanted)}, got {_type_name(value)}"]

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match {schema['pattern']}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} is below the minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} is above the maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: {value} must be above {schema['exclusiveMinimum']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} items")
        if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            errors.append(f"{path}: items must be unique")
        if "items" in schema:
            for index, item in enumerate(value):
                errors.extend(_check(item, schema["items"], root, f"{path}[{index}]"))

    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}: missing required field {name!r}")
        properties = schema.get("properties", {})
        for name, child in value.items():
            if name in properties:
                errors.extend(_check(child, properties[name], root, f"{path}.{name}"))
            else:
                extra = schema.get("additionalProperties", True)
                if extra is False:
                    errors.append(f"{path}: unknown field {name!r}")
                elif isinstance(extra, dict):
                    errors.extend(_check(child, extra, root, f"{path}.{name}"))

    if "anyOf" in schema:
        if not any(not _check(value, branch, root, path) for branch in schema["anyOf"]):
            errors.append(f"{path}: matches none of the allowed shapes")

    if "oneOf" in schema:
        results = [_check(value, branch, root, path) for branch in schema["oneOf"]]
        matches = sum(1 for result in results if not result)
        if matches == 0:
            closest = min(results, key=len)
            errors.extend(closest if closest else [f"{path}: matches none of the allowed shapes"])
        elif matches > 1:
            errors.append(f"{path}: matches more than one allowed shape")

    if "if" in schema:
        if not _check(value, schema["if"], root, path):
            if "then" in schema:
                errors.extend(_check(value, schema["then"], root, path))
        elif "else" in schema:
            errors.extend(_check(value, schema["else"], root, path))

    return errors
