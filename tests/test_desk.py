import json
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from cst.api import create_app
from cst.broker import PaperBroker
from cst.config import Settings
from cst.decisions import parse_veto
from cst.depth import DepthResult, judge_kalshi_orderbook, judge_polymarket_book
from cst.engine import Engine
from cst.fees import kalshi_taker_fee, polymarket_taker_fee
from cst.models import Settlement, StrategyParams
from cst.review import govern, heuristic_updates, tighten_value
from cst.simulate import run_report
from cst.store import Store
from cst.strategy import evaluate, pair_id, quote_in_band, wilson_lower
from cst.text import related, same_proposition
from cst.venues.kalshi import quotes_from_kalshi_market
from cst.venues.polymarket import quotes_from_polymarket_market
from tests.conftest import NOW, make_book, make_params, make_quote


def test_polymarket_fee_matches_the_published_identity():
    assert polymarket_taker_fee(100, 0.50, 0.07, 1) == __import__("decimal").Decimal("1.75000")
    assert polymarket_taker_fee(100, 0.90, 0.07, 1) == __import__("decimal").Decimal("0.63000")
    assert polymarket_taker_fee(100, 0.50, 0.05, 1) == __import__("decimal").Decimal("1.25000")
    assert polymarket_taker_fee(100, 0.90, 0, 1) == __import__("decimal").Decimal("0")
    assert polymarket_taker_fee(1, 0.99, 0.0001, 1) == __import__("decimal").Decimal("0")


def test_kalshi_fee_rounds_up_to_the_next_cent():
    assert kalshi_taker_fee(1, "0.50") == __import__("decimal").Decimal("0.02")
    assert kalshi_taker_fee(100, "0.50") == __import__("decimal").Decimal("1.75")
    assert kalshi_taker_fee(1, "0.90") == __import__("decimal").Decimal("0.01")
    assert kalshi_taker_fee(100, "0.90") == __import__("decimal").Decimal("0.63")


def test_same_sentence_matches_across_venues_and_near_misses_do_not():
    score = same_proposition(
        "Will the price of Bitcoin be above $82,000 on October 7?", "Yes", "yes",
        "Will BTC be above $82,000 on Oct 7?", "Yes", "yes",
    )
    assert score >= 0.74
    assert same_proposition(
        "Will the price of Bitcoin be above $82,000 on October 7?", "Yes", "yes",
        "Will BTC be above $80,000 on Oct 7?", "Yes", "yes",
    ) == 0
    assert same_proposition(
        "Will the price of Bitcoin be above $82,000 on October 7?", "Yes", "yes",
        "Will the price of Bitcoin be above $82,000 on October 7?", "No", "no",
    ) == 0
    assert same_proposition(
        "Will Bitcoin be above $82,000 on October 7?", "Yes", "yes",
        "Will Bitcoin be above $82,000 on October 8?", "Yes", "yes",
    ) == 0


def test_related_clusters_twins_and_leaves_different_races_alone():
    french_a = "Will Jordan Bardella win the 2027 French presidential election?"
    french_b = "Will Éric Zemmour win the 2027 French presidential election?"
    american = "Will Donald Trump win the 2028 US presidential election?"
    assert related(french_a, "No", "e1", "polymarket", french_b, "No", "e2", "polymarket") >= 0.48
    assert related(french_a, "No", "e1", "polymarket", american, "Yes", "e9", "polymarket") < 0.48
    assert related(
        "Will Bitcoin be above $82,000 on October 7?", "Yes", "a", "polymarket",
        "Will Bitcoin be above $82,000 on October 8?", "Yes", "b", "kalshi",
    ) >= 0.48


def test_fee_eats_a_near_certain_contract():
    quote = make_quote(bid=0.985, ask=0.99, fee_rate=0.07)
    result = evaluate([quote], make_params(), make_book(streaks={quote.key: 2}), now=NOW)
    assert result.proposals == []
    assert result.decisions[0].reason_code == "fee"


def _cross_pair():
    cheap = make_quote()
    rich = make_quote(
        venue="kalshi",
        market_id="KXBTCD-TEST",
        event_id="KXBTCD-TEST",
        bid=0.97,
        ask=0.98,
        min_shares=1,
        fee_model="kalshi",
        fee_rate=0.07,
        liquidity=100,
        volume=100,
        ask_size=20,
    )
    return cheap, rich


def _cover(_quote, shares, _action):
    return DepthResult(True, shares, "The displayed size covers the clip.")


def test_an_unapproved_pair_is_not_a_buy_until_a_person_confirms_it():
    cheap, rich = _cross_pair()
    streaks = {cheap.key: 2, rich.key: 2}
    result = evaluate([cheap, rich], make_params(), make_book(streaks=streaks), now=NOW)
    assert result.proposals == []
    row = next(item for item in result.decisions if item.reason_code == "equivalence")
    assert row.pair_id == pair_id(cheap.key, rich.key)
    approved = evaluate(
        [cheap, rich],
        make_params(),
        make_book(streaks=streaks, approved_pairs={row.pair_id}),
        now=NOW,
    )
    assert len(approved.proposals) == 1
    assert approved.proposals[0].quote.venue == "polymarket"
    assert approved.proposals[0].signal == "cross_venue"
    assert approved.proposals[0].edge > 0.02


def test_one_event_keeps_one_clip():
    first = make_quote(market_id="a")
    second = make_quote(
        market_id="b",
        title="Will the price of Bitcoin be above $80,000 on October 7?",
    )
    twin_a = make_quote(venue="kalshi", market_id="ka", event_id="ka", bid=0.97, ask=0.98, min_shares=1, fee_model="kalshi", liquidity=50, volume=50, ask_size=10)
    twin_b = make_quote(
        venue="kalshi", market_id="kb", event_id="kb",
        title=second.title, bid=0.97, ask=0.98, min_shares=1, fee_model="kalshi", liquidity=50, volume=50, ask_size=10,
    )
    quotes = [first, second, twin_a, twin_b]
    pairs = {pair_id(first.key, twin_a.key), pair_id(second.key, twin_b.key)}
    result = evaluate(
        quotes,
        make_params(),
        make_book(streaks={q.key: 2 for q in quotes}, approved_pairs=pairs),
        now=NOW,
    )
    assert len(result.proposals) == 1
    assert any(item.reason_code == "correlation" for item in result.decisions)


def test_far_dated_favorite_is_not_a_trade():
    quote = make_quote(end_time=NOW + timedelta(days=400))
    result = evaluate([quote], make_params(), make_book(streaks={quote.key: 3}), now=NOW)
    assert result.proposals == []
    assert result.decisions[0].reason_code == "horizon"


def test_unstable_quote_is_watched():
    quote = make_quote()
    result = evaluate([quote], make_params(min_stable_scans=2), make_book(streaks={quote.key: 1}), now=NOW)
    assert result.proposals == []
    assert result.decisions[0].action == "watching"


def test_quote_without_a_second_price_stays_in_cash():
    quote = make_quote()
    result = evaluate([quote], make_params(), make_book(streaks={quote.key: 2}), now=NOW)
    assert result.proposals == []
    assert result.decisions[0].reason_code == "edge"
    assert quote_in_band(quote, make_params())


def test_wilson_bound_is_below_the_raw_rate():
    assert wilson_lower(0, 0) == 0
    assert wilson_lower(27, 30) < 27 / 30
    assert wilson_lower(30, 30) > wilson_lower(20, 30)


def test_paper_buy_and_settlement_keep_the_cash_identity(tmp_path):
    store = Store(tmp_path / "book.sqlite", StrategyParams(), 1000)
    store.reset(StrategyParams())
    broker = PaperBroker(store)
    quote = make_quote()
    trade = broker.buy(quote, quote.min_shares, "cross_venue", "test", 1)
    assert trade is not None
    cash_after_buy = store.cash()
    assert cash_after_buy < 1000
    position = store.positions()[0]
    closed = broker.settle(position, True)
    assert store.positions() == []
    assert abs((cash_after_buy + quote.min_shares) - store.cash()) < 1e-6
    assert closed.pnl > 0
    assert abs(store.cash() - (1000 + closed.pnl)) < 1e-4


def test_a_stop_realizes_a_small_loss_instead_of_the_whole_premium(tmp_path):
    store = Store(tmp_path / "book.sqlite", StrategyParams(), 1000)
    store.reset(StrategyParams())
    broker = PaperBroker(store)
    quote = make_quote()
    broker.buy(quote, quote.min_shares, "cross_venue", "test", 1)
    position = store.positions()[0]
    closed = broker.sell(position, 0.80, "stop")
    assert closed.pnl < 0
    assert abs(closed.pnl) < position.cost_basis
    assert store.cash() > 1000 - position.cost_basis


def test_governor_refuses_to_loosen_after_losses():
    params = StrategyParams()
    losses = [Settlement("0.93–0.96", False, 0.94, 0.01, -4) for _ in range(8)]
    updated, notes, applied = govern(params, {"min_probability": 0.85, "min_edge": 0.005}, set(), losses)
    assert "min_probability" not in applied
    assert updated.min_probability == params.min_probability
    assert notes


def test_governor_refuses_to_loosen_even_after_a_profitable_record():
    params = StrategyParams()
    wins = [Settlement("0.93–0.96", True, 0.94, 0.01, 0.2) for _ in range(25)]
    updated, notes, applied = govern(
        params,
        {"min_probability": 0.85, "min_edge": params.min_edge - 0.002, "max_spread": 0.035},
        {"min_edge"},
        wins,
    )
    assert applied == {}
    assert updated.min_probability == 0.90
    assert updated.min_edge == params.min_edge
    assert updated.max_spread == params.max_spread
    assert notes
    # A long winning record used to lower min_edge. It must not.
    rich = [Settlement("0.90–0.93", True, 0.90, 0.005, 0.05) for _ in range(100)]
    updates = heuristic_updates(params, rich)
    assert updates.get("min_edge", params.min_edge) >= params.min_edge
    assert "max_spread" not in updates
    assert tighten_value(params, "min_probability") == 0.91
    assert tighten_value(params, "scan_interval_seconds") is None
    # Raising the twin bar lets overlaps through that the old bar blocked.
    blocked, _notes, twin_applied = govern(params, {"correlation_threshold": 0.50}, set(), [])
    assert "correlation_threshold" not in twin_applied
    assert blocked.correlation_threshold == params.correlation_threshold
    assert abs(tighten_value(params, "correlation_threshold") - 0.46) < 1e-9


def test_governor_can_tighten_and_only_one_step():
    params = StrategyParams()
    updated, _notes, applied = govern(params, {"min_probability": 0.99}, set(), [])
    assert applied["min_probability"] == params.min_probability + 0.01
    assert updated.min_probability == 0.91


def test_losing_record_tightens_the_heuristic():
    params = StrategyParams()
    losses = [Settlement("0.93–0.96", False, 0.94, 0.02, -4) for _ in range(10)]
    updates = heuristic_updates(params, losses)
    assert updates["min_probability"] > params.min_probability
    assert updates["min_edge"] > params.min_edge


def test_model_sits_out_when_the_quote_is_fair_and_trades_a_real_gap():
    report = run_report(paths=80, markets=60)
    fair = next(item for item in report["books"] if item["strategy"] == "common" and "true chance" in item["world"])
    naive = next(item for item in report["books"] if item["strategy"] == "naive")
    rich = next(item for item in report["books"] if item["strategy"] == "common" and "second" in item["world"])
    assert fair["mean_trades"] == 0
    assert fair["mean_pnl"] == 0
    assert naive["mean_pnl"] < 0
    assert rich["mean_pnl"] > 0
    assert rich["mean_trades"] > 0


def test_parsers_keep_a_real_favorite_and_drop_a_stub():
    market = {
        "id": "1",
        "question": "Will the price of Bitcoin be above $82,000 on October 7?",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.94", "0.06"]),
        "bestBid": 0.93,
        "bestAsk": 0.94,
        "endDate": "2026-10-08T00:00:00Z",
        "feeSchedule": {"rate": 0.07, "exponent": 1, "takerOnly": True},
        "feeType": "crypto_fees",
        "liquidity": "20000",
        "volume": "5000",
        "orderMinSize": 5,
        "active": True,
        "closed": False,
        "acceptingOrders": True,
    }
    event = {"id": "9", "title": "Bitcoin on October 7", "slug": "bitcoin", "liquidity": "20000"}
    quotes = quotes_from_polymarket_market(market, event)
    assert {item.side for item in quotes} == {"yes", "no"}
    yes = next(item for item in quotes if item.side == "yes")
    assert yes.ask == 0.94
    assert yes.fee_rate == 0.07
    no = next(item for item in quotes if item.side == "no")
    assert round(no.bid, 2) == 0.06

    stub = {
        "ticker": "KXTEST-1",
        "event_ticker": "KXTEST",
        "title": "A one-sided print",
        "status": "active",
        "yes_bid_dollars": "0.0000",
        "yes_ask_dollars": "0.9900",
        "no_bid_dollars": "0.0100",
        "no_ask_dollars": "1.0000",
        "volume_fp": "0",
        "open_interest_fp": "0",
        "close_time": "2026-10-08T00:00:00Z",
    }
    assert quotes_from_kalshi_market(stub) == []


def test_engine_paper_cycle_buys_the_cheap_favorite(tmp_path):
    cheap = make_quote()
    rich = make_quote(
        venue="kalshi", market_id="KX1", event_id="KX1",
        bid=0.97, ask=0.98, min_shares=1, fee_model="kalshi",
        liquidity=80, volume=80, ask_size=15,
    )
    settings = Settings(data_dir=str(tmp_path), min_stable_scans=1, bankroll=1000)
    engine = Engine(settings, fetcher=lambda _settings: ([cheap, rich], []))
    engine.refresher = lambda *_args: None
    engine.depth = _cover
    engine.decider = lambda _proposals: {}
    engine.store.approve_pair(pair_id(cheap.key, rich.key), "Checked both resolution rules. Same contract.")
    state = engine.run_cycle()
    assert state["counts"]["bought"] == 1
    assert state["book"]["cash"] < 1000
    assert state["book"]["equity"] < 1000  # the spread and the fee show up immediately
    assert state["positions"][0]["venue"] == "polymarket"
    assert state["mode"] == "paper"


def test_drawdown_pauses_new_buys(tmp_path):
    cheap = make_quote()
    rich = make_quote(
        venue="kalshi", market_id="KX1", event_id="KX1",
        bid=0.97, ask=0.98, min_shares=1, fee_model="kalshi",
        liquidity=80, volume=80, ask_size=15,
    )
    settings = Settings(data_dir=str(tmp_path), min_stable_scans=1, bankroll=1000, max_drawdown=0.05)
    engine = Engine(settings, fetcher=lambda _settings: ([cheap, rich], []))
    engine.refresher = lambda *_args: None
    engine.depth = _cover
    engine.decider = lambda _proposals: {}
    engine.store.approve_pair(pair_id(cheap.key, rich.key), "Checked both resolution rules. Same contract.")
    engine.store.set_cash(900)
    with engine.store.lock:
        engine.store._put("peak", 1000)
        engine.store.conn.commit()
    state = engine.run_cycle()
    assert state["counts"]["bought"] == 0
    assert state["book"]["entries_paused"] is True


def test_dashboard_and_health(tmp_path):
    settings = Settings(data_dir=str(tmp_path), bankroll=1000)
    engine = Engine(settings, fetcher=lambda _settings: ([], []))
    engine.refresher = lambda *_args: None
    app = create_app(engine, start_loop=False)
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        page = client.get("/")
        assert page.status_code == 200
        assert "The Common Sense Trade" in page.text
        assert "Scan now" in page.text
        assert "Live unavailable" in page.text
        assert "Pause buys" in page.text
        state = client.get("/api/state")
        body = state.json()
        assert body["book"]["equity"] == 1000
        assert body["mode"] == "paper"
        assert body["live"] == "unavailable"
        token = body["csrf"]
        denied = client.post("/api/scan")
        assert denied.status_code == 403
        foreign = client.post("/api/scan", headers={"X-CSRF-Token": token, "Origin": "https://evil.example"})
        assert foreign.status_code == 403
        scanned = client.post("/api/scan", headers={"X-CSRF-Token": token, "Origin": "http://127.0.0.1:8000"})
        assert scanned.status_code == 200
        assert scanned.json()["counts"]["bought"] == 0


def test_websocket_sends_the_paper_snapshot(tmp_path):
    settings = Settings(data_dir=str(tmp_path), bankroll=1000)
    engine = Engine(settings, fetcher=lambda _settings: ([], []))
    app = create_app(engine, start_loop=False)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as socket:
            payload = socket.receive_json()
    assert payload["mode"] == "paper"
    assert payload["live"] == "unavailable"


def test_source_never_places_an_order():
    text = "\n".join(path.read_text() for path in Path("src/cst").rglob("*.py"))
    dashboard = "\n".join(path.read_text() for path in Path("dashboard").rglob("*") if path.suffix in {".html", ".js"})
    assert "/portfolio/" not in text
    assert "create_and_post_order" not in text
    assert "clob.polymarket.com/order" not in text
    assert "Arm live" not in dashboard
    assert "approve-pair" in dashboard


def test_polymarket_price_alone_does_not_settle():
    market = {
        "id": "1",
        "question": "Will the price of Bitcoin be above $82,000 on October 7?",
        "outcomes": ["Yes", "No"],
        "outcomePrices": ["1", "0"],
        "bestBid": 0.99,
        "bestAsk": 0.995,
        "clobTokenIds": ["yes-token", "no-token"],
        "endDate": "2026-10-08T00:00:00Z",
        "active": True,
        "closed": False,
        "acceptingOrders": True,
    }
    live = quotes_from_polymarket_market(market, {"id": "9", "title": "Bitcoin"})
    assert live
    assert all(not item.settled for item in live)
    assert next(item for item in live if item.side == "yes").token_id == "yes-token"
    closed = dict(market, closed=True, active=False, umaResolutionStatus="")
    unresolved = quotes_from_polymarket_market(closed, {"id": "9", "title": "Bitcoin"})
    assert unresolved == []
    dust = dict(closed, outcomePrices=["0", "0"], bestBid=None, bestAsk=None, umaResolutionStatus=None)
    assert quotes_from_polymarket_market(dust, {"id": "9"}) == []


def test_polymarket_uma_resolved_settles():
    market = {
        "id": "1",
        "question": "Resolved favorite",
        "outcomes": ["Yes", "No"],
        "outcomePrices": ["1", "0"],
        "closed": True,
        "active": False,
        "umaResolutionStatus": "resolved",
        "clobTokenIds": "[\"yes-token\", \"no-token\"]",
    }
    quotes = quotes_from_polymarket_market(market, {"id": "9", "title": "Resolved"})
    yes = next(item for item in quotes if item.side == "yes")
    assert yes.settled is True
    assert yes.winner == "yes"
    assert yes.token_id == "yes-token"


def test_displayed_size_has_to_cover_the_clip():
    thin = judge_polymarket_book(
        {"bids": [{"price": "0.93", "size": "10"}], "asks": [{"price": "0.94", "size": "4"}]},
        "buy",
        0.94,
        5,
    )
    assert thin.ok is False
    covered = judge_polymarket_book(
        {"asks": [{"price": "0.94", "size": "3"}, {"price": "0.93", "size": "3"}]},
        "buy",
        0.94,
        5,
    )
    assert covered.ok is True
    moved = judge_polymarket_book({"asks": [{"price": "0.96", "size": "20"}]}, "buy", 0.94, 5)
    assert moved.ok is False
    assert judge_polymarket_book(None, "buy", 0.94, 5).ok is False
    book = {"orderbook_fp": {"yes_dollars": [["0.93", "4"]], "no_dollars": [["0.06", "8"]]}}
    assert judge_kalshi_orderbook(book, "yes", "buy", 0.94, 5).ok is True
    assert judge_kalshi_orderbook(book, "yes", "buy", 0.94, 9).ok is False
    assert judge_kalshi_orderbook(book, "yes", "buy", 0.93, 1).ok is False
    assert judge_kalshi_orderbook({"orderbook": {}}, "yes", "buy", 0.94, 1).ok is False


def test_thin_book_does_not_fill(tmp_path):
    cheap, rich = _cross_pair()
    settings = Settings(data_dir=str(tmp_path), min_stable_scans=1, bankroll=1000)
    engine = Engine(settings, fetcher=lambda _settings: ([cheap, rich], []))
    engine.refresher = lambda *_args: None
    engine.decider = lambda _proposals: {}
    engine.depth = lambda _quote, _shares, _action: DepthResult(False, 1, "The size on the offer is 1, short of 5.")
    engine.store.approve_pair(pair_id(cheap.key, rich.key), "Checked the resolution rules.")
    state = engine.run_cycle()
    assert state["counts"]["bought"] == 0
    assert state["book"]["cash"] == 1000
    assert state["positions"] == []
    assert any(row["reason_code"] == "book" for row in state["tape"])
    assert not any(row["action"] == "bought" for row in state["tape"])


def test_decisions_veto_cannot_add_a_clip_and_a_bad_body_drops_nothing():
    mapping = {"c0": "polymarket:m1:yes"}
    dropped = parse_veto({
        "answers": [
            {
                "type": "choice",
                "name": "c0",
                "choice": "drop",
                "probabilities": [
                    {"value": "drop", "probability": 0.8},
                    {"value": "keep", "probability": 0.2},
                ],
            },
            {
                "type": "choice",
                "name": "invented",
                "choice": "drop",
                "probabilities": [{"value": "drop", "probability": 0.99}],
            },
        ]
    }, mapping)
    assert set(dropped) == {"polymarket:m1:yes"}
    assert parse_veto("nope", mapping) == {}
    assert parse_veto({}, mapping) == {}
    assert parse_veto({"answers": [{"name": "c0", "choice": "drop"}]}, mapping) == {}
    low = {"answers": [{"type": "choice", "name": "c0", "choice": "drop", "probabilities": [{"value": "drop", "probability": 0.4}]}]}
    assert parse_veto(low, mapping) == {}
    duplicate = {"answers": [low["answers"][0], dict(low["answers"][0], probabilities=[{"value": "drop", "probability": 0.9}])]}
    assert parse_veto(duplicate, mapping) == {}
    refused = {"answers": [{"type": "refusal", "name": "c0", "choice": "drop", "probabilities": [{"value": "drop", "probability": 0.99}]}]}
    assert parse_veto(refused, mapping) == {}


def test_a_veto_removes_the_buy(tmp_path):
    cheap, rich = _cross_pair()
    settings = Settings(data_dir=str(tmp_path), min_stable_scans=1, bankroll=1000)
    engine = Engine(settings, fetcher=lambda _settings: ([cheap, rich], []))
    engine.refresher = lambda *_args: None
    engine.depth = _cover
    engine.store.approve_pair(pair_id(cheap.key, rich.key), "Checked the resolution rules.")
    engine.decider = lambda proposals: {proposals[0].key: "same risk"} if proposals else {"invented:1:yes": "buy"}
    state = engine.run_cycle()
    assert state["counts"]["bought"] == 0
    assert state["positions"] == []
    assert any(row["reason_code"] == "veto" for row in state["tape"])


def test_pause_block_and_tighten_stick(tmp_path):
    settings = Settings(data_dir=str(tmp_path), bankroll=1000)
    engine = Engine(settings, fetcher=lambda _settings: ([], []))
    engine.refresher = lambda *_args: None
    app = create_app(engine, start_loop=False)
    with TestClient(app) as client:
        token = client.get("/api/state").json()["csrf"]
        headers = {"X-CSRF-Token": token}
        paused = client.post("/api/pause", headers=headers, json={"paused": True})
        assert paused.status_code == 200
        assert paused.json()["operator_pause"] is True
        blocked = client.post("/api/block", headers=headers, json={"key": "polymarket:m1:yes"})
        assert blocked.status_code == 200
        tightened = client.post("/api/knob", headers=headers, json={"key": "min_probability"})
        assert tightened.status_code == 200
        probability = next(row for row in tightened.json()["params"] if row["key"] == "min_probability")
        assert probability["value"] == 0.91
        clock = client.post("/api/knob", headers=headers, json={"key": "scan_interval_seconds"})
        assert clock.status_code == 400
        empty = client.post("/api/approve-pair", headers=headers, json={"pair_id": "a|b", "note": "  "})
        assert empty.status_code == 400
    other = Store(tmp_path / "book.sqlite", StrategyParams(), 1000)
    assert other.operator_pause() is True
    assert "polymarket:m1:yes" in other.blocked()
    assert other.params().min_probability == 0.91
    assert other.audit()


def test_manual_close_needs_size(tmp_path):
    settings = Settings(data_dir=str(tmp_path), bankroll=1000)
    engine = Engine(settings, fetcher=lambda _settings: ([], []))
    quote = make_quote()
    PaperBroker(engine.store).buy(quote, quote.min_shares, "learned", "test", 1)
    position_id = engine.store.positions()[0].id
    engine.refresher = lambda *_args: quote
    engine.depth = lambda _q, _s, _a: DepthResult(False, 0, "The size on the bid is 1, short of 5.")
    app = create_app(engine, start_loop=False)
    with TestClient(app) as client:
        token = client.get("/api/state").json()["csrf"]
        headers = {"X-CSRF-Token": token}
        refused = client.post("/api/close", headers=headers, json={"id": position_id})
        assert refused.status_code == 409
        assert engine.store.positions()
        engine.depth = _cover
        closed = client.post("/api/close", headers=headers, json={"id": position_id})
        assert closed.status_code == 200
        assert engine.store.positions() == []
