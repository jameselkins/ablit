"""Match GPU model names across the spellings the marketplaces use.

Each marketplace names the same GPU differently. Vast says "RTX 5090" and
"RTX PRO 6000 WS", RunPod says "RTX 5090" and "RTX PRO 6000 Blackwell
Workstation Edition", Clore keeps NVIDIA's name, such as "NVIDIA GeForce RTX
5090", and cuts some long names short ("RTX PRO 6000 Blackwell Server Edit").
Lium uses short names, such as "RTX 5090". The drivers put those names in
`Offer.gpu_model` as they are, apart from Clore, which strips the "NVIDIA
GeForce " and "NVIDIA " prefixes.

To compare names, `gpu_name_words` turns a name into a tuple of lowercase words.
It drops vendor and filler words, expands a few short forms that only Vast uses,
and splits letters from digits, so "RTX5090", "rtx 5090" and "NVIDIA GeForce RTX
5090" all become ("rtx", "5090"). A name the user gives matches an offer when its
words appear in a row, in order, among the offer's words. "5090" therefore
matches "RTX 5090", and "RTX PRO 6000" matches every edition of that card. A
short name also matches longer models that contain it, such as "L40" and "L40S",
and `--exclude-gpu` drops the one that is not wanted.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Words that name a vendor, a product line or a generation, or that pad a name.
# Dropping them lets the marketplaces' spellings meet. "blackwell" is dropped
# because the RTX PRO cards are all Blackwell and only some names say so.
_DROPPED = frozenset({"nvidia", "geforce", "tesla", "amd", "edition", "edit", "generation", "blackwell"})

# Whole words that Vast writes short. A word is expanded before letters and
# digits are split, so the "s" in "L40S" is not read as "server".
_EXPANDED = {
    "ws": ("workstation",),
    "s": ("server",),
    "maxq": ("max", "q"),
}

_WORD = re.compile(r"[a-z0-9]+")
_LETTERS_OR_DIGITS = re.compile(r"[a-z]+|[0-9]+")


def gpu_name_words(name: str) -> tuple[str, ...]:
    """Return the words used to compare a GPU name, in order."""
    words: list[str] = []
    for word in _WORD.findall(name.lower()):
        for expanded in _EXPANDED.get(word, (word,)):
            words.extend(_LETTERS_OR_DIGITS.findall(expanded))
    return tuple(w for w in words if w not in _DROPPED)


def _contains(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    size = len(needle)
    return size > 0 and any(haystack[i : i + size] == needle for i in range(len(haystack) - size + 1))


def gpu_matches(gpu_model: str, names: Iterable[str]) -> bool:
    """True when `gpu_model` matches at least one of `names`."""
    model = gpu_name_words(gpu_model)
    return any(_contains(model, gpu_name_words(name)) for name in names)


def parse_gpu_names(values: Iterable[str] | None) -> tuple[str, ...]:
    """Split repeated, comma-separated flag values into a tuple of names, without blanks or repeats.

    Raises ValueError for a name that has no words left to compare, such as "NVIDIA".
    """
    names: list[str] = []
    for value in values or ():
        for part in value.split(","):
            name = part.strip()
            if not name:
                continue
            if not gpu_name_words(name):
                raise ValueError(f"{name!r} does not name a GPU model")
            if name not in names:
                names.append(name)
    return tuple(names)
