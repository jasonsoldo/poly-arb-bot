import json
import time

from poly_arb_bot import ev_shadow
from poly_arb_bot.ev_shadow import _historical_volatility, evaluate_market_event


def market():
    return {
        "market_id": "m1", "asset": "BTC", "interval": "5m", "window": "current",
        "open_price": 100.0, "close_ts": 1045.0, "fee_rate": 0.07,
        "settlement_source": "chainlink",
    }


def event():
    return {
        "event_id": "paired-1", "event_type": "shadow_eval", "strategy": "paired_lock",
        "market_id": "m1", "ts": 1000.0, "up_vwap": 0.45, "down_vwap": 0.56,
        "up_fee": 0.01, "down_fee": 0.01, "up_fill": 10.0, "down_fill": 10.0,
        "size": 10.0, "source_age_ms": 20.0, "books_synced": True,
        "up_best_ask": 0.44, "down_best_ask": 0.55,
        "up_available_depth": 100.0, "down_available_depth": 100.0,
        "up_book_imbalance": 0.2, "down_book_imbalance": -0.2,
        "clock_skew_ms": 10.0,
        "subscription_generation": 2, "ws_session_id": 3,
    }


def venue(volatility=0.001, model_span_seconds=120):
    return {"updated_at_ms": 1_000_000, "assets": {"BTC": {
        "fast_price": 101.0, "consensus_price": 101.0, "settlement_reference": 100.8,
        "fresh_exchange_source_count": 3, "fresh_usd_spot_source_count": 2,
        "cross_source_divergence_bps": 5.0, "reference_quorum_met": True,
        "reference_state": "REFERENCE_READY", "volatility_per_sqrt_second": volatility,
        "model_sample_count": 40, "model_sample_span_seconds": model_span_seconds,
        "momentum_bps_30s": 2.0, "clock_skew_ms": 10.0,
        "sources": {
            "coinbase": {"symbol": "BTC-USD", "market_type": "spot", "quote_currency": "USD", "price": 101.0, "bid": 100.9, "ask": 101.1, "source_timestamp": "x", "received_at": 999000, "message_age_ms": 10, "status": "FRESH"},
            "kraken": {"symbol": "BTC/USD", "market_type": "spot", "quote_currency": "USD", "price": 101.0, "bid": 100.9, "ask": 101.1, "source_timestamp": "x", "received_at": 999000, "message_age_ms": 10, "status": "FRESH"},
            "chainlink": {"symbol": "btc/usd", "market_type": "settlement", "quote_currency": "USD", "price": 100.8, "bid": None, "ask": None, "source_timestamp": "x", "received_at": 999000, "message_age_ms": 10, "status": "FRESH"},
        },
    }}}


def test_reference_age_recomputes_quorum_without_slow_optional_source(monkeypatch):
    monkeypatch.setenv("REFERENCE_MAX_AGE_MS", "3000")
    state = venue()
    state["assets"]["BTC"]["sources"]["binance"] = {
        "symbol": "BTCUSDT", "market_type": "spot", "quote_currency": "USDT",
        "price": 101.0, "bid": 100.9, "ask": 101.1,
        "message_age_ms": 9000, "status": "FRESH",
    }
    rows = evaluate_market_event(event(), market(), state, now=1000.0)
    assert all(row["reference_quorum_met"] is True for row in rows)
    assert all(row["reference_age_ms"] == 10 for row in rows)
    assert all(row["reason"] != "reference_data_stale" for row in rows)


def test_reference_age_includes_venue_file_age(monkeypatch):
    monkeypatch.setenv("REFERENCE_MAX_AGE_MS", "3000")
    state = venue()
    state["updated_at_ms"] = 996_000
    rows = evaluate_market_event(event(), market(), state, now=1000.0)
    assert all(row["reference_quorum_met"] is False for row in rows)
    assert all(row["reason"] == "insufficient_reference_sources" for row in rows)


def test_directional_reference_uses_coinbase_source_specific_age(monkeypatch):
    monkeypatch.setenv("REFERENCE_MAX_AGE_MS", "3000")
    monkeypatch.setenv("COINBASE_REFERENCE_MAX_AGE_MS", "10000")
    state = venue()
    state["assets"]["BTC"]["sources"] = {
        "binance": {
            "symbol": "BTCUSDT", "market_type": "spot", "quote_currency": "USDT",
            "price": 101.0, "message_age_ms": 100, "status": "FRESH",
        },
        "coinbase": {
            "symbol": "BTC-USD", "market_type": "spot", "quote_currency": "USD",
            "price": 101.0, "message_age_ms": 8_000, "status": "FRESH",
        },
        "chainlink": {
            "symbol": "btc/usd", "market_type": "settlement", "quote_currency": "USD",
            "price": 100.8, "message_age_ms": 100, "status": "FRESH",
        },
    }

    rows = evaluate_market_event(event(), market(), state, now=1000.0)

    assert all(row["reference_quorum_met"] is True for row in rows)
    assert all(row["reason"] != "reference_data_stale" for row in rows)


def test_coinbase_reference_age_limit_is_part_of_strategy_config_hash(monkeypatch):
    monkeypatch.setenv("COINBASE_REFERENCE_MAX_AGE_MS", "10000")
    _, first_hash = ev_shadow.strategy_config()
    monkeypatch.setenv("COINBASE_REFERENCE_MAX_AGE_MS", "9000")
    _, second_hash = ev_shadow.strategy_config()

    assert first_hash != second_hash


def test_directional_and_lottery_have_independent_strategy_hashes():
    assert ev_shadow.strategy_config("late_window_directional_ev")[1] != (
        ev_shadow.strategy_config("low_price_lottery_ev")[1]
    )


def test_lottery_model_config_does_not_change_directional_hash(monkeypatch):
    directional = ev_shadow.strategy_config("late_window_directional_ev")[1]
    lottery = ev_shadow.strategy_config("low_price_lottery_ev")[1]

    monkeypatch.setenv("LOTTERY_MARKET_BLEND", "0.25")

    assert ev_shadow.strategy_config("late_window_directional_ev")[1] == directional
    assert ev_shadow.strategy_config("low_price_lottery_ev")[1] != lottery


def test_paired_event_produces_independent_directional_and_lottery_audits():
    rows = evaluate_market_event(event(), market(), venue(), now=1000.0)
    assert len(rows) == 4
    assert {row["strategy"] for row in rows} == {"late_window_directional_ev", "low_price_lottery_ev"}
    assert {row["outcome"] for row in rows} == {"Up", "Down"}
    assert all(row["event_id"].startswith("paired-1:") for row in rows)
    assert all(row["real_order_submissions"] == 0 for row in rows)
    assert all(row["target_size"] == 10 for row in rows)
    assert all(row["config_version"] == "shadow-buy-rules-v7" for row in rows)


def test_terminal_high_confidence_signal_emits_hedged_combination():
    current_market = market()
    current_market["close_ts"] = 1010
    current_event = event()
    current_event.update(
        up_vwap=.80, up_best_ask=.80, up_fee=.01,
        down_vwap=.04, down_best_ask=.04, down_fee=.01,
    )
    state = venue(volatility=.0001)
    state["assets"]["BTC"]["settlement_reference"] = 101.0
    state["assets"]["BTC"]["sources"]["chainlink"]["price"] = 101.0

    rows = evaluate_market_event(current_event, current_market, state, now=1000.0)

    combined = next(row for row in rows if row["event_type"] == "shadow_hedged_opportunity")
    assert combined["main_outcome"] == "Up"
    assert combined["hedge_outcome"] == "Down"
    assert combined["estimated_probability"] >= .9
    assert combined["main_win_pnl"] > 0
    assert combined["reversal_pnl"] >= -1.0
    assert combined["expected_portfolio_pnl"] > 0
    assert combined["real_order_submissions"] == 0
    assert all(len(row["config_hash"]) == 64 for row in rows)
    model_rows = [row for row in rows if row["event_type"] == "shadow_eval"]
    assert all(row["volatility_per_sqrt_second"] == .0001 for row in model_rows)
    assert all(row["expected_move_log_std"] > 0 for row in model_rows)
    assert all(row["paired_book_imbalance"] == .2 for row in model_rows)
    assert all(row["up_final_model_z"] == (
        row["up_standardized_distance"] + row["up_momentum_z"] + row["up_imbalance_z"]
    ) for row in model_rows)
    assert all(row["confidence_type"] == "input_quality_not_historical_accuracy" for row in model_rows)
    assert all("strategy_config" not in row for row in rows)


def test_terminal_reject_inherits_directional_reason_and_keeps_candidate_fields():
    current_market = market()
    current_market["close_ts"] = 1010
    current_event = event()
    current_event.update(
        up_vwap=.60, up_best_ask=.60, up_fee=.01,
        down_vwap=.40, down_best_ask=.40, down_fee=.01,
    )

    state = venue()
    state["assets"]["BTC"]["settlement_reference"] = 100.0
    state["assets"]["BTC"]["momentum_bps_30s"] = 0.0
    state["assets"]["BTC"]["sources"]["chainlink"]["price"] = 100.0
    rows = evaluate_market_event(current_event, current_market, state, now=1000.0)

    combined = next(row for row in rows if row["event_type"] == "shadow_hedge_eval")
    assert combined["reason"] == "model_confidence_below_threshold"
    assert combined["main_outcome"] in {"Up", "Down"}
    assert combined["estimated_probability"] is not None
    assert combined["main_expected_fill_price"] is not None
    assert combined["seconds_to_close"] == 10
    assert combined["main_cost"] is None
    assert combined["expected_portfolio_pnl"] is None
    assert combined["volatility_per_sqrt_second"] == .001
    assert combined["model_sample_count"] == 40
    assert combined["model_sample_span_seconds"] == 120


def test_terminal_combination_is_not_evaluated_outside_directional_window():
    rows = evaluate_market_event(event(), market(), venue(), now=1000.0)

    assert not any(
        row["event_type"] in {"shadow_hedge_eval", "shadow_hedged_opportunity"}
        for row in rows
    )


def test_process_once_emits_transitions_and_bounded_decision_heartbeats(tmp_path, monkeypatch):
    audit_path = tmp_path / "shadow-audit.jsonl"
    market_path = tmp_path / "markets.json"
    venue_path = tmp_path / "venue.json"
    output_path = tmp_path / "strategy-audit.jsonl"
    state_path = tmp_path / "state.json"
    events = [
        {"event_id": "p1", "ts": 100, "decision": "REJECT"},
        {"event_id": "p2", "ts": 101, "decision": "REJECT"},
        {"event_id": "p3", "ts": 102, "decision": "ACCEPT"},
        {"event_id": "p4", "ts": 103, "decision": "ACCEPT"},
        {"event_id": "p5", "ts": 104, "decision": "REJECT"},
        {"event_id": "p6", "ts": 165, "decision": "REJECT"},
    ]
    for row in events:
        row.update(event_type="shadow_eval", strategy="paired_lock", market_id="m1")
    audit_path.write_text("\n".join(map(json.dumps, events)) + "\n", encoding="utf-8")
    market_path.write_text(json.dumps({"markets": [{"market_id": "m1"}]}), encoding="utf-8")
    venue_path.write_text("{}", encoding="utf-8")

    def fake_evaluate(event, market, venue, **kwargs):
        decision = event["decision"]
        return [{
            "event_id": event["event_id"] + ":directional:Up",
            "ts": event["ts"], "strategy": "late_window_directional_ev",
            "market_id": "m1", "outcome": "Up", "decision": decision,
            "reason": "opportunity" if decision == "ACCEPT" else "too_early",
            "config_hash": "config",
        }]

    monkeypatch.setattr(ev_shadow, "evaluate_market_event", fake_evaluate)
    monkeypatch.setenv("STRATEGY_ACCEPT_AUDIT_HEARTBEAT_SECONDS", "5")
    monkeypatch.setenv("STRATEGY_REJECT_AUDIT_HEARTBEAT_SECONDS", "60")

    emitted = ev_shadow.process_once(
        audit_path, market_path, venue_path, output_path, state_path, historical_models={}
    )
    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]

    assert emitted == 4
    assert [row["event_id"] for row in rows] == [
        "p1:directional:Up", "p3:directional:Up",
        "p5:directional:Up", "p6:directional:Up",
    ]


def test_probability_model_fails_closed_without_volatility_samples():
    rows = evaluate_market_event(event(), market(), venue(volatility=None), now=1000.0)
    assert all(row["decision"] == "REJECT" for row in rows)
    assert all(row["reason"] == "volatility_unavailable" for row in rows)


def test_probability_model_uses_market_settlement_reference_not_spot_consensus():
    state = venue(volatility=.001)
    asset = state["assets"]["BTC"]
    asset["consensus_price"] = 99.0
    asset["settlement_reference"] = 101.0
    asset["sources"]["coinbase"]["price"] = 99.0
    asset["sources"]["kraken"]["price"] = 99.0
    asset["sources"]["chainlink"]["price"] = 101.0
    current = event()
    current["up_book_imbalance"] = 0.0
    current["down_book_imbalance"] = 0.0
    asset["momentum_bps_30s"] = 0.0

    rows = evaluate_market_event(current, market(), state, now=1000.0)
    up = next(row for row in rows if row["strategy"] == "late_window_directional_ev" and row["outcome"] == "Up")

    assert up["estimated_probability"] > .5
    assert up["reference_price"] == 101.0
    assert up["probability_reference_source"] == "settlement_reference"
    assert up["probability_reference_price"] == 101.0
    assert up["distance_to_price_to_beat"] == 1.0


def test_directional_and_lottery_use_independent_probability_models():
    state = venue(volatility=.001)
    current = event()
    current["up_book_imbalance"] = 0.4
    current["down_book_imbalance"] = -0.2

    rows = evaluate_market_event(current, market(), state, now=1000.0)
    directional = next(
        row for row in rows
        if row["strategy"] == "late_window_directional_ev" and row["outcome"] == "Up"
    )
    lottery = next(
        row for row in rows
        if row["strategy"] == "low_price_lottery_ev" and row["outcome"] == "Up"
    )

    assert directional["probability_model_id"] == "directional_normal_cdf_v1"
    assert lottery["probability_model_id"] == "lottery_market_blend_v1"
    assert directional["config_hash"] != lottery["config_hash"]
    assert directional["estimated_probability"] != lottery["estimated_probability"]
    assert lottery["raw_estimated_probability"] != lottery["estimated_probability"]
    assert abs(lottery["estimated_probability"] - lottery["market_implied_probability"]) < abs(
        lottery["raw_estimated_probability"] - lottery["market_implied_probability"]
    )


def test_probability_model_fails_closed_without_minimum_time_coverage(monkeypatch):
    monkeypatch.setenv("MODEL_MIN_SAMPLE_SPAN_SECONDS", "60")
    rows = evaluate_market_event(event(), market(), venue(model_span_seconds=10), now=1000.0)
    assert all(row["estimated_probability"] is None for row in rows)
    assert all(row["reason"] == "model_sample_span_insufficient" for row in rows)
    assert all(row["model_sample_span_seconds"] == 10 for row in rows)
    assert all(row["minimum_model_sample_span_seconds"] == 60 for row in rows)


def test_binance_kline_closes_produce_per_second_volatility():
    rows = [
        [0, "0", "0", "0", "100", "0", 60_000],
        [60_000, "0", "0", "0", "101", "0", 120_000],
        [120_000, "0", "0", "0", "99", "0", 180_000],
    ]
    volatility, samples = _historical_volatility(rows)
    assert volatility > 0
    assert samples == 2


def test_historical_model_is_used_until_live_samples_are_ready():
    model = {"BTC": {"volatility_per_sqrt_second": 0.001, "model_sample_count": 40,
                     "model_sample_span_seconds": 2400}}
    rows = evaluate_market_event(event(), market(), venue(volatility=None), now=1000.0,
                                 historical_models=model)
    assert all(row["estimated_probability"] is not None for row in rows)
    assert all(row["model_source"] == "binance_historical_1m" for row in rows)


def test_historical_backfill_requests_assets_concurrently(monkeypatch):
    rows = [[index * 60_000, "0", "0", "0", str(100 + index / 10), "0"]
            for index in range(30)]

    class Response:
        def __enter__(self):
            time.sleep(0.05)
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps(rows).encode()

    monkeypatch.setattr(ev_shadow, "urlopen", lambda *_args, **_kwargs: Response())
    started = time.monotonic()
    models = ev_shadow.load_historical_models()
    assert time.monotonic() - started < 0.2
    assert set(models) == set(ev_shadow.BINANCE_SYMBOLS)


def test_chainlink_start_anchor_uses_first_source_sample_at_or_after_start():
    markets = {"m1": {"market_id": "m1", "condition_id": "c1", "asset": "BTC", "interval": "5m", "start_ts": 1000,
                       "settlement_source": "chainlink"}}
    venue_state = {"assets": {"BTC": {"chainlink_samples": [
        {"source_timestamp_ms": 999_900, "price": 99},
        {"source_timestamp_ms": 1_000_100, "price": 100},
        {"source_timestamp_ms": 1_000_200, "price": 101},
    ]}}}
    anchors = ev_shadow.capture_opening_prices(markets, venue_state, {}, now_ms=1_001_000)
    assert anchors["m1"]["price"] == 100
    assert anchors["m1"]["source_timestamp_ms"] == 1_000_100
    assert anchors["m1"]["capture_mode"] == "live_boundary"


def test_restart_backfill_derives_start_from_close_and_interval():
    markets = {"m1": {"market_id": "m1", "condition_id": "c1", "asset": "BTC", "interval": "1h",
                       "close_ts": 4600, "settlement_source": "binance"}}
    venue_state = {"assets": {"BTC": {"binance_samples": [
        {"source_timestamp_ms": 1_000_050, "price": 101},
    ]}}}
    anchors = ev_shadow.capture_opening_prices(markets, venue_state, {}, now_ms=2_000_000)
    assert anchors["m1"]["start_ts"] == 1000
    assert anchors["m1"]["price"] == 101
    assert anchors["m1"]["capture_mode"] == "restart_backfill"


def test_restart_backfill_does_not_use_current_price_outside_boundary():
    markets = {"m1": {"market_id": "m1", "condition_id": "c1", "asset": "BTC", "interval": "1h",
                       "close_ts": 4600, "settlement_source": "binance"}}
    venue_state = {"assets": {"BTC": {"binance_samples": [
        {"source_timestamp_ms": 1_020_000, "price": 105},
    ]}}}
    anchors = ev_shadow.capture_opening_prices(markets, venue_state, {}, now_ms=2_000_000)
    assert anchors == {}


def test_mismatched_persisted_anchor_is_replaced_by_current_market_anchor():
    markets = {"m1": {"market_id": "m1", "condition_id": "new", "asset": "BTC", "interval": "5m",
                       "start_ts": 1000, "settlement_source": "chainlink"}}
    existing = {"m1": {"market_id": "m1", "condition_id": "old", "asset": "BTC", "interval": "5m",
                       "start_ts": 1000, "price": 90, "source": "chainlink",
                       "source_timestamp_ms": 1_000_100}}
    venue_state = {"assets": {"BTC": {"chainlink_samples": [
        {"source_timestamp_ms": 1_000_200, "price": 100},
    ]}}}
    anchors = ev_shadow.capture_opening_prices(markets, venue_state, existing, now_ms=1_001_000)
    assert anchors["m1"]["condition_id"] == "new"
    assert anchors["m1"]["price"] == 100


def test_removed_market_anchor_is_pruned():
    existing = {"old": {"market_id": "old", "price": 90}}
    assert ev_shadow.capture_opening_prices({}, {}, existing, now_ms=1_001_000) == {}


def test_missed_chainlink_start_anchor_fails_closed():
    row = market()
    row.update(open_price=None, start_ts=900)
    rows = evaluate_market_event(event(), row, venue(), now=1000.0, opening_prices={})
    assert all(item["reason"] == "price_to_beat_capture_missed" for item in rows)


def test_stale_book_is_not_hidden_by_missing_price_to_beat():
    row = market()
    row.update(open_price=None, start_ts=900)
    old_event = event()
    old_event["source_age_ms"] = 10_000
    rows = evaluate_market_event(old_event, row, venue(), now=1000.0, opening_prices={})
    assert all(item["reason"] == "clob_book_stale" for item in rows)
    assert all(item["blocking_reasons"][:2] == [
        "clob_book_stale", "price_to_beat_capture_missed"
    ] for item in rows)


def test_binance_hourly_anchor_uses_binance_source_samples():
    markets = {"m1": {"market_id": "m1", "condition_id": "c1", "asset": "BTC", "interval": "1h",
                       "settlement_source": "binance", "start_ts": 1000}}
    venue_state = {"assets": {"BTC": {
        "chainlink_samples": [{"source_timestamp_ms": 1_000_000, "price": 99}],
        "binance_samples": [{"source_timestamp_ms": 1_000_050, "price": 101}],
    }}}
    anchors = ev_shadow.capture_opening_prices(markets, venue_state, {}, now_ms=1_001_000)
    assert anchors["m1"]["price"] == 101
    assert anchors["m1"]["source"] == "binance"


def test_unknown_settlement_source_does_not_capture_anchor():
    markets = {"m1": {"market_id": "m1", "asset": "BTC",
                       "settlement_source": "unverified", "start_ts": 1000}}
    venue_state = {"assets": {"BTC": {
        "chainlink_samples": [{"source_timestamp_ms": 1_000_000, "price": 99}],
        "binance_samples": [{"source_timestamp_ms": 1_000_000, "price": 101}],
    }}}
    assert ev_shadow.capture_opening_prices(markets, venue_state, {}, now_ms=1_001_000) == {}


def test_hourly_binance_market_does_not_require_chainlink_settlement_feed():
    row = market()
    row.update(interval="1h", settlement_source="binance", open_price=100.0, close_ts=1200)
    state = venue()
    asset = state["assets"]["BTC"]
    asset["sources"]["binance"] = {
        "symbol": "BTCUSDT", "market_type": "spot", "quote_currency": "USDT",
        "price": 101.0, "bid": 100.9, "ask": 101.1,
        "message_age_ms": 10, "status": "FRESH",
    }
    asset["sources"]["chainlink"]["status"] = "STALE"
    asset["settlement_reference"] = None
    asset["reference_quorum_met"] = False
    rows = evaluate_market_event(event(), row, state, now=1000.0)
    assert all(item["settlement_reference"] == 101.0 for item in rows)
    assert all(item["reference_quorum_met"] is True for item in rows)
    assert all(item["reason"] != "settlement_reference_unverified" for item in rows)

def test_ev_shadow_normalizes_inconsistent_start_before_anchor_lookup():
    markets = {"m1": {"market_id": "m1", "condition_id": "c1", "asset": "BTC",
                       "interval": "1h", "start_ts": 1100, "close_ts": 4600,
                       "settlement_source": "binance"}}
    venue_state = {"assets": {"BTC": {
        "sources": {"binance": {"supported": True, "status": "FRESH"}},
        "binance_samples": [{"source_timestamp_ms": 1_000_050, "price": 101, "timeframe": "1h"}],
    }}}
    anchors = ev_shadow.capture_opening_prices(markets, venue_state, {}, now_ms=2_000_000)
    assert anchors["m1"]["start_ts"] == 1000
    assert anchors["m1"]["price"] == 101


def test_model_uses_recomputed_strategy_fresh_consensus():
    state = venue()
    state["assets"]["BTC"]["consensus_price"] = None
    rows = evaluate_market_event(event(), market(), state, now=1000.0)
    assert all(row["estimated_probability"] is not None for row in rows)


def test_explicit_book_age_overrides_legacy_source_timestamp_age():
    current = event()
    current["source_age_ms"] = 10_000
    current["book_age_ms"] = 20
    rows = evaluate_market_event(current, market(), venue(), now=1000.0)
    assert all("clob_book_stale" not in row["blocking_reasons"] for row in rows)


def test_unsupported_settlement_source_drops_persisted_anchor():
    markets = {"m1": {"market_id": "m1", "condition_id": "c1", "asset": "DOGE",
                       "interval": "5m", "start_ts": 1000, "settlement_source": "chainlink"}}
    existing = {"m1": {"market_id": "m1", "condition_id": "c1", "asset": "DOGE",
                         "interval": "5m", "start_ts": 1000, "settlement_source": "chainlink",
                         "price": 0.1, "source": "chainlink", "source_timestamp_ms": 1_000_000}}
    venue_state = {"assets": {"DOGE": {"sources": {
        "chainlink": {"supported": False, "status": "UNSUPPORTED"}
    }}}}
    assert ev_shadow.capture_opening_prices(markets, venue_state, existing, now_ms=1_001_000) == {}


def test_cpp_verification_mode_writes_only_parity_mismatches(tmp_path):
    source = tmp_path / "strategy-audit.jsonl"
    output = tmp_path / "strategy-parity.jsonl"
    state = tmp_path / "verify-state.json"
    base = {
        "event_id": "cpp-1", "event_type": "shadow_eval",
        "strategy": "late_window_directional_ev", "timeframe": "5m",
        "expected_fill_price": .56, "estimated_probability": .95,
        "seconds_to_close": 10, "price_to_beat": 100,
        "fees": .01, "slippage": .002, "latency_risk_buffer": .003,
        "settlement_risk_buffer": .002, "model_uncertainty_buffer": .01,
        "execution_risk_buffer": .005, "liquidity": 100, "book_age_ms": 50,
        "reference_age_ms": 50, "clock_skew_ms": 10,
        "minimum_liquidity": 20, "maximum_slippage": .01,
        "maximum_reference_age_ms": 3000, "maximum_book_age_ms": 750,
        "maximum_clock_skew_ms": 250, "market_active": True,
        "market_tradable": True, "target_depth_ok": True,
        "momentum_bps_30s": 2, "order_book_imbalance": .1,
        "reference_quorum_met": True, "settlement_source_verified": True,
        "decision": "ACCEPT", "reason": "positive_net_ev",
        "gross_edge": .39, "net_ev": .373,
        "config_hash": ev_shadow.strategy_config()[1],
    }
    source.write_text(json.dumps(base) + "\n", encoding="utf-8")
    assert ev_shadow.process_verification_once(source, output, state) == 0
    assert not output.exists() or output.read_text(encoding="utf-8") == ""
    bad = dict(base, event_id="cpp-2", decision="REJECT")
    with source.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(bad) + "\n")
    assert ev_shadow.process_verification_once(source, output, state) == 1
    row = json.loads(output.read_text(encoding="utf-8").splitlines()[-1])
    assert row["event_type"] == "strategy_parity_mismatch"
    assert row["source_event_id"] == "cpp-2"


def test_verifier_does_not_rewrite_unchanged_checkpoint(tmp_path):
    source = tmp_path / "strategy-audit.jsonl"
    output = tmp_path / "parity.jsonl"
    state = tmp_path / "state.json"
    source.write_text("", encoding="utf-8")
    assert ev_shadow.process_verification_once(source, output, state) == 0
    assert not state.exists()
