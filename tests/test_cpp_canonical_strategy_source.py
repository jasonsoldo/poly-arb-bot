from pathlib import Path


ENGINE = Path("cpp/market_ws_engine/market_ws_engine.cpp").read_text(encoding="utf-8")
SCRIPT = Path("scripts/run_shadow_loop.sh").read_text(encoding="utf-8")


def test_cpp_engine_loads_strategy_market_metadata_and_module():
    assert '#include "../strategy/ev_strategy.hpp"' in ENGINE
    for field in (
        "condition_id", "asset", "interval", "window", "start_ts", "open_price",
        "open_price_source", "open_price_capture_mode", "open_price_source_timestamp_ms",
        "settlement_source", "accepting_orders",
    ):
        assert field in ENGINE


def test_cpp_strategy_audit_exposes_price_to_beat_provenance():
    assert '\\"price_to_beat_source\\"' in ENGINE
    assert '\\"price_to_beat_capture_mode\\"' in ENGINE
    assert '\\"price_to_beat_source_timestamp_ms\\"' in ENGINE


def test_cpp_engine_emits_four_independent_strategy_evaluations():
    assert "evaluate_reference_strategies" in ENGINE
    assert "strategy::probability_model" in ENGINE
    assert "strategy::lottery_probability_model" in ENGINE
    assert "strategy::evaluate_directional" in ENGINE
    assert "strategy::evaluate_lottery" in ENGINE
    assert '"late_window_directional_ev"' in ENGINE
    assert '"low_price_lottery_ev"' in ENGINE
    assert "shadow_hedged_opportunity" in ENGINE
    assert "expected_portfolio_pnl" in ENGINE
    assert "reversal_pnl" in ENGINE
    assert "strategy::directional_window" in ENGINE
    assert '"directional_not_accepted"' not in ENGINE
    assert "for (const std::string outcome : {\"Up\", \"Down\"})" in ENGINE
    assert "strategy_audit_" in ENGINE


def test_cpp_strategy_audit_is_shadow_only_and_suppressed():
    assert "strategy_emission_state_" in ENGINE
    assert "strategy_accept_heartbeat_seconds_" in ENGINE
    assert "strategy_reject_heartbeat_seconds_" in ENGINE
    assert '\\\"real_order_submissions\\\":0' in ENGINE
    assert '\\\"real_orders\\\":0' in ENGINE
    assert '\\\"real_fills\\\":0' in ENGINE
    assert "config_hash" in ENGINE
    assert "shadow-buy-rules-v7" in ENGINE
    assert "--strategy-config-hash" in ENGINE


def test_strategy_audit_identifies_independent_probability_models():
    assert '\\\"probability_model_id\\\"' in ENGINE
    assert '\\\"raw_estimated_probability\\\"' in ENGINE
    assert "directional_normal_cdf_v1" in ENGINE
    assert "lottery_market_blend_v1" in ENGINE


def test_reference_mutation_drives_strategy_evaluation_without_gating_paired_lock():
    reference_handler = ENGINE.split("void on_reference_snapshot", 1)[1].split("void on_reference_state", 1)[0]
    assert "evaluate_reference_strategies" in reference_handler
    paired = ENGINE.split("void evaluate()", 1)[1].split("void write_health", 1)[0]
    assert "reference_quorum_met" not in paired


def test_runtime_starts_python_only_as_cpp_parity_verifier():
    assert "EV_SHADOW_MODE=verify" in SCRIPT
    assert "logs/strategy-parity.jsonl" in SCRIPT
    assert "logs/strategy-audit.jsonl" in SCRIPT
