from decimal import Decimal
import pytest

from cst.fees import fee_for
from cst.strategy import evaluate
from cst.venues.kalshi import quotes_from_kalshi_market, _series_fees
from tests.conftest import NOW, make_book, make_params


def market():
    return {"ticker": "KXTEST-EXAMPLE", "status": "active", "yes_bid_dollars": "0.90", "yes_ask_dollars": "0.91",
            "no_bid_dollars": "0.09", "no_ask_dollars": "0.10", "close_time": "2026-10-08T12:00:00Z", "volume_fp": "100"}


def test_series_multiplier_flows_into_entry_and_exit_fee_math():
    quote = quotes_from_kalshi_market(market(), series={"fee_type": "quadratic", "fee_multiplier": 3})[0]
    assert quote.fee_verified
    assert quote.fee_rate == 0.21
    assert fee_for("kalshi", 1, quote.ask, quote.fee_rate) == Decimal("0.02")
    # An exact-cent raw fee must not acquire a floating-point extra cent.
    assert fee_for("kalshi", 400, 0.50, quote.fee_rate) == Decimal("21.00")


def test_free_series_is_not_replaced_by_standard_fee():
    quote = quotes_from_kalshi_market(market(), series={"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 0})[0]
    assert quote.fee_verified
    assert fee_for("kalshi", 1, quote.ask, quote.fee_rate) == 0


def test_unknown_or_unreadable_fee_refuses_admission():
    for series in ({}, {"fee_type": "flat", "fee_multiplier": 1}, {"fee_type": "quadratic", "fee_multiplier": "nan"}):
        quote = quotes_from_kalshi_market(market(), series=series)[0]
        assert not quote.fee_verified
        result = evaluate([quote], make_params(), make_book(calibration={"0.90–0.93": (1000, 1000)}, streaks={quote.key: 2}), now=NOW)
        assert not result.proposals
        assert result.decisions[0].reason_code == "fee"


def test_metadata_is_read_once_per_series_and_failures_are_counted():
    calls = []

    class Http:
        def get_json(self, url):
            calls.append(url)
            if url.endswith("BAD"):
                raise RuntimeError("unavailable")
            return {"series": {"fee_type": "quadratic", "fee_multiplier": 2}}

    found, failures = _series_fees(Http(), "https://example.invalid", [market(), market(), dict(market(), ticker="BAD-EXAMPLE")])
    assert len(calls) == 2
    assert set(found) == {"KXTEST"}
    assert failures == 1


@pytest.mark.parametrize("side,bid", [("yes", "0.80"), ("no", "0.86"), ("yes", "0.89")])
def test_fee_lookup_includes_candidates_below_ninety_percent(side, bid):
    calls = []

    class Http:
        def get_json(self, url):
            calls.append(url)
            return {"series": {"fee_type": "quadratic", "fee_multiplier": 1}}

    row = dict(market(), yes_bid_dollars="0.10", no_bid_dollars="0.10")
    row[f"{side}_bid_dollars"] = bid
    row[f"{side}_ask_dollars"] = str(float(bid) + .01)
    found, failures = _series_fees(Http(), "https://example.invalid", [row])
    assert len(calls) == 1 and failures == 0
    quote = next(q for q in quotes_from_kalshi_market(row, series=found["KXTEST"]) if q.side == side)
    assert quote.fee_verified


def test_discovery_metadata_expires_and_does_not_reuse_failed_refresh(monkeypatch):
    from cst.venues.kalshi import _metadata
    now = [0]
    monkeypatch.setattr('cst.venues.kalshi.time.monotonic', lambda: now[0])
    cache, calls = {}, []

    class Http:
        def get_json(self, url):
            calls.append(url)
            if len(calls) == 2:
                raise TimeoutError()
            return {"series": {"fee_type": "quadratic", "fee_multiplier": len(calls)}}

    http = Http()
    url = 'https://example.invalid/series/TEST'
    assert _metadata(http, url, cache)['series']['fee_multiplier'] == 1
    now[0] = 59
    assert _metadata(http, url, cache)['series']['fee_multiplier'] == 1
    assert len(calls) == 1
    now[0] = 60
    with pytest.raises(TimeoutError):
        _metadata(http, url, cache)
    assert url not in cache
    assert _metadata(http, url, cache)['series']['fee_multiplier'] == 3
