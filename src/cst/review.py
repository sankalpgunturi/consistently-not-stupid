"""Retrospective.

A heuristic governor always runs. When an OpenAI key is present, the model
reads the same tape and may suggest drops or one-step parameter moves.
The governor may only tighten a knob, one step, inside the rail. A model
suggestion that would let more risk in is ignored. The fee law is not a parameter.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from cst.models import (
    LOOSEN_DOWN,
    LOOSEN_UP,
    RAILS,
    STEPS,
    Proposal,
    Settlement,
    StrategyParams,
)

log = logging.getLogger("cst.review")


@dataclass
class Retro:
    summary: str
    applied: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    source: str = "governor"

    def to_json(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "applied": self.applied,
            "notes": self.notes,
            "source": self.source,
        }


def heuristic_updates(params: StrategyParams, settlements: list[Settlement]) -> dict[str, float]:
    """Tighten when the settled record does not clear the fee. Never loosen."""
    updates: dict[str, float] = {}
    recent = settlements[-30:]
    n = len(recent)
    if n < 8:
        return updates
    from cst.strategy import wilson_lower

    wins = sum(1 for row in recent if row.won)
    lower = wilson_lower(wins, n)
    avg_all_in = sum(row.price + row.fee_per_share for row in recent) / n
    if lower + 1e-9 < avg_all_in:
        updates["min_probability"] = params.min_probability + STEPS["min_probability"]
        updates["min_edge"] = params.min_edge + STEPS["min_edge"]
    return updates


def heuristic_summary(counts: dict[str, int], bought: int) -> str:
    if bought:
        noun = "clip" if bought == 1 else "clips"
        return f"Bought {bought} minimum {noun} that cleared the fee and a second price."
    favorites = counts.get("favorites", 0)
    if favorites == 0:
        return "No live quote had both sides of the book at the probability bar."
    if counts.get("confirmed", 0) == 0:
        return (
            "Favorites were on the board. None were cheap versus another venue or versus the settled record, "
            "so the book stayed in cash."
        )
    return "A price looked cheap, then the portfolio rules set it aside as a twin, a cap, or a pause."


def _loosening(key: str, old: float, new: float) -> bool:
    if key in LOOSEN_UP:
        return new > old + 1e-12
    if key in LOOSEN_DOWN:
        return new < old - 1e-12
    return False


def _tighter(key: str, a: float, b: float) -> float:
    if key in LOOSEN_UP:
        return min(a, b)
    if key in LOOSEN_DOWN:
        return max(a, b)
    return a


def govern(
    params: StrategyParams,
    proposed: dict[str, float],
    heuristic_keys: set[str],
    settlements: list[Settlement],
) -> tuple[StrategyParams, list[str], dict[str, float]]:
    """Move at most one step tighter. A settled record cannot loosen a knob."""
    del heuristic_keys, settlements
    notes: list[str] = []
    applied: dict[str, float] = {}
    data = params.to_json()
    for key, raw in proposed.items():
        if key not in RAILS or key not in data:
            notes.append(f"Ignored {key}. That knob is not open to the retrospective.")
            continue
        lo, hi = RAILS[key]
        old = float(data[key])
        step = STEPS[key]
        try:
            value = float(raw)
        except (TypeError, ValueError):
            notes.append(f"Ignored {key}. The suggestion was not a number.")
            continue
        if value > old + step:
            value = old + step
        elif value < old - step:
            value = old - step
        value = min(hi, max(lo, value))
        if isinstance(data[key], int) and key.endswith("_seconds") or key in {"min_stable_scans", "min_sample", "max_new_per_cycle", "scan_interval_seconds"}:
            value = int(round(value))
            old_cmp = int(data[key])
        else:
            old_cmp = old
            if key in {"min_stable_scans", "min_sample", "max_new_per_cycle"}:
                value = int(round(value))
        if abs(float(value) - float(old_cmp)) < 1e-12:
            continue
        if _loosening(key, float(old_cmp), float(value)):
            notes.append(f"Kept {key} at {old_cmp}. Overrides only tighten.")
            continue
        if key == "scan_interval_seconds":
            notes.append(f"Kept {key} at {old_cmp}. The scan clock is not a risk knob.")
            continue
        data[key] = value
        applied[key] = value
        notes.append(f"{key} moved from {old_cmp} to {value}.")
    return StrategyParams.from_json(data), notes, applied


def tighten_value(params: StrategyParams, key: str) -> float | int | None:
    """One step toward less risk, or None when the knob is closed or already at the rail."""
    if key not in RAILS or key not in STEPS or key == "scan_interval_seconds":
        return None
    if key not in LOOSEN_UP and key not in LOOSEN_DOWN:
        return None
    old = float(getattr(params, key))
    step = STEPS[key]
    new = old - step if key in LOOSEN_UP else old + step
    lo, hi = RAILS[key]
    new = min(hi, max(lo, new))
    if key in {"min_stable_scans", "min_sample", "max_new_per_cycle"}:
        new = int(round(new))
        old = int(round(old))
    if abs(float(new) - float(old)) < 1e-12:
        return None
    if _loosening(key, float(old), float(new)):
        return None
    return new


def merge_suggestions(heuristic: dict[str, float], model: dict[str, float]) -> dict[str, float]:
    merged = dict(heuristic)
    for key, value in model.items():
        if key in merged:
            try:
                merged[key] = _tighter(key, float(merged[key]), float(value))
            except (TypeError, ValueError):
                continue
        else:
            merged[key] = value
    return merged


class Reviewer:
    """One model call per scan. The governor, not the model, writes the parameters."""

    def __init__(self, api_key: str = "", model: str = "gpt-4o-mini"):
        self.api_key = api_key.strip()
        self.model = model

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def review(
        self,
        params: StrategyParams,
        proposals: list[Proposal],
        counts: dict[str, int],
        settlements: list[Settlement],
    ) -> tuple[dict[str, str], dict[str, float], str]:
        """Returns drop reasons by quote key, raw parameter suggestions, and a summary."""
        if not self.enabled:
            return {}, {}, ""
        payload = {
            "rules": [
                "Drop a buy only when it is the same risk or the logical opposite of another buy.",
                "A locked difference between two venues is a reason to keep the cheaper favorite, not to drop it.",
                "Do not suggest buying anything the desk skipped.",
                "parameter_updates may only use the provided knobs and should move by a small amount.",
                "Prefer fewer, clearer trades.",
            ],
            "knobs": {key: {"value": getattr(params, key), "rail": RAILS[key]} for key in RAILS},
            "counts": counts,
            "proposals": [
                {
                    "id": item.key,
                    "venue": item.quote.venue,
                    "title": item.quote.title,
                    "outcome": item.quote.outcome,
                    "ask": item.quote.ask,
                    "edge": round(item.edge, 4),
                    "signal": item.signal,
                    "detail": item.detail,
                }
                for item in proposals
            ],
            "recent_settlements": [
                {"bucket": row.bucket, "won": row.won, "pnl": round(row.pnl, 4)}
                for row in settlements[-15:]
            ],
        }
        try:
            raw = self._complete(payload)
        except Exception as exc:  # network, auth, parse — the governor still runs
            log.warning("retrospective model failed: %s", exc)
            return {}, {}, ""
        drops = {}
        for item in raw.get("drops") or []:
            if not isinstance(item, dict):
                continue
            ident = str(item.get("id") or "")
            reason = str(item.get("reason") or "The model marked this as the same risk as another buy.")
            if ident:
                drops[ident] = reason
        updates = raw.get("parameter_updates") or {}
        if not isinstance(updates, dict):
            updates = {}
        summary = str(raw.get("summary") or "").strip()
        return drops, {k: v for k, v in updates.items() if k in RAILS}, summary

    def _complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, timeout=25)
        response = client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You review a paper book of high-probability prediction-market buys. "
                        "Return JSON with keys summary (string), drops (list of {id, reason}), "
                        "parameter_updates (object), concerns (list of strings)."
                    ),
                },
                {"role": "user", "content": json.dumps(payload)},
            ],
        )
        text = response.choices[0].message.content or "{}"
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            return {}
        return parsed
