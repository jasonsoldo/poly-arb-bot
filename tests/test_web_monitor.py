import json
import threading
import time

import poly_arb_bot.web_monitor as web_monitor
from poly_arb_bot.web_monitor import _jsonl, _strategy_counts, build_status


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
    assert status["counts"]["shadow_accepts"] == 1
    assert status["counts"]["unique_opportunities"] == 0
    assert status["counts"]["active_opportunities"] == 0
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


def test_web_status_does_not_erase_fresh_binance_when_chainlink_is_stale(tmp_path):
    now_ms = time.time() * 1000
    (tmp_path / "venue-status.json").write_text(json.dumps({
        "updated_at_ms": now_ms,
        "assets": {"BTC": {"supported": True, "binance": 65000, "chainlink": 64999,
                            "binance_source_age_ms": 5, "chainlink_source_age_ms": 20000}},
    }), encoding="utf-8")

    status = build_status(tmp_path, tmp_path / "missing.jsonl", tmp_path / "state.json")
    btc = status["reference_prices"]["assets"]["BTC"]

    assert btc["binance"] == 65000
    assert btc["binance_stale"] is False
    assert btc["chainlink"] is None
    assert btc["chainlink_stale"] is True
    assert btc["divergence_bps"] is None


def test_web_status_preserves_not_received_reference_state(tmp_path):
    now_ms = time.time() * 1000
    (tmp_path / "venue-status.json").write_text(json.dumps({
        "updated_at_ms": now_ms,
        "assets": {"BTC": {"supported": True, "binance": None, "chainlink": 65000,
                            "binance_status": "NOT_RECEIVED", "chainlink_status": "FRESH",
                            "binance_source_age_ms": -1, "chainlink_source_age_ms": 5}},
    }), encoding="utf-8")
    status = build_status(tmp_path, tmp_path / "missing.jsonl", tmp_path / "state.json")
    btc = status["reference_prices"]["assets"]["BTC"]
    assert btc["binance_status"] == "NOT_RECEIVED"
    assert btc["binance_stale"] is False
    assert btc["chainlink_status"] == "FRESH"


def test_web_status_includes_completed_shadow_analytics(tmp_path):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir(); logs.mkdir()
    (logs / "shadow-audit.jsonl").write_text(json.dumps({
        "ts": 100.0, "event_type": "shadow_opportunity", "market_id": "m1",
        "expected_execution_value": 0.25,
    }), encoding="utf-8")
    (logs / "shadow-execution.jsonl").write_text(json.dumps({
        "ts": 101.0, "event_type": "shadow_complete", "event_id": "m1:100.0:complete",
        "strategy": "paired_lock", "market_id": "m1", "realized_simulated_pnl": 0.25,
    }), encoding="utf-8")

    status = build_status(data, logs / "legacy.jsonl", tmp_path / "state.json")

    assert status["performance"]["completed"] == 1
    assert status["performance"]["simulated_pnl"] == 0.25
    assert status["counts"]["simulated_complete"] == 1
    assert status["performance_by_strategy"]["paired_lock"]["completed"] == 1


def test_web_status_exposes_latest_completed_pnl_per_asset(tmp_path):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir(); logs.mkdir()
    rows = [
        {"ts": 101.0, "event_type": "shadow_complete", "event_id": "btc-old",
         "strategy": "paired_lock", "market_id": "btc-5m-old", "asset": "BTC",
         "timeframe": "5m", "realized_simulated_pnl": -0.53},
        {"ts": 103.0, "event_type": "shadow_complete", "event_id": "eth-latest",
         "strategy": "paired_lock", "market_id": "eth-15m", "asset": "ETH",
         "timeframe": "15m", "realized_simulated_pnl": 0.12},
        {"ts": 102.0, "event_type": "shadow_complete", "event_id": "btc-latest",
         "strategy": "paired_lock", "market_id": "btc-5m-new", "asset": "BTC",
         "timeframe": "5m", "realized_simulated_pnl": 0.56},
    ]
    rows.extend(
        {"ts": 200.0 + index, "event_type": "shadow_complete", "event_id": f"hype-{index}",
         "strategy": "paired_lock", "market_id": f"hype-{index}", "asset": "HYPE",
         "timeframe": "5m", "realized_simulated_pnl": -0.01}
        for index in range(101)
    )
    (logs / "shadow-execution.jsonl").write_text(
        "\n".join(map(json.dumps, rows)) + "\n", encoding="utf-8"
    )
    (logs / "shadow-audit.jsonl").write_text("", encoding="utf-8")

    status = build_status(data, logs / "missing.jsonl", tmp_path / "state.json")

    assert status["asset_latest_pnl"]["BTC"] == {
        "pnl": 0.56, "strategy": "paired_lock", "ts": 102.0,
        "market_id": "btc-5m-new", "timeframe": "5m",
    }
    assert status["asset_latest_pnl"]["ETH"]["pnl"] == 0.12
    assert status["asset_latest_pnl"]["SOL"] is None


def test_web_status_does_not_block_on_initial_large_strategy_audit(tmp_path, monkeypatch):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir(); logs.mkdir()
    (data / "shadow-health.json").write_text(json.dumps({
        "updated_at": time.time(), "ws_connected": True,
    }), encoding="utf-8")
    (data / "venue-status.json").write_text(json.dumps({
        "updated_at_ms": time.time() * 1000, "assets": {},
    }), encoding="utf-8")
    (logs / "strategy-audit.jsonl").write_text("{}\n", encoding="utf-8")
    release = threading.Event()

    def slow_counts(paths):
        release.wait(1)
        return {
            name: {"evaluations": 0, "accepts": 0, "rejections": 0,
                   "model_evaluations": 0, "latest_model_evaluated": False,
                   "unique_opportunities": 0, "active_opportunities": 0}
            for name in ("late_window_directional_ev", "low_price_lottery_ev", "paired_lock")
        }

    monkeypatch.setattr(web_monitor, "STRATEGY_ASYNC_THRESHOLD_BYTES", 1)
    monkeypatch.setattr(web_monitor, "_strategy_counts", slow_counts)
    threading.Timer(0.2, release.set).start()

    started = time.perf_counter()
    status = build_status(data, logs / "missing.jsonl", tmp_path / "state.json")
    elapsed = time.perf_counter() - started

    assert elapsed < 0.15
    assert status["analytics_refreshing"] is True
    assert status["system_status"] == "DEGRADED"
    release.wait(1)


def test_web_status_exposes_open_strategy_shadow_positions(tmp_path):
    data = tmp_path / "data"
    state = tmp_path / "state"
    data.mkdir(); state.mkdir()
    (state / "strategy-shadow.json").write_text(json.dumps({
        "positions": {"p1": {"strategy": "late_window_directional_ev"}},
        "completed": [], "audit_offset": 0,
    }), encoding="utf-8")
    status = build_status(data, tmp_path / "missing.jsonl", tmp_path / "orders.json")
    assert status["shadow_lifecycle"]["open_positions"] == 1
    assert status["shadow_lifecycle"]["portfolio_rejections"] == {}


def test_web_status_does_not_display_future_clock_events(tmp_path):
    log = tmp_path / "audit.jsonl"
    log.write_text(json.dumps({
        "ts": time.time() + 3600, "event_type": "shadow_eval", "market_id": "future",
        "reason": "books_not_synced", "decision": "REJECT",
    }), encoding="utf-8")

    status = build_status(tmp_path, log, tmp_path / "state.json")

    assert status["events"] == []
    assert status["shadow_markets"] == []
    assert status["shadow_report"]["future_events"] == 1


def test_rejected_pair_blocks_validation_and_exposes_auditable_cost_chain(tmp_path):
    now = time.time()
    (tmp_path / "live_markets.json").write_text(json.dumps({"markets": [{"market_id": "m1"}]}), encoding="utf-8")
    log = tmp_path / "audit.jsonl"
    log.write_text(json.dumps({
        "ts": now, "event_type": "shadow_eval", "market_id": "m1", "decision": "REJECT",
        "reason": "net_cost_above_threshold", "fok": True, "size": 10,
        "up_fill": 10, "down_fill": 10, "up_vwap": .1, "down_vwap": .92,
        "gross_cost": 10.2, "up_fee": .04, "down_fee": .05, "buffer": .02,
        "net_cost": 10.31, "guaranteed_payout": 10, "locked_profit": -.31,
        "expected_execution_value": -.31, "books_synced": True,
        "source_age_ms": 20, "up_book_age_ms": 10, "down_book_age_ms": 12,
        "leg_1_fill_probability": 1, "leg_2_fill_probability": .8,
    }), encoding="utf-8")

    status = build_status(tmp_path, log, tmp_path / "state.json")

    assert status["pipeline_steps"]["validate"] == "BLOCKED"
    assert status["strategy_score"]["total"] == 0
    assert status["strategy_score"]["metrics"]["expected_execution_value"] == -.31
    assert status["strategy_score"]["checks"]["depth"] == "PASS"
    assert status["strategy_score"]["checks"]["book_sync"] == "PASS"
    assert status["current_pair"]["net_cost"] == 10.31
    assert status["current_pair"]["decision"] == "REJECT"


def test_status_explains_ready_gap_and_initializes_resyncs(tmp_path):
    now = time.time()
    (tmp_path / "live_markets.json").write_text(json.dumps({"markets": [
        {"market_id": "a", "asset": "BTC", "interval": "5m"},
        {"market_id": "b", "asset": "BTC", "interval": "15m"},
    ]}), encoding="utf-8")
    (tmp_path / "shadow-health.json").write_text(json.dumps({
        "updated_at": now, "ws_connected": True, "ready_markets": 1,
        "full_resyncs": 0, "waiting_up_snapshot": 1, "waiting_down_snapshot": 0,
    }), encoding="utf-8")

    status = build_status(tmp_path, tmp_path / "missing.jsonl", tmp_path / "state.json")

    assert status["clob_readiness"] == {
        "discovered_markets": 2, "paired_markets_ready": 1, "not_ready": 1,
        "waiting_up_snapshot": 1, "waiting_down_snapshot": 0,
    }
    assert status["shadow_health"]["resyncs"] == 0


def test_web_status_keeps_three_strategy_statistics_separate(tmp_path):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir(); logs.mkdir()
    (data / "live_markets.json").write_text(json.dumps({"markets": [{"market_id": "m1"}]}), encoding="utf-8")
    (logs / "shadow-audit.jsonl").write_text(json.dumps({
        "ts": time.time(), "event_id": "p1", "event_type": "shadow_eval", "strategy": "paired_lock",
        "market_id": "m1", "decision": "REJECT", "reason": "net_cost_above_threshold",
    }), encoding="utf-8")
    (logs / "strategy-audit.jsonl").write_text("\n".join([
        json.dumps({"ts": time.time(), "event_id": "d1", "event_type": "shadow_eval", "strategy": "late_window_directional_ev", "market_id": "m1", "decision": "ACCEPT", "reason": "positive_net_ev", "estimated_probability": .6}),
        json.dumps({"ts": time.time(), "event_id": "l1", "event_type": "shadow_eval", "strategy": "low_price_lottery_ev", "market_id": "m1", "decision": "REJECT", "reason": "entry_price_above_limit", "estimated_probability": .6}),
    ]), encoding="utf-8")

    status = build_status(data, logs / "legacy.jsonl", tmp_path / "state.json")

    assert status["strategy_counts"]["paired_lock"] == {"evaluations": 1, "accepts": 0, "rejections": 1, "model_evaluations": 0, "latest_model_evaluated": False, "unique_opportunities": 0, "active_opportunities": 0}
    assert status["strategy_counts"]["late_window_directional_ev"] == {"evaluations": 1, "accepts": 1, "rejections": 0, "model_evaluations": 1, "latest_model_evaluated": True, "unique_opportunities": 1, "active_opportunities": 1}
    assert status["strategy_counts"]["low_price_lottery_ev"] == {"evaluations": 1, "accepts": 0, "rejections": 1, "model_evaluations": 1, "latest_model_evaluated": True, "unique_opportunities": 0, "active_opportunities": 0}
    assert status["counts"]["shadow_evaluations"] == 3
    assert status["current_pair"]["reason"] == "net_cost_above_threshold"


def test_web_status_ages_each_normalized_reference_source(tmp_path):
    now_ms = time.time() * 1000
    (tmp_path / "venue-status.json").write_text(json.dumps({
        "updated_at_ms": now_ms,
        "assets": {"BTC": {"sources": {
            "coinbase": {"price": 100, "message_age_ms": 5, "status": "FRESH"},
            "kraken": {"price": None, "message_age_ms": None, "status": "NOT_RECEIVED"},
        }}},
    }), encoding="utf-8")

    status = build_status(tmp_path, tmp_path / "missing.jsonl", tmp_path / "state.json")

    btc = status["reference_prices"]["assets"]["BTC"]["sources"]
    assert btc["coinbase"]["status"] == "FRESH"
    assert btc["kraken"]["status"] == "NOT_RECEIVED"
    assert status["latency_rankings"]["coinbase"]["samples"] == 1
    assert status["latency_rankings"]["kraken"]["samples"] == 0


def test_recent_jsonl_reader_does_not_require_full_history(tmp_path):
    path = tmp_path / "large.jsonl"
    path.write_text("".join(json.dumps({"n": i}) + "\n" for i in range(5000)), encoding="utf-8")
    rows = _jsonl(path, limit=3)
    assert [row["n"] for row in rows] == [4999, 4998, 4997]


def test_strategy_count_cache_consumes_only_appended_events(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(json.dumps({"event_id": "1", "event_type": "shadow_eval", "strategy": "paired_lock", "decision": "REJECT"}) + "\n", encoding="utf-8")
    assert _strategy_counts((path,))["paired_lock"]["evaluations"] == 1
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event_id": "2", "event_type": "shadow_eval", "strategy": "paired_lock", "decision": "ACCEPT"}) + "\n")
    assert _strategy_counts((path,))["paired_lock"] == {"evaluations": 2, "accepts": 1, "rejections": 1, "model_evaluations": 0, "latest_model_evaluated": False, "unique_opportunities": 1, "active_opportunities": 1}


def test_continuous_accepts_count_as_one_unique_opportunity(tmp_path):
    path = tmp_path / "audit.jsonl"
    rows = [
        {"event_id": "1", "event_type": "shadow_eval", "strategy": "paired_lock", "market_id": "m1", "decision": "ACCEPT"},
        {"event_id": "2", "event_type": "shadow_eval", "strategy": "paired_lock", "market_id": "m1", "decision": "ACCEPT"},
        {"event_id": "3", "event_type": "shadow_eval", "strategy": "paired_lock", "market_id": "m1", "decision": "REJECT"},
        {"event_id": "4", "event_type": "shadow_eval", "strategy": "paired_lock", "market_id": "m1", "decision": "ACCEPT"},
    ]
    path.write_text("\n".join(map(json.dumps, rows)) + "\n", encoding="utf-8")
    counts = _strategy_counts((path,))["paired_lock"]
    assert counts["accepts"] == 3
    assert counts["unique_opportunities"] == 2
    assert counts["active_opportunities"] == 1


def test_web_exposes_recent_strategy_breakdown_by_asset_and_reason(tmp_path):
    data = tmp_path / "data"; logs = tmp_path / "logs"
    data.mkdir(); logs.mkdir()
    (data / "live_markets.json").write_text(json.dumps({"markets": [
        {"market_id": "btc", "asset": "BTC", "interval": "5m"},
        {"market_id": "hype", "asset": "HYPE", "interval": "5m"},
    ]}), encoding="utf-8")
    (logs / "strategy-audit.jsonl").write_text("\n".join([
        json.dumps({"ts": time.time() - 1, "event_id": "d1", "event_type": "shadow_eval",
                    "strategy": "late_window_directional_ev", "market_id": "btc", "asset": "BTC",
                    "timeframe": "5m", "decision": "REJECT", "reason": "too_early"}),
        json.dumps({"ts": time.time(), "event_id": "d2", "event_type": "shadow_eval",
                    "strategy": "late_window_directional_ev", "market_id": "hype", "asset": "HYPE",
                    "timeframe": "5m", "decision": "REJECT", "reason": "insufficient_reference_sources"}),
    ]) + "\n", encoding="utf-8")
    status = build_status(data, logs / "missing.jsonl", tmp_path / "state.json")
    assert status["strategy_latest"]["late_window_directional_ev"]["asset"] == "HYPE"
    breakdown = status["strategy_recent"]["late_window_directional_ev"]
    assert breakdown["by_asset"] == {"BTC": 1, "HYPE": 1}
    assert breakdown["rejection_reasons"] == {"too_early": 1, "insufficient_reference_sources": 1}

def test_web_status_exposes_strategy_lifecycle_position_states(tmp_path):
    data = tmp_path / "data"
    state = tmp_path / "state"
    data.mkdir()
    state.mkdir()

    (state / "strategy-shadow.json").write_text(json.dumps({
        "positions": {
            "active": {"lifecycle_state": "ACTIVE", "strategy": "late_window_directional_ev"},
            "pending": {"lifecycle_state": "SETTLEMENT_PENDING", "strategy": "low_price_lottery_ev"},
        },
        "orphaned_positions": [
            {"lifecycle_state": "ORPHANED", "strategy": "late_window_directional_ev"}
        ],
        "completed": [],
        "portfolio_rejections": {},
    }), encoding="utf-8")

    status = build_status(data, tmp_path / "missing.jsonl", state / "orders.json")
    lifecycle = status["shadow_lifecycle"]

    assert lifecycle["open_positions"] == 2
    assert lifecycle["active_positions"] == 1
    assert lifecycle["settlement_pending"] == 1
    assert lifecycle["orphaned_positions"] == 1
    assert len(lifecycle["positions"]) == 2

