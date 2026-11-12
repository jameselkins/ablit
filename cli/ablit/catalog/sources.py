"""Check that every number in `profile` has a source URL."""

from __future__ import annotations

from fnmatch import fnmatchcase
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit


def profile_numbers(profile: dict[str, Any], prefix: str = "profile") -> list[str]:
    """Return the dotted path of every numeric value in the profile."""
    paths: list[str] = []
    for key, value in profile.items():
        path = f"{prefix}.{key}"
        if isinstance(value, dict):
            paths.extend(profile_numbers(value, path))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            paths.append(path)
    return sorted(paths)


def source_urls(sources: list[Any]) -> list[str]:
    urls: list[str] = []
    for item in sources:
        urls.append(item if isinstance(item, str) else item.get("url", ""))
    return urls


def uncovered_numbers(entry: dict[str, Any]) -> list[str]:
    """Return the profile numbers that no source covers.

    A source entry is either a bare URL, which cites the entry as a whole, or an
    object with `url` and `covers`. A `covers` item is a dotted path such as
    `profile.gpu.count`, and may end in `*` to match a group.
    """
    patterns: list[str] = []
    for item in entry.get("sources", []):
        if isinstance(item, dict):
            patterns.extend(item.get("covers", []))
    missing = []
    for path in profile_numbers(entry.get("profile", {})):
        if not any(fnmatchcase(path, pattern) for pattern in patterns):
            missing.append(path)
    return missing


def _public_https_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".")
        _ = parsed.port  # Reject invalid port text and numbers.
    except ValueError:
        return False
    if parsed.scheme != "https" or not host or any(character.isspace() for character in host):
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        address = ip_address(host)
    except ValueError:
        # Keep refusing shorthand loopback addresses such as 127.1.
        return not (host.startswith("127.") and all(character in "0123456789." for character in host))
    return address.is_global and not address.is_multicast


def bad_source_urls(entry: dict[str, Any]) -> list[str]:
    """Return sources that are not public https URLs."""
    return [url for url in source_urls(entry.get("sources", [])) if not _public_https_url(url)]
