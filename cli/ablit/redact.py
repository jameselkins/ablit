"""Remove known secrets from text before it reaches a log, run file or report."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from urllib.parse import quote

MASK = "[redacted]"
MIN_SECRET_LENGTH = 6


def _forms(secret: str) -> set[str]:
    """Return the ways a secret can show up in text.

    A secret can appear as typed, percent-encoded in a URL, or escaped inside a
    JSON string.
    """
    forms = {secret, quote(secret, safe=""), json.dumps(secret)[1:-1]}
    return {form for form in forms if form}


class Redactor:
    """Replaces each registered secret with a fixed mask.

    Empty or missing secrets are ignored, because an unset key is normal. A
    secret that is non-empty but shorter than MIN_SECRET_LENGTH raises
    ValueError, because masking a short string would damage ordinary text and
    would give a false sense of safety.
    """

    def __init__(self, secrets: Iterable[str | None] = ()) -> None:
        self._forms: set[str] = set()
        self._pattern: re.Pattern[str] | None = None
        for secret in secrets:
            self.add(secret)

    def add(self, secret: str | None) -> None:
        if not secret:
            return
        if len(secret) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"secrets shorter than {MIN_SECRET_LENGTH} characters cannot be redacted safely"
            )
        self._forms |= _forms(secret)
        self._pattern = None

    def _compiled(self) -> re.Pattern[str] | None:
        if self._pattern is None and self._forms:
            # Longest first, so a secret that contains another secret is
            # removed whole.
            ordered = sorted(self._forms, key=lambda form: (-len(form), form))
            self._pattern = re.compile("|".join(re.escape(form) for form in ordered))
        return self._pattern

    def __call__(self, text: str) -> str:
        pattern = self._compiled()
        if pattern is None:
            return text
        return pattern.sub(MASK, text)


def redact(text: str, secrets: Iterable[str | None]) -> str:
    """Return `text` with every secret in `secrets` replaced by a mask."""
    return Redactor(secrets)(text)
