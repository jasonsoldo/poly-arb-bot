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


def test_paired_event_produces_independent_directional_and_lottery_audits():
    rows = evaluate_market_event(event(), market(), venue(), now=1000.0)
    assert len(rows) == 4
    assert {row["strategy"] for row in rows} == {"late_window_directional_ev", "low_price_lottery_ev"}
    assert {row["outcome"] for row in rows} == {"Up", "Down"}
    assert all(row["event_id"].startswith("paired-1:") for row in rows)
    assert all(row["real_order_submissions"] == 0 for row in rows)
    assert all(row["target_size"] == 10 for row in rows)
    assert all(row["config_version"] == "shadow-buy-rules-v3" for row in rows)
    assert all(len(row["config_hash"]) == 64 for row in rows)
    assert all(row["volatility_per_sqrt_second"] == .001 for row in rows)
    assert all(row["expected_move_log_std"] > 0 for row in rows)
    assert all(row["paired_book_imbalance"] == .2 for row in rows)
    assert all(row["up_final_model_z"] == (
        row["up_standardized_distance"] + row["up_momentum_z"] + row["up_imbalance_z"]
    ) for row in rows)
    assert all(row["confidence_type"] == "input_quality_not_historical_accuracy" for row in rows)


def test_probability_model_fails_closed_without_volatility_samples():
    rows = evaluate_market_event(event(), market(), venue(volatility=None), now=1000.0)
    assert all(row["decision"] == "REJECT" for row in rows)
    assert all(row["reason"] == "volatility_unavailable" for row in rows)


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
