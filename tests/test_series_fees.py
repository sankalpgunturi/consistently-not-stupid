from decimal import Decimal

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
