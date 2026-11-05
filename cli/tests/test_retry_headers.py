"""Retry headers must produce a bounded delay or fall back to the normal backoff."""

import httpx
import pytest

from ablit.backends.lium import _retry_delay as lium_delay
from ablit.backends.runpod import _retry_delay as runpod_delay
from ablit.backends.vast import retry_delay as vast_delay


@pytest.mark.parametrize("delay", [vast_delay, runpod_delay, lium_delay], ids=["vast", "runpod", "lium"])
@pytest.mark.parametrize(("header", "expected"), [(b"0", 0.0), (b"7", 7.0), (b"120", 60.0), (b"\xb2", 4.0)],
                         ids=["zero", "seconds", "capped", "non_ascii_digit"])
def test_retry_after_seconds_require_ascii_digits(delay, header, expected):
    response = httpx.Response(429, headers=[(b"Retry-After", header)])
    assert delay(response, 1) == expected
