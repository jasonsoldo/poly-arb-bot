import json
from dataclasses import replace

from poly_arb_bot.ev_shadow import strategy_config
from poly_arb_bot.strategy_shadow_lifecycle import PortfolioLimits, StrategyShadowLifecycle, process_audit_once


def test_lifecycle_persists_real_order_invariants_on_initialization(tmp_path):
    state = tmp_path / "state.json"
    StrategyShadowLifecycle(state, tmp_path / "audit.jsonl")
    stored = json.loads(state.read_text(encoding="utf-8"))
    assert stored["real_order_submissions"] == 0
    assert stored["real_orders"] == 0
    assert stored["real_fills"] == 0


def accepted(event_id="a1", strategy="late_window_directional_ev", outcome="Up"):
    return {
        "event_id": event_id, "event_type": "shadow_eval", "strategy": strategy,
        "market_id": "m1", "asset": "BTC", "timeframe": "5m", "outcome": outcome,
        "decision": "ACCEPT", "expected_fill_price": 0.4, "fees": 0.01,
        "target_size": 10, "ts": 1000, "config_hash": strategy_config()[1],
    }


def paired(event_id="pair1"):
    return {
        "event_id": event_id, "event_type": "shadow_opportunity", "strategy": "paired_lock",
        "market_id": "m1", "decision": "ACCEPT", "target_size": 10,
        "condition_id": "c1", "asset": "BTC", "timeframe": "5m", "window": "current",
        "generation": 3, "session": 7, "evaluation_sequence": 11,
        "net_cost": 9.7, "locked_profit": 0.3, "ts": 1000,
        "config_version": "paired-lock-shadow-v2", "config_hash": "paired-hash",
    }


def hedged(event_id="hedge1", main_outcome="Up"):
    return {
        "event_id": event_id, "event_type": "shadow_hedged_opportunity",
        "strategy": "late_window_directional_ev", "hedge_strategy": "low_price_lottery_ev",
        "market_id": "m1", "decision": "ACCEPT", "asset": "BTC", "timeframe": "5m",
        "main_outcome": main_outcome, "hedge_outcome": "Down" if main_outcome == "Up" else "Up",
        "main_size": 10, "hedge_size": 8, "main_expected_fill_price": .8,
        "hedge_expected_fill_price": .04, "main_cost": 8.05, "hedge_cost": .35,
        "total_cost": 8.4, "main_win_pnl": 1.6, "reversal_pnl": -.4,
        "expected_portfolio_pnl": 1.4, "worst_case_pnl": -.4,
        "estimated_probability": .9, "seconds_to_close": 8, "target_size": 10,
        "ts": 1000, "config_version": "terminal-hedge-v1", "config_hash": "hedge-hash",
    }


def market(source="chainlink", timeframe="5m"):
    return {"market_id": "m1", "asset": "BTC", "interval": timeframe,
            "settlement_source": source, "close_ts": 1100, "open_price": 100}


def prediction(event_id="prediction-1", strategy="late_window_directional_ev",
               decision="REJECT", seconds_to_close=90):
    return {
        "event_id": event_id, "event_type": "shadow_eval", "strategy": strategy,
        "market_id": "m1", "condition_id": "c1", "asset": "BTC",
        "timeframe": "5m", "window": "current", "outcome": "Up",
        "decision": decision, "reason": "net_ev_below_threshold",
        "estimated_probability": 0.7, "raw_estimated_probability": 0.75,
        "probability_model_id": "directional_normal_cdf_v1",
        "reference_quorum_met": True, "reference_state": "REFERENCE_READY",
        "settlement_source_verified": True, "settlement_reference": 100.5,
        "price_to_beat": 100, "seconds_to_close": seconds_to_close,
        "ts": 1010, "config_version": "strategy-v1", "config_hash": "model-hash",
    }


def test_repeated_accepts_open_one_position(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "complete.jsonl")
    assert lifecycle.consume(accepted(), {"m1": market()}) is True
    assert lifecycle.consume(accepted("a2"), {"m1": market()}) is False
    assert len(lifecycle.data["positions"]) == 1
    position = next(iter(lifecycle.data["positions"].values()))
    assert position["entry_cost"] == 4.1
    assert position["real_order_submissions"] == 0
    assert position["config_version"] == "shadow-portfolio-v6"
    assert len(position["config_hash"]) == 64


def test_raw_directional_accept_is_calibration_only_and_does_not_open_position(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "events.jsonl")
    row = accepted()
    row["config_version"] = "shadow-buy-rules-v7"
    assert lifecycle.consume(row, {"m1": market()}) is False
    assert lifecycle.data["positions"] == {}


def test_terminal_hedge_opens_one_combined_position(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "events.jsonl")
    assert lifecycle.consume(hedged(), {"m1": market()}) is True
    position = next(iter(lifecycle.data["positions"].values()))
    assert position["outcome"] == "Up"
    assert position["hedge_outcome"] == "Down"
    assert position["entry_cost"] == 8.4
    assert position["main_size"] == 10
    assert position["hedge_size"] == 8
    assert position["expected_portfolio_pnl"] == 1.4


def test_terminal_hedge_settlement_uses_main_or_hedge_payout(tmp_path):
    log = tmp_path / "events.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    lifecycle.consume(hedged(), {"m1": market()})
    venue = {"assets": {"BTC": {"chainlink_settlement_samples": [
        {"source_timestamp_ms": 1_100_000, "price": 99},
    ]}}}
    assert lifecycle.settle({"m1": market()}, venue, now=1101) == 1
    complete = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert complete["winning_outcome"] == "Down"
    assert complete["payout"] == 8
    assert complete["realized_simulated_pnl"] == -.4


def test_chainlink_settlement_completes_winning_up_position(tmp_path):
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    lifecycle.consume(accepted(), {"m1": market()})
    venue = {"assets": {"BTC": {"chainlink_settlement_samples": [
        {"source_timestamp_ms": 1_100_000, "price": 101},
    ]}}}
    assert lifecycle.settle({"m1": dict(market(), open_price=100)}, venue, now=1101) == 1
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert row["event_type"] == "shadow_complete"
    assert row["winning_outcome"] == "Up"
    assert row["realized_simulated_pnl"] == 5.9
    assert row["real_orders"] == 0


def test_binance_settlement_requires_matching_timeframe(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "complete.jsonl")
    lifecycle.consume(accepted(), {"m1": market("binance", "1h")})
    venue = {"assets": {"BTC": {"binance_settlement_samples": [
        {"source_timestamp_ms": 1_100_000, "price": 99, "timeframe": "4h"},
        {"source_timestamp_ms": 1_100_000, "price": 101, "timeframe": "1h"},
    ]}}}
    assert lifecycle.settle({"m1": dict(market("binance", "1h"), open_price=100)}, venue, now=1101) == 1


def test_missing_official_settlement_keeps_position_open(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "complete.jsonl")
    lifecycle.consume(accepted(), {"m1": market()})
    assert lifecycle.settle({"m1": dict(market(), open_price=100)}, {"assets": {}}, now=1200) == 0
    assert len(lifecycle.data["positions"]) == 1


def test_paired_lock_completes_only_after_official_close_sample(tmp_path):
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    assert lifecycle.consume(paired(), {"m1": market()}) is True
    assert lifecycle.settle({"m1": market()}, {"assets": {}}, now=1200) == 0
    venue = {"assets": {"BTC": {"chainlink_settlement_samples": [
        {"source_timestamp_ms": 1_100_000, "price": 101},
    ]}}}
    assert lifecycle.settle({"m1": market()}, venue, now=1200) == 1
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert row["strategy"] == "paired_lock"
    assert row["payout"] == 10
    assert row["realized_simulated_pnl"] == 0.3
    assert row["condition_id"] == "c1"
    assert row["generation"] == 3
    assert row["session"] == 7
    assert row["evaluation_sequence"] == 11
    assert row["strategy_config_version"] == "paired-lock-shadow-v2"
    assert row["strategy_config_hash"] == "paired-hash"
    assert row["timestamp"] == 1200
    assert row["real_fills"] == 0


def test_paired_lock_does_not_require_directional_opening_anchor(tmp_path):
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    row = market()
    row["open_price"] = None
    lifecycle.consume(paired(), {"m1": row})
    venue = {"assets": {"BTC": {"chainlink_settlement_samples": [
        {"source_timestamp_ms": 1_100_000, "price": 101},
    ]}}}
    assert lifecycle.settle({"m1": row}, venue, now=1200) == 1
    complete = json.loads(log.read_text(encoding="utf-8"))
    assert complete["winning_outcome"] is None


def test_strategy_audit_offset_is_persisted_and_accept_is_not_replayed(tmp_path):
    audit = tmp_path / "strategy.jsonl"
    audit.write_text(json.dumps(accepted()) + "\n", encoding="utf-8")
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "complete.jsonl")
    markets = {"m1": market()}
    assert process_audit_once(audit, lifecycle, markets) == 1
    assert process_audit_once(audit, lifecycle, markets) == 0
    assert lifecycle.data["audit_offset"] == audit.stat().st_size


def test_rejected_fixed_horizon_prediction_is_captured_without_opening_trade(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "events.jsonl")

    assert lifecycle.capture_prediction(prediction(), {"m1": market()}) is True

    assert lifecycle.data["positions"] == {}
    assert len(lifecycle.data["probability_predictions"]) == 1
    stored = next(iter(lifecycle.data["probability_predictions"].values()))
    assert stored["origin_decision"] == "REJECT"
    assert stored["estimated_up_probability"] == 0.7
    assert stored["calibration_horizon_seconds"] == 90


def test_prediction_capture_is_one_up_sample_per_market_model_and_horizon(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "events.jsonl")
    markets = {"m1": market()}

    assert lifecycle.capture_prediction(prediction(), markets) is True
    assert lifecycle.capture_prediction(prediction("prediction-2"), markets) is False
    down = prediction("prediction-down")
    down["outcome"] = "Down"
    assert lifecycle.capture_prediction(down, markets) is False
    too_early = prediction("prediction-early", seconds_to_close=91)
    too_early["market_id"] = "m2"
    assert lifecycle.capture_prediction(
        too_early, {"m2": dict(market(), market_id="m2")}
    ) is False


def test_prediction_capture_requires_valid_probability_inputs(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "events.jsonl")
    markets = {"m1": market()}
    for field in (
        "estimated_probability", "probability_model_id", "config_hash",
        "price_to_beat", "settlement_reference",
    ):
        row = prediction(f"missing-{field}")
        row[field] = None
        assert lifecycle.capture_prediction(row, markets) is False
    row = prediction("blocked-reference")
    row["reference_quorum_met"] = False
    assert lifecycle.capture_prediction(row, markets) is False
    row = prediction("unverified-settlement")
    row["settlement_source_verified"] = False
    assert lifecycle.capture_prediction(row, markets) is False


def test_prediction_settlement_writes_independent_calibration_event(tmp_path):
    log = tmp_path / "events.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    lifecycle.capture_prediction(prediction(), {"m1": market()})
    venue = {"assets": {"BTC": {"chainlink_settlement_samples": [
        {"source_timestamp_ms": 1_100_000, "price": 101},
    ]}}}

    assert lifecycle.settle({"m1": market()}, venue, now=1101) == 0

    complete = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert complete["event_type"] == "shadow_prediction_complete"
    assert complete["actual_up"] == 1
    assert complete["winning_outcome"] == "Up"
    assert complete["brier_score"] == 0.09
    assert complete["origin_decision"] == "REJECT"
    assert complete["trade_accepted"] is False
    assert lifecycle.data["probability_predictions"] == {}
    assert lifecycle.data["probability_calibration"]["late_window_directional_ev"]["samples"] == 1


def test_cross_strategy_same_market_outcome_is_not_double_opened(tmp_path):
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    markets = {"m1": market()}
    assert lifecycle.consume(accepted(), markets) is True
    assert lifecycle.consume(accepted("l1", "low_price_lottery_ev"), markets) is False
    reject = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert reject["event_type"] == "shadow_position_reject"
    assert reject["reason"] == "correlated_market_outcome_exposure"


def test_directional_and_lottery_share_close_window_risk_limit(tmp_path):
    limits = replace(PortfolioLimits(), combined_max_per_close_window=1)
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log, limits)
    markets = {"m1": market(), "m2": dict(market(), market_id="m2", asset="ETH")}
    assert lifecycle.consume(accepted(), markets) is True
    second = accepted("l1", "low_price_lottery_ev")
    second.update(market_id="m2", asset="ETH")
    assert lifecycle.consume(second, markets) is False
    assert json.loads(log.read_text().splitlines()[-1])["reason"] == "combined_close_window_limit"


def test_default_portfolio_limit_allows_one_directional_risk_per_close_window(tmp_path):
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    markets = {"m1": market(), "m2": dict(market(), market_id="m2", asset="ETH")}
    assert lifecycle.consume(accepted(), markets) is True
    second = accepted("l1", "low_price_lottery_ev")
    second.update(market_id="m2", asset="ETH")
    assert lifecycle.consume(second, markets) is False
    assert json.loads(log.read_text().splitlines()[-1])["reason"] == "combined_close_window_limit"


def test_lottery_close_window_and_total_notional_limits_are_enforced(tmp_path):
    limits = replace(PortfolioLimits(), combined_max_per_close_window=2,
                     lottery_max_per_close_window=1,
                     lottery_max_open_notional=10.0)
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log, limits)
    markets = {"m1": market(), "m2": dict(market(), market_id="m2")}
    assert lifecycle.consume(accepted("l1", "low_price_lottery_ev"), markets) is True
    second = accepted("l2", "low_price_lottery_ev")
    second["market_id"] = "m2"
    second["asset"] = "ETH"
    assert lifecycle.consume(second, markets) is False
    assert json.loads(log.read_text().splitlines()[-1])["reason"] == "lottery_close_window_limit"


def test_lottery_total_open_notional_limit_is_enforced(tmp_path):
    limits = replace(PortfolioLimits(), lottery_max_open_notional=5.0)
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log, limits)
    markets = {"m1": market(), "m2": dict(market(), market_id="m2", close_ts=1300)}
    assert lifecycle.consume(accepted("l1", "low_price_lottery_ev"), markets) is True
    second = accepted("l2", "low_price_lottery_ev")
    second["market_id"] = "m2"
    second["asset"] = "ETH"
    assert lifecycle.consume(second, markets) is False
    assert json.loads(log.read_text().splitlines()[-1])["reason"] == "lottery_open_notional_limit"


def test_strategy_order_size_limits_are_enforced(tmp_path):
    limits = replace(PortfolioLimits(), directional_max_order_size=5,
                     lottery_max_order_size=5)
    log = tmp_path / "complete.jsonl"
    markets = {"m1": market()}
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log, limits)
    assert lifecycle.consume(accepted(), markets) is False
    reject = json.loads(log.read_text().splitlines()[-1])
    assert reject["reason"] == "directional_order_size_limit"
    assert reject["real_fills"] == 0


def test_calibration_mode_bypasses_portfolio_limits_and_records_counterfactual(tmp_path):
    limits = replace(PortfolioLimits(), directional_max_order_size=5)
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", tmp_path / "events.jsonl", limits,
        calibration_mode=True,
    )

    assert lifecycle.consume(accepted(), {"m1": market()}) is True

    position = next(iter(lifecycle.data["positions"].values()))
    assert position["risk_mode"] == "CALIBRATION_UNTHROTTLED"
    assert position["portfolio_limits_enforced"] is False
    assert position["would_block_reason"] == "directional_order_size_limit"
    assert lifecycle.data["calibration_bypasses"] == {"directional_order_size_limit": 1}
    assert lifecycle.data["current_risk_halts"] == {}


def test_calibration_mode_can_be_enabled_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SHADOW_CALIBRATION_MODE", "true")
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "events.jsonl")
    assert lifecycle.data["calibration_mode"] is True
    assert lifecycle.data["portfolio_limits_enforced"] is False


def test_lottery_daily_loss_blocks_new_positions_after_settlement(tmp_path, monkeypatch):
    monkeypatch.setattr("poly_arb_bot.strategy_shadow_lifecycle.time.time", lambda: 1200)
    limits = replace(PortfolioLimits(), lottery_max_daily_loss=0.5)
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log, limits)
    markets = {"m1": market(), "m2": dict(market(), market_id="m2", close_ts=1300)}
    lifecycle.consume(accepted("l1", "low_price_lottery_ev", "Down"), markets)
    venue = {"assets": {"BTC": {"chainlink_settlement_samples": [
        {"source_timestamp_ms": 1_100_000, "price": 101},
    ]}}}
    assert lifecycle.settle(markets, venue, now=1200) == 1
    second = accepted("l2", "low_price_lottery_ev")
    second["market_id"] = "m2"
    assert lifecycle.consume(second, markets) is False
    assert json.loads(log.read_text().splitlines()[-1])["reason"] == "lottery_daily_loss_limit"


def test_existing_completion_log_is_backfilled_for_loss_limits(tmp_path, monkeypatch):
    monkeypatch.setattr("poly_arb_bot.strategy_shadow_lifecycle.time.time", lambda: 1200)
    log = tmp_path / "complete.jsonl"
    log.write_text(json.dumps({
        "ts": 1101, "event_id": "old:complete", "event_type": "shadow_complete",
        "strategy": "late_window_directional_ev", "market_id": "old",
        "strategy_config_hash": strategy_config()[1],
        "realized_simulated_pnl": -6.0,
    }) + "\n", encoding="utf-8")
    limits = replace(PortfolioLimits(), directional_max_daily_loss=5.0)
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log, limits)
    assert len(lifecycle.data["completed_trades"]) == 1
    assert lifecycle.consume(accepted("d2"), {"m1": market()}) is False
    assert json.loads(log.read_text().splitlines()[-1])["reason"] == "directional_daily_loss_limit"


def test_old_config_loss_does_not_block_current_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr("poly_arb_bot.strategy_shadow_lifecycle.time.time", lambda: 1200)
    log = tmp_path / "complete.jsonl"
    log.write_text(json.dumps({
        "ts": 1101, "event_id": "old:complete", "event_type": "shadow_complete",
        "strategy": "late_window_directional_ev", "market_id": "old",
        "strategy_config_hash": "old-hash", "realized_simulated_pnl": -100.0,
    }) + "\n", encoding="utf-8")
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    assert lifecycle.consume(accepted("current"), {"m1": market()}) is True


def test_existing_state_trade_hash_is_migrated_from_canonical_log(tmp_path):
    current_hash = strategy_config()[1]
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "positions": {}, "completed": ["done"], "completed_trades": [{
            "event_id": "done", "strategy": "late_window_directional_ev",
            "market_id": "m1", "ts": 1000, "pnl": -1,
        }],
    }), encoding="utf-8")
    log = tmp_path / "complete.jsonl"
    log.write_text(json.dumps({
        "event_id": "done", "event_type": "shadow_complete",
        "strategy": "late_window_directional_ev", "market_id": "m1",
        "strategy_config_hash": current_hash, "realized_simulated_pnl": -1,
    }), encoding="utf-8")
    lifecycle = StrategyShadowLifecycle(state, log)
    assert lifecycle.data["completed_trades"][0]["strategy_config_hash"] == current_hash


def test_existing_state_trade_hash_is_migrated_from_rotated_log(tmp_path):
    current_hash = strategy_config()[1]
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "positions": {}, "completed": ["done"], "completed_trades": [{
            "event_id": "done", "strategy": "late_window_directional_ev",
            "market_id": "m1", "ts": 1000, "pnl": -1,
        }],
    }), encoding="utf-8")
    log = tmp_path / "complete.jsonl"
    log.write_text("", encoding="utf-8")
    (tmp_path / "complete.jsonl.1").write_text(json.dumps({
        "event_id": "done", "event_type": "shadow_complete",
        "strategy_config_hash": current_hash,
    }), encoding="utf-8")
    lifecycle = StrategyShadowLifecycle(state, log)
    assert lifecycle.data["completed_trades"][0]["strategy_config_hash"] == current_hash


def test_completed_event_preserves_entry_model_evidence(tmp_path):
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    row = accepted()
    row.update(estimated_probability=.7, net_ev=.2, gross_edge=.3,
               consensus_price=101, settlement_reference=100.8,
               probability_reference_source="settlement_reference",
               probability_reference_price=100.8, reference_state="REFERENCE_READY",
               volatility_per_sqrt_second=.001, up_final_model_z=.5,
               paired_book_imbalance=.2,
               model_sample_span_seconds=120,
               minimum_model_sample_span_seconds=60,
               confidence_type="input_quality_not_historical_accuracy")
    lifecycle.consume(row, {"m1": market()})
    venue = {"assets": {"BTC": {"chainlink_settlement_samples": [
        {"source_timestamp_ms": 1_100_000, "price": 101},
    ]}}}
    lifecycle.settle({"m1": market()}, venue, now=1200)
    complete = json.loads(log.read_text().splitlines()[-1])
    assert complete["estimated_probability"] == .7
    assert complete["net_ev"] == .2
    assert complete["consensus_price"] == 101
    assert complete["settlement_reference"] == 100.8
    assert complete["probability_reference_source"] == "settlement_reference"
    assert complete["probability_reference_price"] == 100.8
    assert complete["volatility_per_sqrt_second"] == .001
    assert complete["up_final_model_z"] == .5
    assert complete["paired_book_imbalance"] == .2
    assert complete["model_sample_span_seconds"] == 120
    assert complete["minimum_model_sample_span_seconds"] == 60

def test_opened_position_has_explicit_active_lifecycle_state(tmp_path):
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json",
        tmp_path / "complete.jsonl",
    )

    assert lifecycle.consume(accepted(), {"m1": market()}) is True

    position = next(iter(lifecycle.data["positions"].values()))
    assert position["lifecycle_state"] == "ACTIVE"


def test_missing_settlement_marks_position_pending_before_orphan_timeout(tmp_path):
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json",
        tmp_path / "complete.jsonl",
        orphan_after_seconds=900,
    )
    lifecycle.consume(accepted(), {"m1": market()})

    assert lifecycle.settle(
        {"m1": market()},
        {"assets": {}},
        now=1200,
    ) == 0

    position = next(iter(lifecycle.data["positions"].values()))
    assert position["lifecycle_state"] == "SETTLEMENT_PENDING"
    assert position["settlement_pending_since"] == 1200
    assert lifecycle.data["orphaned_positions"] == []


def test_unsettled_position_is_orphaned_and_releases_portfolio_capacity(tmp_path):
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json",
        log,
        orphan_after_seconds=900,
    )
    lifecycle.consume(accepted(), {"m1": market()})

    assert lifecycle.settle(
        {"m1": market()},
        {"assets": {}},
        now=2001,
    ) == 0

    assert lifecycle.data["positions"] == {}
    assert len(lifecycle.data["orphaned_positions"]) == 1

    orphan = lifecycle.data["orphaned_positions"][0]
    assert orphan["lifecycle_state"] == "ORPHANED"
    assert orphan["orphan_reason"] == "settlement_sample_unavailable"
    assert orphan["real_orders"] == 0
    assert orphan["real_order_submissions"] == 0
    assert orphan["real_fills"] == 0
    assert orphan["timestamp"] == 2001

    log_row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert log_row["event_type"] == "shadow_orphaned"


def test_lifecycle_checkpoints_are_dirty_and_coalesced(tmp_path):
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", tmp_path / "events.jsonl",
        checkpoint_interval_seconds=5,
    )
    writes = []
    lifecycle._write_state = lambda: writes.append(dict(lifecycle.data))
    lifecycle._mark_dirty()
    lifecycle._save()
    lifecycle._mark_dirty()
    lifecycle._save()
    assert writes == []
    lifecycle.flush()
    assert len(writes) == 1
    lifecycle.flush()
    assert len(writes) == 1

