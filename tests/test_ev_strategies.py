from poly_arb_bot.ev_strategies import DirectionalInput, decision_audit, evaluate_directional, evaluate_lottery
from poly_arb_bot.reference_layer import ReferenceQuote, ReferenceState


def reference(ready=True, sources=None):
    return ReferenceState(sources or [], 101, 100, 100, 2 if ready else 1, 1, 10, ready,
                          "REFERENCE_READY" if ready else "REFERENCE_BLOCKED",
                          None if ready else "insufficient_reference_sources")


def base(**overrides):
    data = dict(strategy="late_window_directional_ev", market_id="m1", condition_id="c1",
                asset="BTC", timeframe="5m", outcome="Up",
                market_price=.55, expected_fill_price=.56, estimated_probability=.95,
                seconds_to_close=10, price_to_beat=99, reference=reference(), fee_per_share=.01,
                slippage_per_share=.002, latency_risk_buffer=.003, settlement_risk_buffer=.002,
                model_uncertainty_buffer=.01, execution_risk_buffer=.005, liquidity=100,
                book_age_ms=50, reference_age_ms=50, clock_skew_ms=10,
                minimum_liquidity=20, maximum_slippage=.01,
                maximum_reference_age_ms=3000, maximum_book_age_ms=750,
                maximum_clock_skew_ms=250,
                market_active=True, market_tradable=True, target_depth_ok=True, momentum_bps_30s=2,
                order_book_imbalance=.1, confidence=.8, settlement_source_verified=True)
    data.update(overrides)
    return DirectionalInput(**data)


def test_late_window_directional_accepts_positive_net_ev():
    result = evaluate_directional(base())
    assert result.decision == "ACCEPT"
    assert round(result.net_ev, 3) == .373
    assert result.strategy == "late_window_directional_ev"


def test_directional_fails_closed_without_reference_quorum():
    result = evaluate_directional(base(reference=reference(False)))
    assert result.decision == "REJECT"
    assert result.reason == "insufficient_reference_sources"


def test_lottery_has_independent_price_and_ev_rules():
    result = evaluate_lottery(base(strategy="low_price_lottery_ev", market_price=.02,
                                   expected_fill_price=.021, estimated_probability=.08,
                                   seconds_to_close=300))
    assert result.decision == "ACCEPT"
    assert result.strategy == "low_price_lottery_ev"
    rejected = evaluate_lottery(base(strategy="low_price_lottery_ev", expected_fill_price=.08,
                                     market_price=.08, estimated_probability=.2, seconds_to_close=300))
    assert rejected.reason == "entry_price_above_limit"


def test_completed_statistics_are_not_created_by_accept():
    result = evaluate_directional(base())
    assert result.completed is False
    assert result.real_order_submissions == 0


def test_buy_rules_fail_closed_on_missing_clock_skew_and_excess_slippage():
    assert evaluate_directional(base(clock_skew_ms=None)).reason == "clock_skew_unavailable"
    assert evaluate_directional(base(slippage_per_share=.02)).reason == "slippage_exceeded"


def test_buy_rules_require_target_depth_and_microstructure_inputs():
    assert evaluate_directional(base(liquidity=19)).reason == "insufficient_liquidity"
    assert evaluate_directional(base(momentum_bps_30s=None)).reason == "momentum_unavailable"
    assert evaluate_directional(base(order_book_imbalance=None)).reason == "order_book_imbalance_unavailable"


def test_directional_audit_contains_strategy_specific_cost_and_reference_fields():
    row = base()
    audit = decision_audit(row, evaluate_directional(row), event_id="e1", generation=2, session=3,
                           evaluation_sequence=4, timestamp=1000)
    required = {"event_id", "event_type", "strategy", "asset", "timeframe", "outcome",
                "estimated_probability", "market_price", "expected_fill_price", "gross_edge",
                "fees", "slippage", "latency_risk_buffer", "settlement_risk_buffer", "net_ev",
                "fast_price", "consensus_price", "settlement_reference",
                "fresh_exchange_source_count", "fresh_usd_spot_source_count",
                "cross_source_divergence_bps", "reference_quorum_met", "reference_state",
                "price_to_beat", "distance_to_price_to_beat", "decision", "reason",
                "real_order_submissions"}
    assert required <= set(audit)
    assert audit["strategy"] == "late_window_directional_ev"
    assert audit["real_order_submissions"] == 0


def test_stale_clob_is_primary_reason_and_all_blockers_are_preserved():
    row = base(
        price_to_beat=None,
        estimated_probability=None,
        probability_block_reason="price_to_beat_capture_missed",
        book_age_ms=10_000,
    )
    result = evaluate_directional(row)
    assert result.reason == "clob_book_stale"
    assert result.blocking_reasons[:2] == (
        "clob_book_stale",
        "price_to_beat_capture_missed",
    )


def test_accept_has_no_blocking_reasons():
    result = evaluate_directional(base())
    assert result.blocking_reasons == ()


def test_audit_explains_reference_source_acceptance_and_rejection():
    sources = [
        ReferenceQuote("coinbase", "BTC", "BTC-USD", "spot", "USD", 100, 99, 101, 1, 2, 10, "FRESH"),
        ReferenceQuote("binance", "BTC", "BTCUSDT", "spot", "USDT", 100, 99, 101, 1, 2, 4000, "STALE"),
        ReferenceQuote("chainlink", "BTC", "btc/usd", "settlement", "USD", 100, None, None, 1, 2, 20, "FRESH"),
    ]
    row = base(reference=reference(True, sources=sources), settlement_source="chainlink")
    audit = decision_audit(row, evaluate_directional(row), event_id="e1", generation=2, session=3,
                           evaluation_sequence=4, timestamp=1000)
    assert audit["blocking_reasons"] == []
    assert set(audit["valid_reference_sources"]) == {"coinbase", "chainlink"}
    assert audit["rejected_reference_sources"] == ["binance"]
    statuses = {item["source"]: item for item in audit["reference_source_statuses"]}
    assert statuses["coinbase"]["accepted_for_quorum"] is True
    assert statuses["chainlink"]["role"] == "settlement_reference"
    assert statuses["binance"]["rejection_reason"] == "stale"
