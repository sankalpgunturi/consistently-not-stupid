from datetime import datetime, timedelta, timezone

from cst.models import BookView, Quote, StrategyParams

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


def make_quote(**overrides) -> Quote:
    data = dict(
        venue="polymarket",
        market_id="m1",
        event_id="e1",
        event_title="Bitcoin on October 7",
        title="Will the price of Bitcoin be above $82,000 on October 7?",
        outcome="Yes",
        side="yes",
        bid=0.93,
        ask=0.94,
        ask_size=-1,
        volume=10_000,
        liquidity=20_000,
        end_time=NOW + timedelta(hours=20),
        category="Crypto",
        fee_model="polymarket",
        fee_rate=0.04,
        fee_exponent=1,
        min_shares=5,
    )
    data.update(overrides)
    return Quote(**data)


def make_params(**overrides) -> StrategyParams:
    params = StrategyParams(min_stable_scans=1)
    for key, value in overrides.items():
        setattr(params, key, value)
    return params


def make_book(**overrides) -> BookView:
    data = dict(equity=1000, cash=1000, peak=1000, deployed=0)
    data.update(overrides)
    return BookView(**data)
