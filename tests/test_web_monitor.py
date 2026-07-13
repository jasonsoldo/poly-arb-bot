import json
import time

from poly_arb_bot.web_monitor import build_status


def test_web_status_ignores_snapshot_signals_without_current_market(tmp_path):
    (tmp_path / "live_snapshot.json").write_text(json.dumps({"signals": [{"market_id": "stale"}]}), encoding="utf-8")
    (tmp_path / "live_markets.json").write_text(json.dumps({"markets": []}), encoding="utf-8")
    state = tmp_path / "orders.json"
    state.write_text(json.dumps({"client_order_ids": {"old": "id"}}), encoding="utf-8")

    status = build_status(tmp_path, tmp_path / "orders.jsonl", state)

    assert status["signals"] == []
    assert status["counts"]["executed_orders"] == 0
    assert status["counts"]["risk_decisions"] == 1


def test_web_status_separates_model_edge_from_risk_passed(tmp_path):
    signal = {
        "market_id": "current",
        "model_probability": 0.9,
        "expected_fill_price": 0.5,
        "market_price": 0.5,
        "seconds_to_close": 2050,
        "liquidity": 100,
        "orderbook_age_ms": 10,
        "settlement_source_ok": True,
        "max_allowed_price": 0.99,
    }
    (tmp_path / "live_snapshot.json").write_text(json.dumps({"signals": [signal]}), encoding="utf-8")
    (tmp_path / "live_markets.json").write_text(json.dumps({"markets": [{"market_id": "current"}]}), encoding="utf-8")

    status = build_status(tmp_path, tmp_path / "orders.jsonl", tmp_path / "state.json")

    assert status["counts"]["raw_signals"] == 1
    assert status["counts"]["model_edges"] == 1
    assert status["counts"]["risk_passed"] == 0
    assert status["blocked_reasons"] == {"time_window": 1}


def test_web_status_does_not_count_dry_run_attempt_as_executed(tmp_path):
    (tmp_path / "live_snapshot.json").write_text(json.dumps({"signals": []}), encoding="utf-8")
    (tmp_path / "live_markets.json").write_text(json.dumps({"markets": []}), encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"client_order_ids": {"a": {"status": "dry_run"}}}), encoding="utf-8")

    status = build_status(tmp_path, tmp_path / "orders.jsonl", state)

    assert status["counts"]["executed_orders"] == 0
    assert status["counts"]["shadow_attempts"] == 1


def test_web_status_summarizes_cpp_shadow_audit(tmp_path):
    (tmp_path / "live_snapshot.json").write_text(json.dumps({"signals": []}), encoding="utf-8")
    (tmp_path / "live_markets.json").write_text(json.dumps({"markets": [{"market_id": "m1"}]}), encoding="utf-8")
    log = tmp_path / "shadow.jsonl"
    log.write_text("\n".join([
        json.dumps({"event_type": "shadow_eval", "market_id": "m1", "reason": "no_edge", "fok": True}),
        json.dumps({"event_type": "shadow_opportunity", "market_id": "m1", "fok": True, "profit": 0.1}),
    ]), encoding="utf-8")
    status = build_status(tmp_path, log, tmp_path / "state.json")
    assert status["counts"]["shadow_evaluations"] == 1
    assert status["counts"]["fok_passed"] == 1
    assert status["counts"]["shadow_opportunities"] == 1
    assert status["shadow_markets"][0]["event_type"] == "shadow_opportunity"


def test_web_status_prefers_canonical_cpp_audit_over_legacy_order_log(tmp_path):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir()
    logs.mkdir()
    (data / "live_snapshot.json").write_text(json.dumps({"signals": []}), encoding="utf-8")
    (data / "live_markets.json").write_text(json.dumps({"markets": [{"market_id": "m1"}]}), encoding="utf-8")
    legacy = logs / "orders.jsonl"
    legacy.write_text(json.dumps({"event_type": "order_decision"}), encoding="utf-8")
    (logs / "shadow-audit.jsonl").write_text(
        json.dumps({"event_type": "shadow_eval", "market_id": "m1", "reason": "no_edge", "fok": True}),
        encoding="utf-8",
    )

    status = build_status(data, legacy, tmp_path / "state.json")

    assert status["counts"]["shadow_evaluations"] == 1
    assert status["shadow_markets"][0]["market_id"] == "m1"


def test_web_status_exposes_cpp_reference_prices(tmp_path):
    (tmp_path / "live_snapshot.json").write_text(json.dumps({"signals": []}), encoding="utf-8")
    (tmp_path / "live_markets.json").write_text(json.dumps({"markets": []}), encoding="utf-8")
    (tmp_path / "venue-status.json").write_text(
        json.dumps({"updated_at_ms": time.time() * 1000, "binance_btcusdt": 65000, "chainlink_btcusd": 64998, "divergence_bps": 0.3077}),
        encoding="utf-8",
    )
    status = build_status(tmp_path, tmp_path / "orders.jsonl", tmp_path / "state.json")
    assert status["reference_prices"]["binance_btcusdt"] == 65000
    assert status["reference_prices"]["chainlink_btcusd"] == 64998


def test_web_status_hides_stale_reference_prices(tmp_path):
    (tmp_path / "live_snapshot.json").write_text(json.dumps({"signals": []}), encoding="utf-8")
    (tmp_path / "live_markets.json").write_text(json.dumps({"markets": []}), encoding="utf-8")
    (tmp_path / "venue-status.json").write_text(
        json.dumps({"updated_at_ms": 1, "binance_btcusdt": 65000, "chainlink_btcusd": 64998}), encoding="utf-8"
    )
    status = build_status(tmp_path, tmp_path / "orders.jsonl", tmp_path / "state.json")
    assert status["reference_prices"]["stale"] is True
    assert status["reference_prices"]["binance_btcusdt"] is None


def test_web_status_combines_fresh_clob_and_reference_health(tmp_path):
    now = time.time()
    (tmp_path / "live_snapshot.json").write_text(json.dumps({"signals": []}), encoding="utf-8")
    (tmp_path / "live_markets.json").write_text(json.dumps({"markets": [{"market_id": "m1"}]}), encoding="utf-8")
    (tmp_path / "venue-status.json").write_text(json.dumps({"updated_at_ms": now * 1000}), encoding="utf-8")
    (tmp_path / "shadow-health.json").write_text(
        json.dumps({"updated_at": now, "ws_connected": True, "ready_markets": 1}), encoding="utf-8"
    )
    status = build_status(tmp_path, tmp_path / "orders.jsonl", tmp_path / "state.json")
    assert status["system_status"] == "ONLINE"
    assert status["shadow_health"]["ready_markets"] == 1


def test_web_status_exposes_shadow_execution_state(tmp_path):
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    data_dir.mkdir()
    state_dir.mkdir()
    (state_dir / "shadow-execution.json").write_text(
        json.dumps({"state": "ORPHAN_HOLD", "market_id": "m1", "updated_at": 123}),
        encoding="utf-8",
    )

    status = build_status(data_dir, tmp_path / "missing.jsonl", state_dir / "orders.json")

    assert status["shadow_execution"]["state"] == "ORPHAN_HOLD"
    assert status["shadow_execution"]["real_order_submissions"] == 0


def test_web_status_uses_full_audit_counts_not_recent_display_window(tmp_path):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir(); logs.mkdir()
    (data / "live_snapshot.json").write_text(json.dumps({"signals": []}), encoding="utf-8")
    (data / "live_markets.json").write_text(json.dumps({"markets": [{"market_id": "m1"}]}), encoding="utf-8")
    rows = [json.dumps({"event_type": "shadow_eval", "market_id": "m1", "reason": "no_edge", "fok": True})] * 1005
    (logs / "shadow-audit.jsonl").write_text("\n".join(rows), encoding="utf-8")
    status = build_status(data, logs / "orders.jsonl", tmp_path / "state.json")
    assert status["counts"]["shadow_evaluations"] == 1005
    assert status["counts"]["fok_passed"] == 1005


def test_web_status_builds_seven_by_four_market_matrix(tmp_path):
    now = time.time()
    markets = [
        {"market_id": "btc-now", "asset": "BTC", "interval": "5m", "close_ts": now + 100},
        {"market_id": "btc-next", "asset": "BTC", "interval": "5m", "close_ts": now + 400},
        {"market_id": "hype", "asset": "HYPE", "interval": "4h", "close_ts": now + 10000},
    ]
    (tmp_path / "live_markets.json").write_text(json.dumps({"markets": markets}), encoding="utf-8")
    status = build_status(tmp_path, tmp_path / "missing.jsonl", tmp_path / "state.json")
    assert set(status["market_matrix"]) == {"BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE"}
    assert set(status["market_matrix"]["BTC"]) == {"5m", "15m", "1h", "4h"}
    assert status["market_matrix"]["BTC"]["5m"]["count"] == 2
    assert status["market_matrix"]["HYPE"]["4h"]["count"] == 1


def test_web_status_marks_reference_assets_independently_stale(tmp_path):
    now_ms = time.time() * 1000
    (tmp_path / "venue-status.json").write_text(json.dumps({
        "updated_at_ms": now_ms,
        "assets": {
            "BTC": {"supported": True, "binance": 65000, "chainlink": 64999,
                    "binance_source_age_ms": 5, "chainlink_source_age_ms": 6},
            "ETH": {"supported": True, "binance": 3000, "chainlink": 2999,
                    "binance_source_age_ms": 20000, "chainlink_source_age_ms": 20000},
            "HYPE": {"supported": False, "binance": None, "chainlink": None},
        },
    }), encoding="utf-8")
    status = build_status(tmp_path, tmp_path / "missing.jsonl", tmp_path / "state.json")
    assert status["reference_prices"]["assets"]["BTC"]["binance"] == 65000
    assert status["reference_prices"]["assets"]["ETH"]["binance"] is None
    assert status["reference_prices"]["assets"]["HYPE"]["supported"] is False
