"""Referral disclosure text and the first-run choices that use it.

Nothing in the planner or the backend drivers can import this package. A
contract in .importlinter enforces that, so referral data cannot change which
machine ablit picks.

Links appear only in `ablit setup`, only for a marketplace where the user has no
account, and only next to the disclosure. When a project ID is empty, the paid
choice is not offered at all.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources

MARKETPLACES = ("clore", "runpod", "vast")  # alphabetical, the order setup lists them

DISPLAY_NAMES = {"clore": "Clore.ai", "vast": "Vast.ai", "runpod": "RunPod"}

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

VAST_DISCLOSURE = """\
Paid link: if you create a new Vast.ai account through the first link below,
the ablit maintainer earns 3% of what you spend there for the life of your
account, paid by Vast as credits. ablit adds nothing to your prices.
Commissions never affect which machine ablit picks. To hide paid links, run
`ablit setup --no-referral`.
  Sign up (paid link):   https://cloud.vast.ai/?ref_id=<ID>
  Sign up (no referral): https://cloud.vast.ai/
"""

RUNPOD_DISCLOSURE = """\
Paid link: if you create a new RunPod account through the first link below,
the ablit maintainer earns a commission of 3% of what you spend on Pods and 5%
of what you spend on Serverless, paid by RunPod to the maintainer in RunPod
credits for 6 months. The new account must sign up with Google sign-in. ablit
adds nothing to your prices. Commissions never affect which machine ablit picks.
To hide paid links, run `ablit setup --no-referral`.
  Sign up (paid link):   https://runpod.io?ref=<ID>
  Sign up (no referral): https://www.runpod.io/
"""

# Terms as Clore's referral program page states them on 2026-10-21:
# https://docs.clore.ai/getting-started/deposit-withdrawal/referral-program
# The commission is a share of the fees Clore charges the referred renter, not of
# the rental price. The standard share is 15%, and a limited-time promotion pays
# 50% to all referrers with no end date given. Clore pays it from its own fee. The
# page does not say how long a referred user keeps earning, so the text says so.
# The renter fee of 5% is from https://docs.clore.ai/for-renters/fee-structure,
# which makes the commission about 0.7% of spend at 15% and about 2.4% at 50%.
CLORE_DISCLOSURE = """\
Paid link: if you create a new Clore.ai account through the first link below,
the ablit maintainer earns a share of the fees Clore charges you. The share is
15%, or 50% during a promotion that Clore says is limited-time and gives no end
date for. At Clore's 5% renter fee that is about 0.7% of what you spend, or about
2.4% during the promotion. Clore pays it from its own fee, and its page does not
say how long it lasts. ablit adds nothing to your prices. Commissions never
affect which machine ablit picks. To hide paid links, run `ablit setup --no-referral`.
  Sign up (paid link):   https://clore.ai/login?ref_id=<ID>
  Sign up (no referral): https://clore.ai/
"""

_TEMPLATES = {"clore": CLORE_DISCLOSURE, "vast": VAST_DISCLOSURE, "runpod": RUNPOD_DISCLOSURE}


def load_ids(text: str | None = None) -> dict[str, str]:
    """Read the project referral IDs. Missing or empty values come back as ''."""
    if text is None:
        text = resources.files("ablit.referral").joinpath("ids.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text)
    ids: dict[str, str] = {}
    for name in MARKETPLACES:
        value = str(data.get(name, {}).get("id", "")).strip()
        if value and not _ID_PATTERN.match(value):
            raise ValueError(f"the referral ID for {name} has characters that are not allowed in a link")
        ids[name] = value
    return ids


def referral_enabled(env: Mapping[str, str] | None = None, config_value: bool | None = None) -> bool:
    """Return False when the user turned referral links off.

    `ablit setup --no-referral`, ABLIT_NO_REFERRAL=1 and `referral = false` in
    the config each turn them off.
    """
    env = os.environ if env is None else env
    if env.get("ABLIT_NO_REFERRAL", "").strip() not in ("", "0"):
        return False
    return config_value is not False


def disclosure(marketplace: str, ids: Mapping[str, str]) -> str | None:
    """Return the disclosure with the ID filled in, or None when no ID is set."""
    project_id = ids.get(marketplace, "")
    if not project_id:
        return None
    return _TEMPLATES[marketplace].replace("<ID>", project_id)


@dataclass(frozen=True)
class Choice:
    key: str
    label: str


def setup_choices(
    marketplace: str, ids: Mapping[str, str], enabled: bool = True
) -> tuple[str | None, list[Choice]]:
    """Return the disclosure text and the choices for a user with no account.

    The three choices carry equal weight. Without a project ID, or with
    referral links turned off, there is no disclosure and no paid choice.
    """
    text = disclosure(marketplace, ids) if enabled else None
    if text is None:
        return None, [Choice("2", "Sign up directly"), Choice("3", "Skip")]
    return text, [
        Choice("1", "Sign up with the paid link"),
        Choice("2", "Sign up directly"),
        Choice("3", "Skip"),
    ]
