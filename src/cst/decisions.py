"""One Decisions API call per scan. It may drop a proposed clip. It cannot add one.

A clip leaves the set only when the answer named for that clip chooses drop
and the probability on drop is at least 0.6. A timeout, a missing key, a
refusal, a duplicate, or a body we cannot read leaves the deterministic set.
"""

from __future__ import annotations

import logging

import httpx

from cst.models import Position, Proposal

log = logging.getLogger("cst.decisions")

MODEL = "gpt-6-luna"
DROP_FLOOR = 0.6
ENDPOINT = "https://api.openai.com/v1/decisions"


def build_request(proposals: list[Proposal], positions: list[Position] | None = None) -> tuple[dict, dict[str, str]]:
    name_to_key: dict[str, str] = {}
    questions = []
    lines = []
    for index, proposal in enumerate(proposals):
        name = f"c{index}"
        name_to_key[name] = proposal.key
        quote = proposal.quote
        lines.append(
            f"{name}: {quote.venue} | {quote.title} | {quote.outcome} | side {quote.side} | "
            f"ask {quote.ask:.3f} | {proposal.signal} | {proposal.detail}"
        )
        questions.append({
            "type": "choice",
            "name": name,
            "instructions": (
                "Is this clip the same risk, or the logical opposite, of another proposed clip or an already-open position in the input? "
                "Choose drop only in that case. Choose keep when the favorite is a distinct contract."
            ),
            "choices": [
                {"value": "keep", "description": "Distinct risk. Leave the clip in the set."},
                {"value": "drop", "description": "Duplicate or logical opposite of a proposed clip or open position."},
            ],
        })
    held = [f"h{index}: {p.venue} | {p.title} | {p.outcome} | side {p.side} | {p.shares:g} contracts"
            for index, p in enumerate(positions or [])]
    context = "Already-open positions (context only; do not propose exits):\n" + ("\n".join(held) or "None")
    body = {
        "model": MODEL,
        "input": context + "\n\nProposed paper clips:\n" + "\n".join(lines),
        "questions": questions,
    }
    return body, name_to_key


def _drop_probability(answer: dict) -> float | None:
    raw = answer.get("probabilities")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("value") == "drop":
                try:
                    return float(item.get("probability"))
                except (TypeError, ValueError):
                    return None
        return None
    if isinstance(raw, dict) and "drop" in raw:
        try:
            return float(raw["drop"])
        except (TypeError, ValueError):
            return None
    return None


def parse_veto(body, name_to_key: dict[str, str]) -> dict[str, str]:
    """Map quote keys to drop reasons. Unknown, duplicate, and refused answers are ignored."""
    if not isinstance(body, dict):
        return {}
    answers = body.get("answers")
    if not isinstance(answers, list):
        return {}
    counts: dict[str, int] = {}
    for answer in answers:
        if isinstance(answer, dict) and answer.get("name"):
            name = str(answer["name"])
            counts[name] = counts.get(name, 0) + 1
    drops: dict[str, str] = {}
    for answer in answers:
        if not isinstance(answer, dict):
            continue
        name = str(answer.get("name") or "")
        if counts.get(name) != 1 or name not in name_to_key:
            continue
        if str(answer.get("type") or "") == "refusal":
            continue
        choice = answer.get("choice", answer.get("value"))
        if choice != "drop":
            continue
        probability = _drop_probability(answer)
        if probability is None or probability + 1e-12 < DROP_FLOOR:
            continue
        key = name_to_key[name]
        drops[key] = (
            f"The decisions call marked this clip as a duplicate or an opposite, "
            f"with drop probability {probability:.0%}."
        )
    return drops


def veto_proposals(proposals: list[Proposal], api_key: str, timeout: float = 20, usage_sink=None, positions: list[Position] | None = None) -> dict[str, str]:
    if not api_key.strip() or not proposals:
        return {}
    body, mapping = build_request(proposals, positions)
    try:
        response = httpx.post(
            ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key.strip()}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        log.warning("decisions call failed open: %s", exc)
        return {}
    if usage_sink is not None:
        usage_sink(payload.get("usage") if isinstance(payload, dict) else None)
    return parse_veto(payload, mapping)
