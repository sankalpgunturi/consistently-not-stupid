from datetime import datetime, timedelta, timezone

import pytest

from cst.config import Settings

from cst.models import BookView, Quote, StrategyParams

# Keep synthetic engine quotes in the future as the calendar advances.
NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Tests must never consume the operator's keys or call a paid model."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("CST_OPENAI_API_KEY", "")


def make_quote(**overrides) -> Quote:
    data = dict(
        venue="kalshi",
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
        fee_model="kalshi",
        fee_rate=0.07,
        fee_exponent=1,
        min_shares=1,
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
