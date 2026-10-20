from __future__ import annotations

import json
import string
from urllib.parse import quote

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ablit.redact import MASK, Redactor, redact

ENDPOINT_KEY = "sk-endpoint-0123456789abcdef"
VAST_KEY = "vast_FAKEKEY_abcdef123456"
RUNPOD_KEY = "rpa_FAKEKEY_zyxwvu987654"


def test_removes_each_secret():
    text = f"Authorization: Bearer {ENDPOINT_KEY}\nvast={VAST_KEY} runpod={RUNPOD_KEY}"
    out = redact(text, [ENDPOINT_KEY, VAST_KEY, RUNPOD_KEY])
    for secret in (ENDPOINT_KEY, VAST_KEY, RUNPOD_KEY):
        assert secret not in out
    assert out.count(MASK) == 3


def test_removes_repeated_and_embedded_secrets():
    out = redact(f"{ENDPOINT_KEY}{ENDPOINT_KEY} x{VAST_KEY}y", [ENDPOINT_KEY, VAST_KEY])
    assert ENDPOINT_KEY not in out and VAST_KEY not in out


def test_removes_url_encoded_and_json_escaped_forms():
    secret = 'abc/def+ghi"jkl\\mno'
    assert quote(secret, safe="") != secret
    text = f"url=/x?key={quote(secret, safe='')} body={json.dumps({'k': secret})} raw={secret}"
    out = redact(text, [secret])
    assert quote(secret, safe="") not in out
    assert json.dumps(secret)[1:-1] not in out
    assert secret not in out


def test_longest_secret_wins_when_one_contains_another():
    inner = "token-abcdef"
    outer = "token-abcdef-extra-part"
    out = redact(f"value {outer} end", [inner, outer])
    assert out == f"value {MASK} end"


def test_empty_and_missing_secrets_are_ignored():
    assert redact("plain text", ["", None]) == "plain text"
    assert Redactor()("nothing to hide") == "nothing to hide"


def test_short_secrets_are_refused_instead_of_silently_skipped():
    with pytest.raises(ValueError):
        Redactor(["abc"])


def test_text_without_secrets_is_unchanged():
    text = "bootstrap stage 05 done in 12 seconds"
    assert redact(text, [ENDPOINT_KEY]) == text


def test_redactor_can_be_extended_after_creation():
    redactor = Redactor([ENDPOINT_KEY])
    redactor.add(VAST_KEY)
    assert VAST_KEY not in redactor(f"key {VAST_KEY}")


_secret_chars = string.ascii_letters + string.digits


@given(
    secrets=st.lists(st.text(_secret_chars, min_size=8, max_size=40).map(lambda s: "sk-" + s), min_size=1, max_size=4),
    pieces=st.lists(st.text(string.printable, max_size=30), min_size=0, max_size=6),
    data=st.data(),
)
def test_no_secret_survives(secrets, pieces, data):
    parts = list(pieces)
    for secret in secrets:
        parts.insert(data.draw(st.integers(0, len(parts))), secret)
    out = redact("".join(parts), secrets)
    for secret in secrets:
        assert secret not in out
