"""A small model of the rule, separate from the live paper book.

Two worlds. In the fair world the quote is the true chance, so a favorite
bought at the ask loses the fee. In the dislocated world some favorites are
a few cents under a second venue. This desk trades only there.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from cst.fees import polymarket_taker_fee


@dataclass
class PathResult:
    equity: float
    pnl: float
    fees: float
    trades: int
    wins: int
    losses: int
    curve: list[float]


def _one(rng: random.Random, strategy: str, world: str, n: int, bankroll: float) -> PathResult:
    cash = bankroll
    fees_paid = 0.0
    trades = wins = losses = 0
    curve = [bankroll]
    for i in range(n):
        true_q = 0.90 + rng.random() * 0.07
        if world == "dislocated" and i % 3 == 0:
            mid = max(0.90, true_q - 0.03)
            other_bid = min(0.985, true_q - 0.004)
        else:
            mid = true_q
            other_bid = mid - 0.01
        bid = mid - 0.005
        ask = mid + 0.005
        if bid < 0.90 or ask < 0.90 or ask >= 0.995:
            curve.append(cash)
            continue
        shares = 5
        fee = float(polymarket_taker_fee(shares, ask, 0.04, 1))
        per_share = fee / shares
        if (1 - ask - per_share) < 0.01:
            curve.append(cash)
            continue
        take = strategy == "naive"
        if strategy == "common":
            edge = other_bid - ask - per_share
            take = edge >= 0.01 and other_bid >= 0.90
        if not take:
            curve.append(cash)
            continue
        cost = shares * ask + fee
        if cost > cash:
            curve.append(cash)
            continue
        cash -= cost
        fees_paid += fee
        trades += 1
        if rng.random() < true_q:
            cash += shares
            wins += 1
        else:
            losses += 1
        curve.append(cash)
    return PathResult(cash, cash - bankroll, fees_paid, trades, wins, losses, curve)


def _summarize(paths: list[PathResult], bankroll: float) -> dict:
    n = len(paths)
    pnls = [item.pnl for item in paths]
    trades = [item.trades for item in paths]
    below = sum(1 for item in pnls if item < -1e-6)
    return {
        "paths": n,
        "mean_pnl": round(sum(pnls) / n, 2),
        "median_pnl": round(sorted(pnls)[n // 2], 2),
        "worst_pnl": round(min(pnls), 2),
        "mean_fees": round(sum(item.fees for item in paths) / n, 2),
        "mean_trades": round(sum(trades) / n, 2),
        "paths_under_start": round(below / n, 3),
        "mean_equity": round(bankroll + sum(pnls) / n, 2),
    }


def _downsample(curve: list[float], points: int = 40) -> list[float]:
    if len(curve) <= points:
        return [round(value, 2) for value in curve]
    step = (len(curve) - 1) / (points - 1)
    return [round(curve[int(round(i * step))], 2) for i in range(points)]


def run_report(seed: int = 7, paths: int = 200, markets: int = 90, bankroll: float = 1000) -> dict:
    specs = (
        ("naive", "fair", "Buy every 90% quote", "The quote is the true chance"),
        ("common", "fair", "Consistently Not Stupid", "The quote is the true chance"),
        ("common", "dislocated", "Consistently Not Stupid", "Some second venues are a few cents higher"),
    )
    books = []
    curves = {}
    for index, (strategy, world, name, world_name) in enumerate(specs):
        rng = random.Random(seed + index * 17)
        results = [_one(random.Random(rng.randrange(1, 10**9)), strategy, world, markets, bankroll) for _ in range(paths)]
        summary = _summarize(results, bankroll)
        summary["name"] = name
        summary["world"] = world_name
        summary["strategy"] = strategy
        books.append(summary)
        # One path, drawn with the same seed, for the chart.
        illustrative = _one(random.Random(seed + 100 + index), strategy, world, markets, bankroll)
        curves[f"{strategy}-{world}"] = _downsample(illustrative.curve)
    return {
        "bankroll": bankroll,
        "markets": markets,
        "paths": paths,
        "books": books,
        "curves": {
            "naive_fair": curves["naive-fair"],
            "common_fair": curves["common-fair"],
            "common_dislocated": curves["common-dislocated"],
        },
        "note": (
            "This is a model with a known true chance. It is not the live paper book. "
            "It shows why a 90% quote is not, by itself, a trade."
        ),
    }
