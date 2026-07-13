from poly_arb_bot.ev_strategies import DirectionalInput, decision_audit, evaluate_directional, evaluate_lottery
from poly_arb_bot.reference_layer import ReferenceState


def reference(ready=True):
    return ReferenceState([], 101, 100, 100, 2 if ready else 1, 1, 10, ready,
                          "REFERENCE_READY" if ready else "REFERENCE_BLOCKED",
                          None if ready else "insufficient_reference_sources")


def base(**overrides):
    data = dict(strategy="late_window_directional_ev", market_id="m1", condition_id="c1",
                asset="BTC", timeframe="5m", outcome="Up",
                market_price=.55, expected_fill_price=.56, estimated_probability=.65,
                seconds_to_close=45, price_to_beat=99, reference=reference(), fee_per_share=.01,
                slippage_per_share=.002, latency_risk_buffer=.003, settlement_risk_buffer=.002,
                model_uncertainty_buffer=.01, execution_risk_buffer=.005, liquidity=100,
                book_age_ms=50, settlement_source_verified=True)
    data.update(overrides)
    return DirectionalInput(**data)


def test_late_window_directional_accepts_positive_net_ev():
    result = evaluate_directional(base())
    assert result.decision == "ACCEPT"
    assert round(result.net_ev, 3) == .073
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
