#!/usr/bin/env python3
"""ablit watchdog. Standard library only, so one file can be copied to the box.

Every 30 seconds it computes spend from the start time, the hourly rate locked
at rent time, and the bytes received times the per-GB price. It destroys its own
instance when spend plus a reserve reaches the cap. It also destroys the
instance if no bytes arrive for 10 minutes during the download.

The reserve is 2 minutes of the hourly rate plus 2 minutes of transfer at the
advertised speed.

The decision logic is plain functions over numbers, so tests can run it without
a clock or a network. The destroy call is a stub.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, replace

INTERVAL_SECONDS = 30
RESERVE_MINUTES = 2.0
STALL_SECONDS = 600.0
HEARTBEAT_PATH = "/ablit/heartbeat.json"
RUN_FILE = "/ablit/run.json"

CONTINUE = "continue"
DESTROY_CAP = "destroy_cap"
DESTROY_STALL = "destroy_stall"


@dataclass(frozen=True)
class Config:
    """Values fixed at rent time."""

    started_at: float  # seconds since the epoch
    hourly_rate: float  # dollars per hour, locked at rent time
    transfer_price_per_gb: float  # dollars per decimal GB received
    cap_usd: float
    advertised_mbps: float  # used only for the reserve
    baseline_rx_bytes: int = 0  # bytes already counted when the run started
    stall_seconds: float = STALL_SECONDS
    reserve_minutes: float = RESERVE_MINUTES


@dataclass(frozen=True)
class State:
    """What the watchdog remembers between checks."""

    last_rx_bytes: int
    last_progress_at: float


@dataclass(frozen=True)
class Decision:
    action: str
    spend: float
    reserve: float
    reason: str
    state: State


def spend(cfg: Config, now: float, rx_bytes: int) -> float:
    """Dollars spent so far, from time on the machine plus bytes received."""
    hours = max(now - cfg.started_at, 0.0) / 3600.0
    received_gb = max(rx_bytes - cfg.baseline_rx_bytes, 0) / 1e9
    return cfg.hourly_rate * hours + cfg.transfer_price_per_gb * received_gb


def reserve(cfg: Config) -> float:
    """Dollars held back so that the destroy call finishes before the cap."""
    minutes = cfg.reserve_minutes
    compute = cfg.hourly_rate * minutes / 60.0
    bytes_per_second = cfg.advertised_mbps * 1e6 / 8.0
    transfer = cfg.transfer_price_per_gb * bytes_per_second * minutes * 60.0 / 1e9
    return compute + transfer


def initial_state(now: float, rx_bytes: int) -> State:
    return State(last_rx_bytes=rx_bytes, last_progress_at=now)


def decide(
    cfg: Config, state: State, now: float, rx_bytes: int, downloading: bool
) -> Decision:
    """Decide what to do at this check. The cap check comes before the stall check."""
    if rx_bytes > state.last_rx_bytes:
        state = State(last_rx_bytes=rx_bytes, last_progress_at=now)
    else:
        state = replace(state, last_rx_bytes=max(state.last_rx_bytes, rx_bytes))

    spent = spend(cfg, now, rx_bytes)
    held = reserve(cfg)

    if spent + held >= cfg.cap_usd:
        return Decision(
            DESTROY_CAP, spent, held,
            f"spend ${spent:.4f} plus reserve ${held:.4f} reached the cap ${cfg.cap_usd:.4f}",
            state,
        )
    idle = now - state.last_progress_at
    if downloading and idle >= cfg.stall_seconds:
        return Decision(
            DESTROY_STALL, spent, held,
            f"no bytes arrived for {idle:.0f} seconds during the download",
            state,
        )
    return Decision(CONTINUE, spent, held, "within the cap", state)


def seconds_until_cap(cfg: Config, now: float, rx_bytes: int) -> float | None:
    """Seconds until spend plus reserve reaches the cap at the hourly rate alone.

    Returns None when the hourly rate is zero. Transfer charges that have not
    happened yet are not predicted.
    """
    if cfg.hourly_rate <= 0:
        return None
    room = cfg.cap_usd - spend(cfg, now, rx_bytes) - reserve(cfg)
    return max(room, 0.0) / cfg.hourly_rate * 3600.0


def read_rx_bytes(path: str = "/proc/net/dev") -> int:
    """Total bytes received on every interface except loopback."""
    total = 0
    with open(path, encoding="utf-8") as handle:
        for line in handle.readlines()[2:]:
            name, _, rest = line.partition(":")
            if name.strip() == "lo" or not rest:
                continue
            total += int(rest.split()[0])
    return total


def destroy_instance(reason: str) -> None:
    """Destroy this instance through the marketplace API.

    TODO(step 3): untested. The call needs the credential that is limited to
    this one instance, which `Backend.watchdog_credential` creates. The
    watchdog must never hold the user's full account key.
    """
    raise NotImplementedError("destroy_instance is not written yet")


def _load_config(path: str) -> Config:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return Config(
        started_at=float(data["started_at"]),
        hourly_rate=float(data["hourly_rate"]),
        transfer_price_per_gb=float(data["transfer_price_per_gb"]),
        cap_usd=float(data["cap_usd"]),
        advertised_mbps=float(data["advertised_mbps"]),
        baseline_rx_bytes=int(data.get("baseline_rx_bytes", 0)),
    )


def _download_active(stages_dir: str) -> bool:
    """The download counts as active until its stage marker exists."""
    import os

    return not os.path.exists(os.path.join(stages_dir, "05-download.done"))


def run(cfg: Config, stages_dir: str = "/ablit/stages") -> int:
    """Check every 30 seconds until a destroy decision is made."""
    state = initial_state(time.time(), read_rx_bytes())
    while True:
        now = time.time()
        decision = decide(cfg, state, now, read_rx_bytes(), _download_active(stages_dir))
        state = decision.state
        with open(HEARTBEAT_PATH, "w", encoding="utf-8") as handle:
            json.dump({"at": now, "spend": decision.spend, "action": decision.action}, handle)
        if decision.action != CONTINUE:
            print(decision.reason, file=sys.stderr, flush=True)
            destroy_instance(decision.reason)
            return 8
        time.sleep(INTERVAL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ablit spend-cap watchdog")
    parser.add_argument("--config", default=RUN_FILE, help="JSON file written at bootstrap")
    parser.add_argument("--once", action="store_true", help="print one decision and exit")
    parser.add_argument("--destroy-now", action="store_true", help="destroy this instance and exit")
    args = parser.parse_args(argv)

    if args.destroy_now:
        destroy_instance("requested by bootstrap")
        return 0
    cfg = _load_config(args.config)
    if args.once:
        now = time.time()
        rx = read_rx_bytes()
        print(json.dumps(decide(cfg, initial_state(now, rx), now, rx, True).__dict__, default=str))
        return 0
    return run(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
