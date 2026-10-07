"""Title matching.

Two scores, on purpose. ``same_proposition`` is strict and is the only
score allowed to treat two venues as one price. ``related`` is looser and
is how the book refuses a second copy of a risk it already holds.
"""

from __future__ import annotations

import re

_STOP = frozenset(
    """
    a an the of to for on in at by be is are was were this that it from or and
    with as if than then into about will market contract price be above below
    """.split()
)
# "above" and "below" stay. They are the trade.
_STOP = frozenset(w for w in _STOP if w not in {"above", "below"})

_MONTHS = {
    "jan": "january",
    "feb": "february",
    "mar": "march",
    "apr": "april",
    "jun": "june",
    "jul": "july",
    "aug": "august",
    "sep": "september",
    "sept": "september",
    "oct": "october",
    "nov": "november",
    "dec": "december",
}
_ALIASES = {"btc": "bitcoin", "eth": "ethereum", "xbt": "bitcoin"}
_NEGATION = frozenset({"no", "not", "never", "without"})
_YESNO = frozenset({"yes", "no", "y", "n"})


def _canonicalize(text: str) -> str:
    raw = text.lower().replace(",", "")
    raw = raw.replace("&", " and ")
    raw = re.sub(r"[^a-z0-9.\s]", " ", raw)
    parts = []
    for token in raw.split():
        token = _ALIASES.get(token, token)
        token = _MONTHS.get(token, token)
        parts.append(token)
    return " ".join(parts)


def _numbers(text: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?", _canonicalize(text))


def _years(nums: list[str]) -> set[str]:
    out = set()
    for n in nums:
        if n.isdigit() and 2020 <= int(n) <= 2100:
            out.add(n)
    return out


def _days(nums: list[str]) -> set[str]:
    out = set()
    for n in nums:
        if n.isdigit() and "." not in n and 1 <= int(n) <= 31 and not (2020 <= int(n) <= 2100):
            out.add(n)
    return out


def _levels(nums: list[str]) -> set[str]:
    out = set()
    for n in nums:
        if n in _years(nums) or n in _days(nums):
            continue
        out.add(n)
    return out


def tokens(text: str, *, drop_numbers: bool = False) -> frozenset[str]:
    kept = []
    for token in _canonicalize(text).split():
        if token in _STOP:
            continue
        if drop_numbers and re.fullmatch(r"\d+(?:\.\d+)?", token):
            continue
        if len(token) < 2 and not token.isdigit():
            continue
        kept.append(token)
    return frozenset(kept)


def proposition(title: str, outcome: str) -> str:
    if outcome.strip().lower() in _YESNO:
        return title
    return f"{title} {outcome}"


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    shared = a & b
    if not shared:
        return 0.0
    return len(shared) / len(a | b)


def same_proposition(title_a: str, outcome_a: str, side_a: str, title_b: str, outcome_b: str, side_b: str) -> float:
    """Return a similarity in 0..1. Zero means 'do not treat these as one price'."""
    if side_a != side_b:
        return 0.0
    text_a = proposition(title_a, outcome_a)
    text_b = proposition(title_b, outcome_b)
    nums_a = _numbers(text_a)
    nums_b = _numbers(text_b)
    if _years(nums_a) != _years(nums_b):
        return 0.0
    if _days(nums_a) != _days(nums_b):
        return 0.0
    if _levels(nums_a) != _levels(nums_b):
        return 0.0
    words_a = tokens(text_a)
    words_b = tokens(text_b)
    if bool(words_a & _NEGATION) != bool(words_b & _NEGATION):
        return 0.0
    if len(words_a & words_b) < 3:
        return 0.0
    return _jaccard(words_a, words_b)


def related(title_a: str, outcome_a: str, event_a: str, venue_a: str, title_b: str, outcome_b: str, event_b: str, venue_b: str) -> float:
    """Looser overlap used to keep the book from holding the same risk twice."""
    if venue_a == venue_b and event_a and event_a == event_b:
        return 1.0
    words_a = tokens(proposition(title_a, outcome_a), drop_numbers=True)
    words_b = tokens(proposition(title_b, outcome_b), drop_numbers=True)
    return _jaccard(words_a, words_b)
