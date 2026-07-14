import json
from dataclasses import replace

from poly_arb_bot.ev_shadow import strategy_config
from poly_arb_bot.strategy_shadow_lifecycle import PortfolioLimits, StrategyShadowLifecycle, process_audit_once


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
        "net_cost": 9.7, "locked_profit": 0.3, "ts": 1000,
    }


def market(source="chainlink", timeframe="5m"):
    return {"market_id": "m1", "asset": "BTC", "interval": timeframe,
            "settlement_source": source, "close_ts": 1100, "open_price": 100}


def test_repeated_accepts_open_one_position(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "complete.jsonl")
    assert lifecycle.consume(accepted(), {"m1": market()}) is True
    assert lifecycle.consume(accepted("a2"), {"m1": market()}) is False
    assert len(lifecycle.data["positions"]) == 1
    position = next(iter(lifecycle.data["positions"].values()))
    assert position["entry_cost"] == 4.1
    assert position["real_order_submissions"] == 0
    assert position["config_version"] == "shadow-portfolio-v2"
    assert len(position["config_hash"]) == 64


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


def test_lottery_close_window_and_total_notional_limits_are_enforced(tmp_path):
    limits = replace(PortfolioLimits(), lottery_max_per_close_window=1,
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
    assert json.loads(log.read_text().splitlines()[-1])["reason"] == "directional_order_size_limit"


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
               consensus_price=101, reference_state="REFERENCE_READY")
    lifecycle.consume(row, {"m1": market()})
    venue = {"assets": {"BTC": {"chainlink_settlement_samples": [
        {"source_timestamp_ms": 1_100_000, "price": 101},
    ]}}}
    lifecycle.settle({"m1": market()}, venue, now=1200)
    complete = json.loads(log.read_text().splitlines()[-1])
    assert complete["estimated_probability"] == .7
    assert complete["net_ev"] == .2
    assert complete["consensus_price"] == 101

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

    log_row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert log_row["event_type"] == "shadow_orphaned"

