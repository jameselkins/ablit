"""Match GPU model names across the spellings the marketplaces use.

Each marketplace names the same GPU differently. Vast says "RTX 5090" and
"RTX PRO 6000 WS", RunPod says "RTX 5090" and "RTX PRO 6000 Blackwell
Workstation Edition", Clore keeps NVIDIA's name, such as "NVIDIA GeForce RTX
5090", glues some parts together ("Tesla V100SXM216GB") and cuts some long names
short ("RTX PRO 6000 Blackwell Server Edit"). Lium uses short names, such as
"RTX 5090". The drivers put those names in `Offer.gpu_model` as they are, apart
from Clore, which strips the "NVIDIA GeForce " and "NVIDIA " prefixes.

`gpu_name_key` reads a name as two parts.

1. The model words name the card. Letters and digits are split, so "RTX5090",
   "rtx 5090" and "NVIDIA GeForce RTX 5090" all give ("5090",), and "L40S" gives
   ("l", "40", "s"). Vendor and product line words, such as "NVIDIA", "GeForce"
   and "RTX", are dropped.
2. The form words name an edition or a form of the same card, such as
   "Workstation", "Server", "Max-Q", "PCIe", "SXM" or a memory size like "80GB".
   Short forms such as "WS", "S", "SE" and Clore's cut "Works" become "workstation" and "server".

A name the user gives matches an offer when the model words are the same, and
every form word the user gave is also in the offer's name. "L40" therefore does
not match "L40S", and "4090" does not match "4090 D". "RTX PRO 6000" matches
every edition of that card, and "RTX PRO 6000 Workstation" matches only the
workstation editions.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Words that name a vendor or a product line, or that pad a name. They are dropped.
# "blackwell" is dropped because the RTX PRO cards are all Blackwell and only some names say so.
_DROPPED = frozenset({
    "nvidia", "geforce", "tesla", "quadro", "amd", "rtx", "gtx",
    "blackwell", "edition", "edit", "generation", "gpu", "hbm",
})

# Whole words that name an edition or a form of the same card.
_FORMS = {
    "workstation": "workstation", "ws": "workstation", "works": "workstation",
    "server": "server", "s": "server", "se": "server",
    "maxq": "maxq", "pcie": "pcie", "sxm": "sxm", "sff": "sff",
}

_WORD = re.compile(r"[a-z0-9]+")
_LETTERS_OR_DIGITS = re.compile(r"[a-z]+|[0-9]+")


def gpu_name_key(name: str) -> tuple[tuple[str, ...], frozenset[str]]:
    """Return the model words, in order, and the set of form words of a GPU name."""
    text = name.lower()
    text = re.sub(r"max[\s_-]*q\b", "maxq", text)
    # Clore glues the form to the model and the memory size: "v100sxm216gb", "a100sxm440gb".
    text = re.sub(r"(sxm|hbm)(\d)(\d+gb)\b", r" \1\2 \3", text)
    text = re.sub(r"(sxm|hbm)(\d*)", r" \1\2 ", text)
    model: list[str] = []
    forms: set[str] = set()
    for word in _WORD.findall(text):
        if word in _FORMS:
            forms.add(_FORMS[word])
        elif re.fullmatch(r"(sxm|hbm)\d*", word):
            if word.startswith("sxm"):
                forms.add("sxm")
        elif re.fullmatch(r"\d+gb", word):
            forms.add(word)
        elif word == "gb" and model and model[-1].isdigit():
            forms.add(model.pop() + "gb")
        else:
            model.extend(part for part in _LETTERS_OR_DIGITS.findall(word) if part not in _DROPPED)
    return tuple(model), frozenset(forms)


def gpu_matches(gpu_model: str, names: Iterable[str]) -> bool:
    """True when `gpu_model` is the card that at least one of `names` names."""
    model, forms = gpu_name_key(gpu_model)
    for name in names:
        wanted_model, wanted_forms = gpu_name_key(name)
        if wanted_model and wanted_model == model and wanted_forms <= forms:
            return True
    return False


def parse_gpu_names(values: Iterable[str] | None) -> tuple[str, ...]:
    """Split repeated, comma-separated flag values into a tuple of names, without blanks or repeats.

    Raises ValueError for a name that names no model, such as "NVIDIA" or "80GB".
    """
    names: list[str] = []
    for value in values or ():
        for part in value.split(","):
            name = part.strip()
            if not name:
                continue
            if not gpu_name_key(name)[0]:
                raise ValueError(f"{name!r} does not name a GPU model")
            if name not in names:
                names.append(name)
    return tuple(names)
