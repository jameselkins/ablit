from __future__ import annotations

import re
from pathlib import Path

import pytest

from ablit import referral
from ablit.notices import SPEND_CAP_NOTICE

EMPTY = '[vast]\nid = ""\n[runpod]\nid = ""\n[clore]\nid = ""\n'
SET = '[vast]\nid = "VAST-ID_01"\n[runpod]\nid = "runpodid01"\n[clore]\nid = "cloreid01"\n'


def test_shipped_ids_file_has_only_the_vast_id():
    ids = referral.load_ids()
    assert ids == {"clore": "", "runpod": "", "vast": "755440"}
    text = (Path(referral.__file__).parent / "ids.toml").read_text(encoding="utf-8")
    assert re.search(r'^\[runpod\]\s*\nid = ""', text, re.M)
    assert re.search(r'^\[clore\]\s*\nid = ""', text, re.M)


def test_shipped_vast_id_shows_the_disclosure_next_to_the_paid_link():
    text, choices = referral.setup_choices("vast", referral.load_ids())
    assert text is not None and "https://cloud.vast.ai/?ref_id=755440" in text
    assert text.index("Paid link:") < text.index("ref_id=")
    assert [c.key for c in choices] == ["1", "2", "3"]


def test_every_disclosure_says_how_to_hide_paid_links():
    for text in (referral.VAST_DISCLOSURE, referral.RUNPOD_DISCLOSURE, referral.CLORE_DISCLOSURE):
        assert "ablit setup --no-referral" in text


def test_vast_disclosure_matches_the_policy_exactly(repo_root: Path):
    policy = (repo_root / "POLICY.md").read_text(encoding="utf-8")
    assert referral.VAST_DISCLOSURE in policy, "the Vast disclosure differs from POLICY.md"


def test_spend_cap_notice_matches_the_documentation_exactly(repo_root: Path):
    assert SPEND_CAP_NOTICE in (repo_root / "docs" / "cli-reference.md").read_text(encoding="utf-8")
    assert SPEND_CAP_NOTICE in (repo_root / "README.md").read_text(encoding="utf-8")


def test_runpod_disclosure_carries_the_rates_and_conditions():
    text = referral.RUNPOD_DISCLOSURE
    for phrase in ("3% of what you spend on Pods", "5%", "Serverless", "RunPod credits", "6 months",
                   "Google sign-in", "ablit adds nothing to your prices",
                   "Commissions never affect which machine ablit picks"):
        assert " ".join(text.split()).find(phrase) != -1, phrase


def test_clore_disclosure_carries_the_terms_from_the_program_page():
    text = " ".join(referral.CLORE_DISCLOSURE.split())
    for phrase in ("Paid link:", "15%", "50%", "limited-time", "gives no end date", "about 0.7%", "about 2.4%",
                   "Clore pays it from its own fee", "does not say how long it lasts",
                   "ablit adds nothing to your prices", "Commissions never affect which machine ablit picks"):
        assert phrase in text, phrase
    assert "https://docs.clore.ai/getting-started/deposit-withdrawal/referral-program" in Path(
        referral.__file__).read_text(encoding="utf-8")


def test_clore_paid_choice_appears_only_with_an_id():
    assert referral.disclosure("clore", referral.load_ids(EMPTY)) is None
    text, choices = referral.setup_choices("clore", referral.load_ids(SET))
    assert "https://clore.ai/login?ref_id=cloreid01" in text and "<ID>" not in text
    assert "Sign up (no referral): https://clore.ai/\n" in text
    assert [c.key for c in choices] == ["1", "2", "3"]
    assert referral.DISPLAY_NAMES["clore"] == "Clore.ai"


def test_id_is_filled_into_the_link_and_nowhere_else():
    ids = referral.load_ids(SET)
    vast = referral.disclosure("vast", ids)
    assert "https://cloud.vast.ai/?ref_id=VAST-ID_01" in vast
    assert "<ID>" not in vast
    assert "Sign up (no referral): https://cloud.vast.ai/\n" in vast
    runpod = referral.disclosure("runpod", ids)
    assert "https://runpod.io?ref=runpodid01" in runpod and "<ID>" not in runpod


def test_empty_id_means_no_paid_choice():
    ids = referral.load_ids(EMPTY)
    assert referral.disclosure("vast", ids) is None
    text, choices = referral.setup_choices("vast", ids)
    assert text is None
    assert [c.key for c in choices] == ["2", "3"]
    assert [c.label for c in choices] == ["Sign up directly", "Skip"]


def test_with_an_id_there_are_three_choices_of_equal_weight():
    text, choices = referral.setup_choices("vast", referral.load_ids(SET))
    assert text.startswith("Paid link:")
    assert [(c.key, c.label) for c in choices] == [
        ("1", "Sign up with the paid link"), ("2", "Sign up directly"), ("3", "Skip")]


def test_turning_referrals_off_removes_the_disclosure_and_the_paid_choice():
    text, choices = referral.setup_choices("vast", referral.load_ids(SET), enabled=False)
    assert text is None and [c.key for c in choices] == ["2", "3"]


def test_marketplaces_are_listed_alphabetically():
    assert list(referral.MARKETPLACES) == sorted(referral.MARKETPLACES)


@pytest.mark.parametrize("env,config,expected", [
    ({}, None, True),
    ({"ABLIT_NO_REFERRAL": "1"}, None, False),
    ({"ABLIT_NO_REFERRAL": "0"}, None, True),
    ({}, False, False),
    ({}, True, True),
])
def test_referral_switches(env, config, expected):
    assert referral.referral_enabled(env, config) is expected


@pytest.mark.parametrize("bad", ['a b', 'x"y', "a/b", "a?b=c", "a&b", "x" * 65])
def test_ids_that_could_break_a_link_are_rejected(bad):
    with pytest.raises(ValueError):
        referral.load_ids(f'[vast]\nid = "{bad}"\n')


def _text_files(root: Path):
    # Agent worktrees under .claude/worktrees are other checkouts, not this one.
    skip_dirs = {".git", ".venv", ".hypothesis", ".pytest_cache", "__pycache__", "dist", "node_modules", "worktrees"}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in skip_dirs for part in path.relative_to(root).parts):
            continue
        try:
            yield path, path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue


def test_every_referral_link_sits_next_to_the_disclosure(repo_root: Path):
    pattern = re.compile(r"ref_id=|\?ref=")
    this_file = Path(__file__).resolve()
    found = 0
    for path, text in _text_files(repo_root):
        if path.resolve() == this_file:
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if pattern.search(line):
                found += 1
                window = lines[max(0, index - 8): index + 1]
                assert any("Paid link:" in w for w in window), f"{path}:{index + 1} has a referral link with no disclosure"
    assert found >= 3  # the Vast, RunPod and Clore templates in the referral package


def test_referral_links_are_not_built_anywhere_but_the_referral_package(cli_dir: Path):
    for path in (cli_dir / "ablit").rglob("*.py"):
        if "referral" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert "ref_id=" not in text and "?ref=" not in text, path
