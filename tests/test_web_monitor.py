import json
import threading
import time

import poly_arb_bot.web_monitor as web_monitor
from poly_arb_bot.web_monitor import _jsonl, _strategy_counts, build_status


def test_strategy_counts_ignores_retired_terminal_hedge_events(tmp_path):
    path = tmp_path / "strategy.jsonl"
    rows = [
        {"event_id": "raw", "event_type": "shadow_eval",
         "strategy": "late_window_directional_ev", "decision": "ACCEPT",
         "market_id": "m1", "outcome": "Up", "estimated_probability": 0.96},
        {"event_id": "hedge-reject", "event_type": "shadow_hedge_eval",
         "strategy": "late_window_directional_ev", "decision": "REJECT",
         "market_id": "m1", "main_outcome": "Up", "reason": "hedge_price_above_limit"},
        {"event_id": "hedge-accept", "event_type": "shadow_hedged_opportunity",
         "strategy": "late_window_directional_ev", "decision": "ACCEPT",
         "market_id": "m2", "main_outcome": "Down"},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    result = _strategy_counts((path,))
    counts = result["late_window_directional_ev"]

    assert counts["evaluations"] == 1
    assert counts["accepts"] == 1
    assert "terminal_hedge_evaluations" not in counts
    assert "_terminal_hedge" not in result


def test_web_status_does_not_expose_retired_terminal_hedge(tmp_path):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir(); logs.mkdir()
    (data / "live_markets.json").write_text(
        json.dumps({"markets": [{"market_id": "m1", "asset": "BTC", "interval": "5m"}]}),
        encoding="utf-8",
    )
    (logs / "shadow-audit.jsonl").write_text("", encoding="utf-8")
    event = {
        "ts": time.time(), "event_id": "hedged", "event_type": "shadow_hedged_opportunity",
        "strategy": "late_window_directional_ev", "market_id": "m1", "decision": "ACCEPT",
        "main_outcome": "Up", "hedge_outcome": "Down", "main_size": 10,
        "hedge_size": 5, "main_expected_fill_price": .6,
        "hedge_expected_fill_price": .03, "total_cost": 8.5,
        "main_win_pnl": 1.5, "reversal_pnl": -3.5,
        "expected_portfolio_pnl": 1.2, "worst_case_pnl": -3.5,
        "seconds_to_close": 9,
    }
    (logs / "strategy-audit.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    status = build_status(data, logs / "legacy.jsonl", tmp_path / "state.json")

    assert "current_terminal_hedge" not in status
    assert "terminal_hedge" not in status
    assert "terminal_hedge_evaluations" not in status["counts"]


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
    assert status["shadow_execution"]["real_order_submissions"] is None


def test_web_status_does_not_mask_real_execution_counters(tmp_path):
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    data_dir.mkdir()
    state_dir.mkdir()
    (state_dir / "shadow-execution.json").write_text(json.dumps({
        "state": "IDLE", "real_order_submissions": 2,
        "real_orders": 1, "real_fills": 1,
    }), encoding="utf-8")
    (state_dir / "strategy-shadow.json").write_text(json.dumps({
        "positions": {}, "completed": [],
        "real_order_submissions": 3, "real_orders": 2, "real_fills": 1,
    }), encoding="utf-8")

    status = build_status(data_dir, tmp_path / "missing.jsonl", state_dir / "orders.json")

    assert status["shadow_execution"]["real_order_submissions"] == 2
    assert status["shadow_execution"]["real_orders"] == 1
    assert status["shadow_execution"]["real_fills"] == 1
    assert status["shadow_lifecycle"]["real_order_submissions"] == 3
    assert status["shadow_lifecycle"]["real_orders"] == 2
    assert status["shadow_lifecycle"]["real_fills"] == 1


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


def test_web_status_reports_reference_readiness_per_market(tmp_path):
    now_ms = time.time() * 1000
    (tmp_path / "live_markets.json").write_text(json.dumps({"markets": [
        {"market_id": "bnb-1h", "asset": "BNB", "interval": "1h", "settlement_source": "binance"},
        {"market_id": "bnb-5m", "asset": "BNB", "interval": "5m", "settlement_source": "chainlink"},
    ]}), encoding="utf-8")
    (tmp_path / "venue-status.json").write_text(json.dumps({
        "updated_at_ms": now_ms,
        "assets": {"BNB": {"sources": {
            "binance": {
                "symbol": "bnbusdt", "market_type": "spot", "quote_currency": "USDT",
                "price": 582.0, "message_age_ms": 100, "status": "FRESH",
            },
            "coinbase": {
                "symbol": "BNB-USD", "market_type": "spot", "quote_currency": "USD",
                "price": 581.9, "message_age_ms": 8_000, "status": "FRESH",
            },
            "chainlink": {
                "symbol": "bnb/usd", "market_type": "oracle", "quote_currency": "USD",
                "price": 581.8, "message_age_ms": 50_000, "status": "FRESH",
            },
        }}},
    }), encoding="utf-8")

    status = build_status(tmp_path, tmp_path / "missing.jsonl", tmp_path / "state.json")

    assert status["market_reference_states"]["bnb-1h"]["reference_state"] == "REFERENCE_READY"
    assert status["market_reference_states"]["bnb-5m"]["reference_state"] == "REFERENCE_BLOCKED"
    assert status["market_matrix"]["BNB"]["1h"]["reference_ready"] == 1
    assert status["market_matrix"]["BNB"]["1h"]["reference_blocked"] == 0
    assert status["market_matrix"]["BNB"]["5m"]["reference_ready"] == 0
    assert status["market_matrix"]["BNB"]["5m"]["reference_blocked"] == 1


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
        "strategy": "paired_lock", "market_id": "m1",
        "strategy_config_hash": "paired-current",
        "realized_simulated_pnl": 0.25,
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
         "timeframe": "5m", "strategy_config_hash": "paired-current",
         "realized_simulated_pnl": -0.53},
        {"ts": 103.0, "event_type": "shadow_complete", "event_id": "eth-latest",
         "strategy": "paired_lock", "market_id": "eth-15m", "asset": "ETH",
         "timeframe": "15m", "strategy_config_hash": "paired-current",
         "realized_simulated_pnl": 0.12},
        {"ts": 102.0, "event_type": "shadow_complete", "event_id": "btc-latest",
         "strategy": "paired_lock", "market_id": "btc-5m-new", "asset": "BTC",
         "timeframe": "5m", "strategy_config_hash": "paired-current",
         "realized_simulated_pnl": 0.56},
    ]
    rows.extend(
            {"ts": 200.0 + index, "event_type": "shadow_complete", "event_id": f"hype-{index}",
             "strategy": "paired_lock", "market_id": f"hype-{index}", "asset": "HYPE",
             "timeframe": "5m", "strategy_config_hash": "paired-current",
             "realized_simulated_pnl": -0.01}
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
    assert status["analytics_status"] == "REBUILDING"
    assert status["system_status"] == "ONLINE"
    release.wait(1)


def test_web_status_does_not_block_on_initial_large_shadow_report(tmp_path, monkeypatch):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir(); logs.mkdir()
    (data / "shadow-health.json").write_text(json.dumps({
        "updated_at": time.time(), "ws_connected": True,
    }), encoding="utf-8")
    (data / "venue-status.json").write_text(json.dumps({
        "updated_at_ms": time.time() * 1000, "assets": {},
    }), encoding="utf-8")
    (logs / "shadow-audit.jsonl").write_text("{}\n", encoding="utf-8")
    release = threading.Event()

    class SlowReport:
        def __init__(self, *args):
            pass

        def refresh(self):
            release.wait(1)
            return web_monitor.build_report_empty()

    monkeypatch.setattr(web_monitor, "REPORT_ASYNC_THRESHOLD_BYTES", 1)
    monkeypatch.setattr(web_monitor, "IncrementalReport", SlowReport)
    threading.Timer(0.2, release.set).start()

    started = time.perf_counter()
    status = build_status(data, logs / "missing.jsonl", tmp_path / "state.json")
    elapsed = time.perf_counter() - started

    assert elapsed < 0.15
    assert status["analytics_refreshing"] is True
    assert status["analytics_status"] == "REBUILDING"
    assert status["system_status"] == "ONLINE"
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
    assert status["counts"]["active_shadow_positions"] == 1
    assert status["counts"]["simulated_opened"] == 1


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
    assert status["strategy_counts"]["late_window_directional_ev"] == {
        "evaluations": 1, "accepts": 1, "rejections": 0,
        "model_evaluations": 1, "latest_model_evaluated": True,
        "unique_opportunities": 1, "active_opportunities": 1,
    }
    assert status["strategy_counts"]["low_price_lottery_ev"] == {"evaluations": 1, "accepts": 0, "rejections": 1, "model_evaluations": 1, "latest_model_evaluated": True, "unique_opportunities": 0, "active_opportunities": 0}
    assert status["counts"]["shadow_evaluations"] == 3
    assert status["current_pair"]["reason"] == "net_cost_above_threshold"


def test_web_status_exposes_unambiguous_complete_set_counts(tmp_path):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir(); logs.mkdir()
    (data / "live_markets.json").write_text(
        json.dumps({"markets": [{"market_id": "m1"}]}), encoding="utf-8"
    )
    (logs / "shadow-audit.jsonl").write_text("\n".join([
        json.dumps({
            "ts": time.time(), "event_id": "paired-1", "event_type": "shadow_eval",
            "strategy": "paired_lock", "market_id": "m1", "decision": "REJECT",
        }),
        json.dumps({
            "ts": time.time(), "event_id": "paired-2", "event_type": "shadow_eval",
            "strategy": "paired_lock", "market_id": "m1", "decision": "ACCEPT",
        }),
    ]) + "\n", encoding="utf-8")
    (logs / "strategy-audit.jsonl").write_text("\n".join([
        json.dumps({
            "ts": time.time(), "event_id": "directional", "event_type": "shadow_eval",
            "strategy": "late_window_directional_ev", "market_id": "m1",
            "decision": "REJECT",
        }),
        json.dumps({
            "ts": time.time(), "event_id": "inventory-reject",
            "event_type": "shadow_inventory_eval",
            "strategy": "inventory_rebalancing_arb", "market_id": "m1",
            "decision": "REJECT",
        }),
        json.dumps({
            "ts": time.time(), "event_id": "inventory-action",
            "event_type": "shadow_inventory_action",
            "strategy": "inventory_rebalancing_arb", "market_id": "m1",
            "decision": "ACCEPT",
        }),
        json.dumps({
            "ts": time.time(), "event_id": "maker-quote",
            "event_type": "shadow_maker_quote_eval",
            "strategy": "maker_complete_set_arb", "market_id": "m1",
            "decision": "ACCEPT",
        }),
    ]) + "\n", encoding="utf-8")
    (logs / "shadow-execution.jsonl").write_text("\n".join([
        json.dumps({
            "ts": time.time(), "event_id": "paired-complete",
            "event_type": "shadow_complete", "strategy": "paired_lock",
            "market_id": "m1", "strategy_config_hash": "paired-current",
            "realized_simulated_pnl": .1,
        }),
        json.dumps({
            "ts": time.time(), "event_id": "inventory-complete",
            "event_type": "shadow_complete", "strategy": "inventory_rebalancing_arb",
            "market_id": "m1", "strategy_config_hash": "inventory-current",
            "realized_simulated_pnl": .2,
        }),
    ]) + "\n", encoding="utf-8")
    (data / "shadow-health.json").write_text(json.dumps({
        "paired_config_hash": "paired-current",
        "inventory_config_hash": "inventory-current",
        "maker_config_hash": "maker-current",
        "maker_quote_geometry_candidates": 7,
        "maker_trade_events": 11,
        "maker_single_leg_trade_throughs": 3,
        "maker_both_leg_trade_throughs": 1,
    }), encoding="utf-8")

    status = build_status(data, logs / "legacy.jsonl", tmp_path / "state.json")
    counts = status["counts"]

    assert counts["total_strategy_evaluations"] == 4
    assert counts["probability_strategy_evaluations"] == 1
    assert counts["paired_evaluations"] == 2
    assert "inventory_evaluations" not in counts
    assert "inventory_actions" not in counts
    assert counts["maker_evaluations"] == 1
    assert counts["maker_quote_candidates"] == 1
    assert counts["maker_quote_geometry_candidates"] == 7
    assert counts["maker_trade_events"] == 11
    assert counts["maker_single_leg_trade_throughs"] == 3
    assert counts["maker_both_leg_trade_throughs"] == 1
    assert counts["complete_set_evaluations"] == 3
    assert counts["locked_complete"] == 1


def test_web_status_exposes_split_sell_as_independent_locked_method(tmp_path):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir()
    logs.mkdir()
    now = time.time()
    (data / "live_markets.json").write_text(json.dumps({
        "markets": [{
            "market_id": "m1", "asset": "BTC", "interval": "5m",
            "close_ts": now + 100,
        }],
    }), encoding="utf-8")
    (logs / "shadow-audit.jsonl").write_text(json.dumps({
        "ts": now,
        "event_id": "split-eval",
        "event_type": "shadow_split_sell_eval",
        "strategy": "split_sell_lock",
        "market_id": "m1",
        "up_sell_vwap": .54,
        "down_sell_vwap": .49,
        "combined_bid_vwap": 1.03,
        "observed_profit_threshold_bid_sum": 1.01,
        "profit_threshold_shortfall": 0,
        "required_gross_improvement_bps": 0,
        "net_proceeds": 10.22,
        "split_collateral_cost": 10,
        "locked_profit": .22,
        "decision": "ACCEPT",
        "reason": "split_sell_opportunity",
    }) + "\n", encoding="utf-8")
    (logs / "strategy-audit.jsonl").write_text("", encoding="utf-8")
    (logs / "shadow-execution.jsonl").write_text(json.dumps({
        "ts": now,
        "event_id": "split-complete",
        "event_type": "shadow_complete",
        "strategy": "split_sell_lock",
        "market_id": "m1",
        "strategy_config_hash": "split-current",
        "realized_simulated_pnl": .22,
    }) + "\n", encoding="utf-8")
    (data / "shadow-health.json").write_text(json.dumps({
        "split_sell_config_hash": "split-current",
        "session_strategy_counts": {
            "split_sell_lock": {
                "evaluations": 3, "accepts": 1, "rejections": 2,
            },
        },
    }), encoding="utf-8")

    status = build_status(
        data, logs / "shadow-audit.jsonl", tmp_path / "orders.json"
    )

    assert status["counts"]["split_sell_evaluations"] == 1
    assert status["counts"]["split_sell_accepts"] == 1
    assert status["counts"]["session_split_sell_evaluations"] == 3
    assert status["counts"]["session_split_sell_accepts"] == 1
    assert status["current_split_sell"]["locked_profit"] == .22
    assert status["current_split_sell"]["combined_bid_vwap"] == 1.03
    assert status["performance_by_strategy"]["split_sell_lock"]["completed"] == 1
    assert status["performance_by_strategy"]["split_sell_lock"]["simulated_pnl"] == .22


def test_web_status_ranks_latest_split_sell_near_misses(tmp_path):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir()
    logs.mkdir()
    now = time.time()
    markets = [
        {"market_id": "m1", "asset": "BTC", "interval": "5m", "close_ts": now + 100},
        {"market_id": "m2", "asset": "ETH", "interval": "15m", "close_ts": now + 200},
    ]
    (data / "live_markets.json").write_text(
        json.dumps({"markets": markets}), encoding="utf-8"
    )
    rows = [
        {
            "ts": now, "event_id": "m1-new", "event_type": "shadow_split_sell_eval",
            "strategy": "split_sell_lock", "market_id": "m1", "asset": "BTC",
            "timeframe": "5m", "decision": "REJECT",
            "reason": "split_sell_profit_below_threshold",
            "profit_threshold_shortfall": .12,
            "required_gross_improvement_bps": 120,
        },
        {
            "ts": now - 1, "event_id": "m2-new", "event_type": "shadow_split_sell_eval",
            "strategy": "split_sell_lock", "market_id": "m2", "asset": "ETH",
            "timeframe": "15m", "decision": "REJECT",
            "reason": "split_sell_profit_below_threshold",
            "profit_threshold_shortfall": .04,
            "required_gross_improvement_bps": 40,
        },
        {
            "ts": now - 2, "event_id": "m1-old", "event_type": "shadow_split_sell_eval",
            "strategy": "split_sell_lock", "market_id": "m1", "asset": "BTC",
            "timeframe": "5m", "decision": "REJECT",
            "reason": "split_sell_profit_below_threshold",
            "profit_threshold_shortfall": .01,
            "required_gross_improvement_bps": 10,
        },
    ]
    (logs / "shadow-audit.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    (logs / "strategy-audit.jsonl").write_text("", encoding="utf-8")

    status = build_status(
        data, logs / "shadow-audit.jsonl", tmp_path / "orders.json"
    )

    assert [row["market_id"] for row in status["split_sell_near_misses"]] == [
        "m2", "m1",
    ]
    assert status["split_sell_near_misses"][1]["profit_threshold_shortfall"] == .12


def test_web_status_exposes_incremental_arbitrage_research(tmp_path):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir()
    logs.mkdir()
    now = time.time()
    (data / "live_markets.json").write_text(json.dumps({
        "markets": [{
            "market_id": "m1", "asset": "BTC", "interval": "5m",
            "close_ts": now + 100,
        }],
    }), encoding="utf-8")
    (logs / "shadow-audit.jsonl").write_text(json.dumps({
        "ts": now,
        "event_id": "pair-accept",
        "event_type": "shadow_eval",
        "strategy": "paired_lock",
        "market_id": "m1",
        "asset": "BTC",
        "timeframe": "5m",
        "generation": 1,
        "session": 2,
        "decision": "ACCEPT",
        "fok": True,
        "locked_profit": .05,
        "expected_execution_value": .03,
        "size": 10,
        "time_between_legs_us": 50_000,
    }) + "\n", encoding="utf-8")
    (logs / "strategy-audit.jsonl").write_text("", encoding="utf-8")

    status = build_status(
        data, logs / "shadow-audit.jsonl", tmp_path / "orders.json"
    )

    research = status["arbitrage_research"]
    assert research["semantics"] == "RESEARCH_ONLY_NOT_ORDERS_OR_PNL"
    assert research["funnels"]["paired_lock"]["independent_episodes"] == 1
    assert research["repeatable_patterns"][0]["asset"] == "BTC"
    assert research["repeatable_patterns"][0]["classification"] == "OBSERVED"


def test_web_arbitrage_research_uses_book_executable_not_fill_semantics():
    report = web_monitor._empty_arbitrage_research()
    funnel = report["funnels"]["paired_lock"]

    assert funnel["shadow_attempts"] == 0
    assert funnel["leg_1_book_executable"] == 0
    assert funnel["both_legs_book_executable"] == 0
    assert funnel["orphaned"] == 0
    assert funnel["invalidated"] == 0
    assert "both_legs_filled" not in funnel
    assert report["no_repeatable_arbitrage"] is True
    assert report["conclusion"] == "NO REPEATABLE ARBITRAGE FOUND"


def test_web_merge_keeps_leg_order_and_config_hash_cohorts_separate():
    base = {
        "funnels": {}, "counterfactual_patterns": [],
        "semantics": "RESEARCH_ONLY_NOT_ORDERS_OR_PNL",
    }
    first = dict(base, repeatable_patterns=[{
        "strategy": "paired_lock", "asset": "BTC", "timeframe": "5m",
        "target_size": 10, "delay_ms": 50, "leg_order": "UP_THEN_DOWN",
        "config_hash": "a", "independent_episodes": 20,
        "distinct_close_windows": 12, "classification": "OUT_OF_SAMPLE_VALIDATED",
        "profitable_capacity": 10,
    }])
    second = dict(base, repeatable_patterns=[{
        "strategy": "paired_lock", "asset": "BTC", "timeframe": "5m",
        "target_size": 10, "delay_ms": 50, "leg_order": "DOWN_THEN_UP",
        "config_hash": "a", "independent_episodes": 2,
        "distinct_close_windows": 2, "classification": "OBSERVED",
        "profitable_capacity": None,
    }])

    merged = web_monitor._merge_arbitrage_research([first, second])

    assert len(merged["repeatable_patterns"]) == 2
    assert merged["repeatable_patterns"][0]["classification"] == "OUT_OF_SAMPLE_VALIDATED"
    assert merged["no_repeatable_arbitrage"] is False


def test_web_status_separates_engine_session_counts_and_legacy_inventory(tmp_path):
    data = tmp_path / "data"
    state = tmp_path / "state"
    data.mkdir(); state.mkdir()
    now = time.time()
    (data / "shadow-health.json").write_text(json.dumps({
        "updated_at": now,
        "ws_connected": True,
        "run_id": "run-1",
        "engine_started_at": now - 30,
        "inventory_config_hash": "current-inventory",
        "session_strategy_counts": {
            "paired_lock": {"evaluations": 12, "accepts": 2, "rejections": 10},
            "inventory_rebalancing_arb": {
                "evaluations": 8, "accepts": 1, "rejections": 7,
            },
        },
    }), encoding="utf-8")
    (state / "strategy-shadow.json").write_text(json.dumps({
        "positions": {},
        "completed": [],
        "complete_set_inventory": {
            "legacy": {
                "market_id": "legacy", "asset": "DOGE", "timeframe": "4h",
                "up_quantity": 0, "down_quantity": 10,
                "up_cost": 0, "down_cost": 7.65,
                "close_ts": now + 100,
                "origin_config_hash": "old-inventory",
            },
            "current": {
                "market_id": "current", "asset": "BTC", "timeframe": "5m",
                "up_quantity": 2, "down_quantity": 0,
                "up_cost": .4, "down_cost": 0,
                "close_ts": now + 50,
                "origin_config_hash": "current-inventory",
            },
        },
    }), encoding="utf-8")

    status = build_status(data, tmp_path / "missing.jsonl", state / "orders.json")

    assert status["engine_session"]["run_id"] == "run-1"
    assert status["engine_session"]["evaluations"] == 12
    assert status["counts"]["session_paired_evaluations"] == 12
    assert "session_inventory_actions" not in status["counts"]
    assert status["session_strategy_counts"]["maker_complete_set_arb"] == {
        "evaluations": 0, "accepts": 0, "rejections": 0,
    }
    cohorts = status["shadow_lifecycle"]["inventory_cohorts"]
    assert cohorts["legacy"]["positions"] == 1
    assert cohorts["legacy"]["cost"] == 7.65
    assert cohorts["current"]["positions"] == 1
    assert cohorts["current"]["cost"] == .4
    inventory = {
        row["market_id"]: row
        for row in status["shadow_lifecycle"]["complete_set_inventory"]
    }
    assert inventory["legacy"]["cohort"] == "LEGACY"
    assert inventory["current"]["cohort"] == "CURRENT"


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


def test_strategy_count_cache_resumes_from_disk_summary(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(json.dumps({
        "event_id": "1", "event_type": "shadow_eval", "strategy": "paired_lock",
        "market_id": "m1", "decision": "REJECT",
    }) + "\n", encoding="utf-8")
    assert _strategy_counts((path,))["paired_lock"]["evaluations"] == 1

    web_monitor._STRATEGY_COUNT_CACHE.pop(str(path.resolve()))
    assert _strategy_counts((path,))["paired_lock"]["evaluations"] == 1
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "event_id": "2", "event_type": "shadow_eval", "strategy": "paired_lock",
            "market_id": "m1", "decision": "ACCEPT",
        }) + "\n")
    assert _strategy_counts((path,))["paired_lock"]["evaluations"] == 2


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


def test_web_counts_and_exposes_microstructure_reversion_book_evidence(tmp_path):
    data = tmp_path / "data"; logs = tmp_path / "logs"
    data.mkdir(); logs.mkdir()
    (logs / "strategy-audit.jsonl").write_text("\n".join([
        json.dumps({
            "ts": time.time() - 1, "event_id": "r1", "event_type": "shadow_reversion_eval",
            "strategy": "microstructure_reversion", "market_id": "m1", "asset": "BTC",
            "timeframe": "5m", "decision": "ACCEPT", "reason": "discount_below_anchor",
        }),
        json.dumps({
            "ts": time.time(), "event_id": "r2",
            "event_type": "shadow_reversion_exit_book_executable",
            "strategy": "microstructure_reversion", "market_id": "m1", "asset": "BTC",
            "timeframe": "5m", "decision": "EXIT_EXECUTABLE",
            "reason": "net_profit_exit_book_executable", "net_profit": .12,
            "observation_semantics": "BOOK_EXECUTABLE_NOT_FILL",
        }),
    ]) + "\n", encoding="utf-8")

    status = build_status(data, logs / "missing.jsonl", tmp_path / "state.json")

    counts = status["strategy_counts"]["microstructure_reversion"]
    assert counts["evaluations"] == 1
    assert counts["accepts"] == 1
    assert status["strategy_latest"]["microstructure_reversion"]["net_profit"] == .12

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
        "current_risk_halts": {},
        "would_halt_reasons": {"low_price_lottery_ev": "lottery_consecutive_loss_limit"},
        "calibration_bypasses": {"lottery_consecutive_loss_limit": 7},
        "calibration_mode": True,
        "portfolio_limits_enforced": False,
        "risk_mode": "CALIBRATION_UNTHROTTLED",
        "probability_predictions": {"pending": {
            "market_id": "m1", "strategy": "late_window_directional_ev",
        }},
        "completed_predictions": ["p1:complete", "p2:complete"],
        "probability_calibration": {
            "late_window_directional_ev": {
                "samples": 2, "sum_expected_up_probability": 1.0,
                "sum_actual_up": 1, "sum_brier_score": .68,
                "sum_log_loss": 1.832581463748,
                "origin_accepted": 1, "origin_rejected": 1,
                "calibration_buckets": {
                    "0.2-0.3": {"samples": 1, "sum_probability": .2, "actual_up": 1},
                },
            },
        },
    }), encoding="utf-8")

    status = build_status(data, tmp_path / "missing.jsonl", state / "orders.json")
    lifecycle = status["shadow_lifecycle"]

    assert lifecycle["open_positions"] == 2
    assert lifecycle["active_positions"] == 1
    assert lifecycle["settlement_pending"] == 1
    assert lifecycle["orphaned_positions"] == 1
    assert len(lifecycle["positions"]) == 2
    assert lifecycle["calibration_mode"] is True
    assert lifecycle["current_risk_halts"] == {}
    assert lifecycle["would_halt_reasons"] == {
        "low_price_lottery_ev": "lottery_consecutive_loss_limit"
    }
    assert lifecycle["calibration_bypasses"] == {"lottery_consecutive_loss_limit": 7}
    assert lifecycle["pending_predictions"] == 1
    assert lifecycle["completed_predictions"] == 2
    assert status["probability_observations"]["pending"] == 1
    assert status["probability_observations"]["settled"] == 2
    assert status["probability_observations"]["semantics"] == "CALIBRATION_ONLY_NOT_ORDERS_OR_PNL"
    assert status["probability_observations"]["by_strategy"]["late_window_directional_ev"] == {
        "pending": 1, "settled": 2,
    }
    calibration = status["probability_calibration"]["late_window_directional_ev"]
    assert calibration["samples"] == 2
    assert calibration["brier_score"] == .34
    assert calibration["origin_rejected"] == 1
    assert calibration["calibration_buckets"]["0.2-0.3"]["realized_up_rate"] == 1


def test_web_status_exposes_real_market_dynamic_sizing_and_active_capital(tmp_path):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    state = tmp_path / "state"
    data.mkdir(); logs.mkdir(); state.mkdir()
    (data / "live_markets.json").write_text(json.dumps({"markets": [{
        "market_id": "m1", "asset": "BTC", "interval": "5m",
    }]}), encoding="utf-8")
    paired_row = {
        "ts": time.time(), "event_id": "pair", "event_type": "shadow_eval",
        "strategy": "paired_lock", "market_id": "m1", "decision": "ACCEPT",
        "sizing_mode": "real_market_dynamic_v1", "requested_max_size": 100,
        "dynamic_target_size": 13.5, "market_minimum_size": 5,
        "dynamic_all_in_cost": 12.4, "dynamic_maximum_loss": 12.4,
        "capital_budget_usd": 20, "size_binding_constraint": "executable_depth",
        "up_vwap": .42, "down_vwap": .46, "locked_profit": 1.1,
    }
    directional_row = {
        **paired_row, "event_id": "directional", "strategy": "late_window_directional_ev",
        "dynamic_target_size": 7.25, "dynamic_all_in_cost": 3.1,
        "dynamic_maximum_loss": 3.1, "size_binding_constraint": "capital_budget",
        "config_hash": "directional-dynamic-hash",
    }
    (logs / "shadow-audit.jsonl").write_text(json.dumps(paired_row) + "\n", encoding="utf-8")
    (logs / "strategy-audit.jsonl").write_text(json.dumps(directional_row) + "\n", encoding="utf-8")
    (state / "strategy-shadow.json").write_text(json.dumps({
        "positions": {"p1": {
            "strategy": "late_window_directional_ev", "target_size": 7.25,
            "entry_cost": 3.1, "dynamic_maximum_loss": 3.1,
            "sizing_mode": "real_market_dynamic_v1",
            "market_minimum_size": 5, "capital_budget_usd": 20,
            "size_binding_constraint": "capital_budget",
            "strategy_config_hash": "directional-dynamic-hash",
        }},
        "completed": [], "real_order_submissions": 0, "real_orders": 0,
        "real_fills": 0,
    }), encoding="utf-8")

    status = build_status(data, logs / "shadow-audit.jsonl", state / "orders.json")

    assert status["current_pair"]["dynamic_target_size"] == 13.5
    assert status["current_pair"]["dynamic_all_in_cost"] == 12.4
    assert status["strategy_latest"]["late_window_directional_ev"]["capital_budget_usd"] == 20
    assert status["dynamic_sizing"]["active_positions"] == 1
    assert status["dynamic_sizing"]["active_capital_usd"] == 3.1
    assert status["dynamic_sizing"]["maximum_loss_usd"] == 3.1
    assert status["dynamic_sizing"]["invalid_active_positions"] == 0


def test_web_status_reports_invalid_dynamic_position_fields(tmp_path):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    state = tmp_path / "state"
    data.mkdir(); logs.mkdir(); state.mkdir()
    (data / "live_markets.json").write_text(json.dumps({"markets": []}), encoding="utf-8")
    (logs / "shadow-audit.jsonl").write_text("", encoding="utf-8")
    (state / "strategy-shadow.json").write_text(json.dumps({
        "positions": {"legacy": {
            "strategy": "late_window_directional_ev", "asset": "BTC",
            "timeframe": "5m", "target_size": 10, "entry_cost": 8,
        }},
    }), encoding="utf-8")

    status = build_status(data, logs / "shadow-audit.jsonl", state / "orders.json")

    sizing = status["dynamic_sizing"]
    assert sizing["invalid_active_positions"] == 1
    assert sizing["invalid_active_position_reasons"] == {
        "sizing_mode": 1, "market_minimum_size": 1,
        "dynamic_maximum_loss": 1, "capital_budget_usd": 1,
        "size_binding_constraint": 1,
    }
    assert sizing["invalid_active_position_details"][0]["position_key"] == "legacy"


def test_web_status_rereads_health_after_analytics(tmp_path, monkeypatch):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir(); logs.mkdir()
    (data / "live_markets.json").write_text(json.dumps({"markets": []}), encoding="utf-8")
    (logs / "shadow-audit.jsonl").write_text("", encoding="utf-8")
    health = data / "shadow-health.json"
    health.write_text(json.dumps({"updated_at": time.time() - 30}), encoding="utf-8")
    original = web_monitor._report_for_status

    def refresh_health(*args, **kwargs):
        result = original(*args, **kwargs)
        health.write_text(json.dumps({
            "updated_at": time.time(), "ws_connected": True,
        }), encoding="utf-8")
        return result

    monkeypatch.setattr(web_monitor, "_report_for_status", refresh_health)

    status = build_status(data, logs / "shadow-audit.jsonl", tmp_path / "orders.json")

    assert status["shadow_health"]["stale"] is False
    assert status["shadow_health"]["age_seconds"] < 5



# ---------------------------------------------------------------------------
# maker_paired_accumulate web aggregation (4th strategy panel)
# ---------------------------------------------------------------------------

def _maker_fixture(tmp_path):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    state = tmp_path / "state"
    data.mkdir(); logs.mkdir(); state.mkdir()
    (data / "live_markets.json").write_text(
        json.dumps({"markets": [{"market_id": "m1"}]}), encoding="utf-8")
    (logs / "shadow-audit.jsonl").write_text("", encoding="utf-8")
    now = time.time()
    events = [
        # episode 1: opened -> leg1 filled -> leg2 quoting (still active)
        {"ts": now - 30, "event_id": "mk-open-1", "event_type": "maker_episode_opened",
         "strategy": "maker_paired_accumulate", "episode_id": "maker-episode:aaaabbbbcccc0001",
         "market_id": "m1", "condition_id": "c1", "asset": "BTC", "timeframe": "5m",
         "decision": "ACCEPT", "reason": "maker_pair_margins_pass",
         "state_from": "IDLE", "state_to": "LEG1_WORKING", "leg1_outcome": "Down",
         "leg1_quote_price": 0.46, "leg1_order_size": 25.0, "expected_margin": 0.012,
         "seconds_to_close": 200, "shadow_fill_mode": "strict",
         "real_order_submissions": 0, "real_orders": 0, "real_fills": 0},
        {"ts": now - 20, "event_id": "mk-fill-1", "event_type": "maker_leg_filled",
         "strategy": "maker_paired_accumulate", "episode_id": "maker-episode:aaaabbbbcccc0001",
         "market_id": "m1", "condition_id": "c1", "asset": "BTC", "timeframe": "5m",
         "decision": "FILLED", "reason": "leg1_filled", "leg": 1, "outcome": "Down",
         "fill_price": 0.46, "fill_size": 25.0, "leg1_avg_price": 0.46,
         "leg1_filled_size": 25.0, "fill_mode": "strict",
         "strict_would_fill": True, "queue_would_fill": True,
         "seconds_to_close": 190, "shadow_fill_mode": "strict"},
        {"ts": now - 19, "event_id": "mk-sc-1", "event_type": "maker_episode_state_change",
         "strategy": "maker_paired_accumulate", "episode_id": "maker-episode:aaaabbbbcccc0001",
         "market_id": "m1", "condition_id": "c1", "asset": "BTC", "timeframe": "5m",
         "decision": "STATE_CHANGE", "reason": "leg1_filled",
         "state_from": "LEG1_WORKING", "state_to": "LEG1_FILLED",
         "locked_size": 0.0, "at_risk_size": 25.0, "seconds_to_close": 190},
        {"ts": now - 18, "event_id": "mk-quote-1", "event_type": "maker_quote_updated",
         "strategy": "maker_paired_accumulate", "episode_id": "maker-episode:aaaabbbbcccc0001",
         "market_id": "m1", "condition_id": "c1", "asset": "BTC", "timeframe": "5m",
         "decision": "QUOTE", "reason": "leg2_opened", "leg": 2, "outcome": "Up",
         "new_quote_price": 0.50, "leg2_max_price": 0.535, "leg2_best_bid": 0.49,
         "leg2_best_ask": 0.51, "improve_attempt": 0, "max_improves": 5,
         "seconds_to_close": 189},
        # episode 2: opened -> completed (full cost chain)
        {"ts": now - 15, "event_id": "mk-open-2", "event_type": "maker_episode_opened",
         "strategy": "maker_paired_accumulate", "episode_id": "maker-episode:dddd2222",
         "market_id": "m1", "condition_id": "c2", "asset": "ETH", "timeframe": "15m",
         "decision": "ACCEPT", "reason": "maker_pair_margins_pass",
         "state_from": "IDLE", "state_to": "LEG1_WORKING", "leg1_outcome": "Up",
         "leg1_quote_price": 0.44, "leg1_order_size": 20.0, "expected_margin": 0.02,
         "seconds_to_close": 700, "shadow_fill_mode": "strict"},
        {"ts": now - 10, "event_id": "mk-complete-2", "event_type": "maker_episode_completed",
         "strategy": "maker_paired_accumulate", "episode_id": "maker-episode:dddd2222",
         "market_id": "m1", "condition_id": "c2", "asset": "ETH", "timeframe": "15m",
         "decision": "COMPLETE", "reason": "pair_completed",
         "gross_cost": 0.94, "maker_fees": 0.0, "hedge_taker_fee": 0.0,
         "gas_cost_per_share": 0.0001, "buffer_per_share": 0.005,
         "net_cost": 0.9451, "guaranteed_payout": 1.0, "locked_profit": 0.0549,
         "locked_roi": 0.058, "locked_size": 20.0, "at_risk_size": 0.0,
         "estimated_rebate": 0.009, "estimated_rebate_label": "ESTIMATED REBATE, NOT IN REALIZED PNL",
         "realized_rebate": 0.0, "exit_path": "maker_complete",
         "leg1_avg_price": 0.44, "leg2_avg_price": 0.50, "leg2_max_price": 0.555,
         "leg1_filled_size": 20.0, "leg2_filled_size": 20.0,
         "orphan_seconds": 12.5, "orphan_max_drawdown": 0.0,
         "episode_realized_pnl": 1.196, "seconds_to_close": 650},
        # a rejected evaluation (deduped decision event)
        {"ts": now - 5, "event_id": "mk-reject-1", "event_type": "maker_episode_rejected",
         "strategy": "maker_paired_accumulate", "episode_id": None,
         "market_id": "m1", "condition_id": "c3", "asset": "SOL", "timeframe": "5m",
         "decision": "REJECT", "reason": "expected_margin_below_threshold",
         "blocking_reasons": ["expected_margin_below_threshold"],
         "seconds_to_close": 100, "shadow_fill_mode": "strict",
         "real_order_submissions": 0, "real_orders": 0, "real_fills": 0},
    ]
    (logs / "strategy-audit.jsonl").write_text(
        "\n".join(json.dumps(row) for row in events) + "\n", encoding="utf-8")
    (state / "maker-shadow.json").write_text(json.dumps({
        "updated_at": now,
        "statistics": {
            "strategy": "maker_paired_accumulate",
            "episodes_opened": 2, "episodes_completed": 1,
            "episodes_cancelled": 0, "episodes_closed_with_loss": 0,
            "active_episodes": 1, "leg1_fill_rate": 1.0,
            "leg2_completion_rate": 1.0, "orphan_rate": 0.0,
            "average_locked_profit": 1.196, "average_orphan_loss": None,
            "max_orphan_loss": None, "realized_shadow_pnl": 1.196,
            "consecutive_orphans": 0, "circuit_breaker_open": False,
            "active_total_exposure": 11.5, "active_at_risk_exposure": 11.5,
            "daily_loss": 0.0,
            "limits": {"max_notional_per_market": 25.0, "max_total_exposure": 100.0,
                       "max_at_risk_exposure": 50.0, "max_daily_loss": 5.0,
                       "max_consecutive_orphans": 3},
            "real_order_submissions": 0, "real_orders": 0, "real_fills": 0,
        },
    }), encoding="utf-8")
    return data, logs, state


def test_web_status_exposes_maker_accumulate_panel_aggregation(tmp_path):
    data, logs, state = _maker_fixture(tmp_path)

    status = build_status(data, logs / "shadow-audit.jsonl", state / "orders.json")
    maker = status["maker_accumulate"]

    assert maker["available"] is True
    assert maker["semantics"] == "SHADOW_ONLY_NOT_ORDERS_OR_REAL_PNL"
    # state machine distribution (audit window)
    assert maker["state_counts"]["LEG2_WORKING"] == 0  # quote update is not a state change
    assert maker["state_counts"]["LEG1_FILLED"] == 1
    assert maker["state_counts"]["COMPLETE"] == 1
    assert maker["episodes_in_window"] == 2
    # active episode detail
    active = maker["active_episodes"]
    assert len(active) == 1
    row = active[0]
    assert row["episode_short"] == "bbbbcccc0001"[-8:]
    assert row["state"] == "LEG1_FILLED"
    assert row["leg1_avg_price"] == 0.46
    assert row["leg2_quote_price"] == 0.50
    assert row["leg2_max_price"] == 0.535
    assert row["at_risk_size"] == 25.0
    assert row["at_risk_usd"] == 25.0 * 0.46
    assert row["orphan_seconds"] is not None and row["orphan_seconds"] >= 15
    # cost chain from the latest terminal event
    chain = maker["cost_chain"]
    assert chain["exit_path"] == "maker_complete"
    assert chain["maker_fees"] == 0.0
    assert chain["locked_profit"] == 0.0549
    assert chain["estimated_rebate"] == 0.009
    assert "ESTIMATED REBATE" in chain["estimated_rebate_label"]
    assert chain["episode_realized_pnl"] == 1.196
    # strict vs queue dual accounting from maker_leg_filled events
    fills = maker["fill_modes"]
    assert fills["samples"] == 1
    assert fills["strict_would_fill"] == 1
    assert fills["queue_would_fill"] == 1
    assert fills["shadow_fill_mode"] == "strict"
    # decision stats (opened=ACCEPT x2, rejected=REJECT x1)
    assert maker["decisions"]["evaluations"] == 3
    assert maker["decisions"]["accepts"] == 2
    assert maker["decisions"]["rejections"] == 1
    assert maker["top_reject_reasons"] == [
        {"reason": "expected_margin_below_threshold", "count": 1}]
    # portfolio limits from the bridge state statistics (real machine state)
    expo = maker["exposure"]
    assert expo["total"] == 11.5
    assert expo["at_risk"] == 11.5
    assert expo["daily_loss"] == 0.0
    assert expo["circuit_breaker_open"] is False
    assert expo["limits"]["max_total_exposure"] == 100.0
    assert maker["statistics"]["realized_shadow_pnl"] == 1.196


def test_web_status_maker_accumulate_empty_state_is_not_fabricated(tmp_path):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir(); logs.mkdir()
    (data / "live_markets.json").write_text(
        json.dumps({"markets": []}), encoding="utf-8")
    (logs / "shadow-audit.jsonl").write_text("", encoding="utf-8")

    status = build_status(data, logs / "shadow-audit.jsonl", tmp_path / "orders.json")
    maker = status["maker_accumulate"]

    assert maker["available"] is False
    assert maker["active_episodes"] == []
    assert maker["cost_chain"] is None
    assert maker["statistics"] is None
    assert maker["fill_modes"]["samples"] == 0
    assert maker["decisions"] == {
        "evaluations": 0, "accepts": 0, "rejections": 0,
        "session_evaluations": 0, "session_accepts": 0, "session_rejections": 0,
    }
    assert all(count == 0 for count in maker["state_counts"].values())
