import json

from poly_arb_bot.strategy_shadow_lifecycle import StrategyShadowLifecycle, process_audit_once


def accepted(event_id="a1", strategy="late_window_directional_ev", outcome="Up"):
    return {
        "event_id": event_id, "event_type": "shadow_eval", "strategy": strategy,
        "market_id": "m1", "asset": "BTC", "timeframe": "5m", "outcome": outcome,
        "decision": "ACCEPT", "expected_fill_price": 0.4, "fees": 0.01,
        "target_size": 10, "ts": 1000,
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
