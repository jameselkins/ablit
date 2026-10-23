"""HTTP steps that the drivers share: retry with waits, request pacing, and redirects.

Each driver decides what its answers mean. These helpers only send a request
again when it is safe to, wait between tries, and refuse a redirect. A caller
that must not repeat a request passes `attempts=1`.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from ablit.backends.errors import RequestRejected


def backoff(attempt: int) -> float:
    """Seconds to wait after the given failed try: 2, 4, 8, and so on, up to 30."""
    return min(2.0 * 2 ** attempt, 30.0)


def rate_limited_or_failed(response: httpx.Response) -> bool:
    """True for HTTP 429 and for any server error."""
    return response.status_code == 429 or response.status_code >= 500


def _backoff_delay(response: httpx.Response, attempt: int) -> float:
    return backoff(attempt)


def send_with_retry(
    send: Callable[[], httpx.Response],
    *,
    attempts: int,
    sleep: Callable[[float], None],
    delay: Callable[[httpx.Response, int], float] = _backoff_delay,
    limited: Callable[[httpx.Response], bool] = rate_limited_or_failed,
    before: Callable[[], None] | None = None,
) -> httpx.Response:
    """Call `send` up to `attempts` times and return the last answer.

    A network failure or an answer that `limited` accepts is tried again after a
    wait, except on the last try. A network failure on the last try is raised,
    and a limited answer on the last try is returned for the caller to judge.
    `before` runs ahead of every try.
    """
    response: httpx.Response | None = None
    for attempt in range(attempts):
        if before is not None:
            before()
        try:
            response = send()
        except httpx.TransportError:
            # A network failure before any answer.
            if attempt + 1 >= attempts:
                raise
            sleep(backoff(attempt))
            continue
        if not limited(response):
            break
        if attempt + 1 < attempts:
            sleep(delay(response, attempt))
    assert response is not None
    return response


def pace(last: float | None, interval: float, sleep: Callable[[float], None]) -> float:
    """Wait until `interval` seconds have passed since `last`, and return the new time stamp."""
    if last is not None:
        wait = interval - (time.monotonic() - last)
        if wait > 0:
            sleep(wait)
    return time.monotonic()


def refuse_redirect(backend: str, response: httpx.Response) -> None:
    """Raise when the marketplace answered with a redirect. A redirected write is not resent."""
    if response.is_redirect:
        raise RequestRejected(backend, response.status_code, "the API redirected the request; ablit does not follow it")
