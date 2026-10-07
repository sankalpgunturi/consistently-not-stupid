from cst.strategy import evaluate, price_bucket
from tests.conftest import NOW, make_quote, make_book, make_params


def test_small_sample_refusal_names_the_actual_shortfall():
    quote = make_quote()
    book = make_book(streaks={quote.key: 2}, calibration={price_bucket(quote.ask): (10, 10)})
    result = evaluate([quote], make_params(), book, now=NOW)
    assert not result.proposals
    detail = result.decisions[0].detail
    assert "10 samples; at least 30" in detail
    assert "72.2%" in detail
    assert "needs 96.0%" in detail
    assert result.decisions[0].edge is None


def test_sufficient_sample_can_still_refuse_an_unproven_edge():
    quote = make_quote()
    book = make_book(streaks={quote.key: 2}, calibration={price_bucket(quote.ask): (90, 100)})
    result = evaluate([quote], make_params(), book, now=NOW)
    assert not result.proposals
    assert "90 wins in 100 samples" in result.decisions[0].detail
    assert "needs 96.0%" in result.decisions[0].detail
    assert result.decisions[0].edge < -0.10


def test_stability_refusal_does_not_mislabel_the_fee_as_edge():
    quote = make_quote()
    result = evaluate([quote], make_params(min_stable_scans=2), make_book(streaks={quote.key: 1}), now=NOW)
    assert result.decisions[0].reason_code == "stability"
    assert result.decisions[0].edge is None
