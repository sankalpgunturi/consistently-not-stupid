"""Live trading turns on only for a whole-dollar amount from $1 to $5,000."""

import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from fastapi.testclient import TestClient

from cst.api import create_app
from cst.config import Settings
from cst.engine import Engine
from cst.live import (
    KalshiTrader,
    LiveTradingError,
    buy_order,
    execution_from_response,
    load_private_key,
    parse_live_amount,
    sell_order,
    sign_text,
)
from cst.models import Position
from tests.conftest import make_quote
from tests.test_desk import _cover, _seed_record


def _rsa():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _headers(engine):
    return {"X-CSRF-Token": engine.store.csrf(), "Origin": "http://127.0.0.1:8000"}


class FakeTrader:
    def __init__(self, balance=10_000, fill=True):
        self.balance = balance
        self.fill = fill
        self.orders = []

    def available_dollars(self):
        return self.balance

    def buy(self, quote, shares, cost):
        self.orders.append(("buy", quote.market_id, quote.side, shares, cost))
        if cost > self.balance + 1e-9:
            from cst.live import Execution
            return Execution(False, "The Kalshi balance is short of this order.")
        from cst.live import Execution
        if not self.fill:
            return Execution(False, "Kalshi did not fill the order.")
        return Execution(True, "", quote.ask, shares)

    def sell(self, position, bid):
        self.orders.append(("sell", position.market_id, position.side, bid))
        from cst.live import Execution
        return Execution(True, "", bid, position.shares)


def test_amount_is_a_whole_dollar_from_one_to_five_thousand():
    assert parse_live_amount(1) == (1, None)
    assert parse_live_amount(5000) == (5000, None)
    assert parse_live_amount(5000.0) == (5000, None)
    assert parse_live_amount(0)[0] is None
    assert parse_live_amount(5001)[0] is None
    assert parse_live_amount(1.5)[0] is None
    assert parse_live_amount("100")[0] is None
    assert parse_live_amount(True)[0] is None


def test_yes_buy_is_a_yes_bid_and_no_buy_sells_yes_at_the_complement():
    yes = buy_order(make_quote(side="yes", ask=0.94), 1)[0]
    assert yes["side"] == "bid"
    assert yes["price"] == "0.9400"
    assert yes["time_in_force"] == "fill_or_kill"
    assert yes["count"] == "1.00"
    no = buy_order(make_quote(side="no", ask=0.93), 1)[0]
    assert no["side"] == "ask"
    assert no["price"] == "0.0700"


def test_a_no_fill_is_booked_at_the_no_price():
    execution = execution_from_response("no", __import__("decimal").Decimal("0.93"), {
        "fill_count": "1.00",
        "remaining_count": "0.00",
        "average_fill_price": "0.0700",
    })
    assert execution.filled
    assert execution.price == pytest.approx(0.93)
    missed = execution_from_response("yes", __import__("decimal").Decimal("0.94"), {"fill_count": "0.00"})
    assert missed.filled is False


def test_closing_a_no_position_buys_yes_and_reduces_only():
    position = Position(
        id="p", venue="kalshi", market_id="m1", event_id="e", event_title="", title="",
        outcome="", side="no", category="", shares=1, entry_price=0.93, cost_basis=0.94,
        fees=0.01, signal="", reason="", opened_cycle=1, bid=0.90, end_time=None,
        fee_model="kalshi", fee_rate=0.07, fee_exponent=1,
    )
    payload, _price = sell_order(position, 0.90)
    assert payload["side"] == "bid"
    assert payload["price"] == "0.1000"
    assert payload["time_in_force"] == "immediate_or_cancel"
    assert payload["reduce_only"] is True


def test_signature_covers_the_timestamp_method_and_path():
    key = _rsa()
    signature = sign_text(key, "1000GET/trade-api/v2/portfolio/balance")
    key.public_key().verify(
        __import__("base64").b64decode(signature),
        b"1000GET/trade-api/v2/portfolio/balance",
        __import__("cryptography.hazmat.primitives.asymmetric.padding", fromlist=["padding"]).PSS(
            mgf=__import__("cryptography.hazmat.primitives.asymmetric.padding", fromlist=["padding"]).MGF1(
                __import__("cryptography.hazmat.primitives.hashes", fromlist=["hashes"]).SHA256()
            ),
            salt_length=__import__("cryptography.hazmat.primitives.asymmetric.padding", fromlist=["padding"]).PSS.DIGEST_LENGTH,
        ),
        __import__("cryptography.hazmat.primitives.hashes", fromlist=["hashes"]).SHA256(),
    )
    ed = ed25519.Ed25519PrivateKey.generate()
    signed = sign_text(ed, "1000POST/trade-api/v2/portfolio/events/orders")
    ed.public_key().verify(__import__("base64").b64decode(signed), b"1000POST/trade-api/v2/portfolio/events/orders")


def test_missing_key_file_does_not_switch(tmp_path):
    settings = Settings(
        data_dir=str(tmp_path), bankroll=1000,
        kalshi_api_key_id="key-id", kalshi_private_key_path=str(tmp_path / "missing.pem"),
    )
    engine = Engine(settings, fetcher=lambda _settings: ([], []))
    with pytest.raises(LiveTradingError, match="not on this machine"):
        load_private_key(settings)
    state, error = engine.arm_live(25)
    assert error
    assert state["mode"] == "paper"
    assert state["live"] == "unavailable"
    assert engine.store.cash() == 1000


def test_approve_switches_immediately_and_caps_the_book(tmp_path):
    engine = Engine(Settings(data_dir=str(tmp_path), bankroll=1000), fetcher=lambda _settings: ([], []))
    engine.trader = FakeTrader(balance=80)
    app = create_app(engine, start_loop=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        denied = client.post("/api/live", json={"amount": 25})
        assert denied.status_code == 403
        low = client.post("/api/live", headers=_headers(engine), json={"amount": 0})
        assert low.status_code == 400
        high = client.post("/api/live", headers=_headers(engine), json={"amount": 5001})
        assert high.status_code == 400
        assert engine.store.trading_mode() == "paper"
        switched = client.post("/api/live", headers=_headers(engine), json={"amount": 25})
        assert switched.status_code == 200
        body = switched.json()
        assert body["mode"] == "live"
        assert body["live"] == "on"
        assert body["live_budget"] == 25
        assert body["book"]["cash"] == 25
        assert body["book"]["start"] == 25
        assert body["live_exchange_balance"] == 80
        again = client.post("/api/live", headers=_headers(engine), json={"amount": 40})
        assert again.status_code == 400
        assert engine.store.live_budget() == 25
        reset = client.post("/api/reset", headers=_headers(engine))
        assert reset.status_code == 409
        assert engine.store.trading_mode() == "live"
        assert engine.store.cash() == 25


def test_a_live_buy_is_sent_and_an_unfilled_order_does_not_spend_cash(tmp_path):
    quote = make_quote()
    settings = Settings(entry_window_minutes=0, data_dir=str(tmp_path), min_stable_scans=1, bankroll=1000, max_position_fraction=1)
    engine = Engine(settings, fetcher=lambda _settings: ([quote], []))
    engine.refresher = lambda *_args: None
    engine.depth = _cover
    engine.decider = lambda _proposals: {}
    _seed_record(engine.store)
    engine.trader = FakeTrader(fill=False)
    engine.arm_live(5000)
    missed = engine.run_cycle()
    assert missed["counts"]["bought"] == 0
    assert engine.trader.orders
    assert engine.store.cash() == 5000
    assert engine.store.positions() == []

    engine.trader = FakeTrader(fill=True)
    bought = engine.run_cycle()
    assert bought["counts"]["bought"] == 1
    assert engine.store.positions()
    assert engine.store.cash() < 5000
    assert engine.trader.orders[0][0] == "buy"


def test_paper_mode_does_not_call_the_trader(tmp_path):
    quote = make_quote()
    engine = Engine(
        Settings(entry_window_minutes=0, data_dir=str(tmp_path), min_stable_scans=1, bankroll=1000),
        fetcher=lambda _settings: ([quote], []),
    )
    engine.refresher = lambda *_args: None
    engine.depth = _cover
    engine.decider = lambda _proposals: {}
    _seed_record(engine.store)
    engine.trader = FakeTrader()
    state = engine.run_cycle()
    assert state["counts"]["bought"] == 1
    assert engine.trader.orders == []
    assert state["mode"] == "paper"


def test_short_kalshi_balance_skips_the_order(tmp_path):
    quote = make_quote()
    engine = Engine(Settings(data_dir=str(tmp_path), bankroll=1000), fetcher=lambda _settings: ([], []))
    engine.trader = FakeTrader(balance=0.01)
    engine.arm_live(100)
    trade, detail = engine._execute_buy(quote, 1, "learned", "test", 1)
    assert trade is None
    assert "balance" in detail
    assert engine.store.cash() == 100


def test_signed_order_uses_the_v2_path_and_books_the_fill(tmp_path):
    key = _rsa()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_path = tmp_path / "kalshi.pem"
    key_path.write_bytes(pem)
    seen = {}

    def handler(request: httpx.Request):
        assert "?" not in request.url.path
        assert request.headers["KALSHI-ACCESS-KEY"] == "key-id"
        assert request.headers["KALSHI-ACCESS-SIGNATURE"]
        if request.url.path.endswith("/portfolio/balance"):
            return httpx.Response(200, json={
                "balance": 100000, "balance_dollars": "1000.00", "portfolio_value": 0, "updated_ts": 1,
            })
        seen["path"] = request.url.path
        body = json.loads(request.content)
        assert body["ticker"] == "m1"
        assert body["side"] == "bid"
        assert body["time_in_force"] == "fill_or_kill"
        return httpx.Response(201, json={
            "order_id": "o1", "fill_count": "1.00", "remaining_count": "0.00",
            "average_fill_price": "0.9400", "ts_ms": 1,
        })

    settings = Settings(
        data_dir=str(tmp_path / "book"), bankroll=1000,
        kalshi_api_key_id="key-id", kalshi_private_key_path=str(key_path),
        kalshi_base_url="https://external-api.kalshi.com/trade-api/v2",
    )
    trader = KalshiTrader.from_settings(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    execution = trader.buy(make_quote(), 1, 0.95)
    assert execution.filled
    assert execution.price == pytest.approx(0.94)
    assert seen["path"].endswith("/portfolio/events/orders")


def test_dashboard_offers_the_amount_dialog():
    page = Path("src/cst/dashboard/index.html").read_text()
    script = Path("src/cst/dashboard/app.js").read_text()
    assert "Switch to live trading?" in page
    assert 'id="live-amount"' in page
    assert 'type="text"' in page
    assert 'id="live-error"' in page
    assert 'type="range"' not in page
    assert 'min="1"' in page and 'max="5000"' in page
    assert "Approve" in page
    assert "/api/live" in script
    assert 'post("/api/live"' not in script
    handler = script.split('$("live-approve").addEventListener', 1)[1]
    assert "window.alert" not in handler
    assert "showLiveError" in handler
    button = next(line for line in page.splitlines() if 'id="live"' in line)
    assert "disabled" not in button
