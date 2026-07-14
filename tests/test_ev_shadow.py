import json
import time

from poly_arb_bot import ev_shadow
from poly_arb_bot.ev_shadow import _historical_volatility, evaluate_market_event


def market():
    return {
        "market_id": "m1", "asset": "BTC", "interval": "5m", "window": "current",
        "open_price": 100.0, "close_ts": 1045.0, "fee_rate": 0.07,
    }


def event():
    return {
        "event_id": "paired-1", "event_type": "shadow_eval", "strategy": "paired_lock",
        "market_id": "m1", "ts": 1000.0, "up_vwap": 0.45, "down_vwap": 0.56,
        "up_fee": 0.01, "down_fee": 0.01, "up_fill": 10.0, "down_fill": 10.0,
        "size": 10.0, "source_age_ms": 20.0, "books_synced": True,
        "subscription_generation": 2, "ws_session_id": 3,
    }


def venue(volatility=0.001):
    return {"assets": {"BTC": {
        "fast_price": 101.0, "consensus_price": 101.0, "settlement_reference": 100.8,
        "fresh_exchange_source_count": 3, "fresh_usd_spot_source_count": 2,
        "cross_source_divergence_bps": 5.0, "reference_quorum_met": True,
        "reference_state": "REFERENCE_READY", "volatility_per_sqrt_second": volatility,
        "model_sample_count": 40,
        "sources": {
            "coinbase": {"symbol": "BTC-USD", "market_type": "spot", "quote_currency": "USD", "price": 101.0, "bid": 100.9, "ask": 101.1, "source_timestamp": "x", "received_at": 999000, "message_age_ms": 10, "status": "FRESH"},
            "kraken": {"symbol": "BTC/USD", "market_type": "spot", "quote_currency": "USD", "price": 101.0, "bid": 100.9, "ask": 101.1, "source_timestamp": "x", "received_at": 999000, "message_age_ms": 10, "status": "FRESH"},
            "chainlink": {"symbol": "btc/usd", "market_type": "settlement", "quote_currency": "USD", "price": 100.8, "bid": None, "ask": None, "source_timestamp": "x", "received_at": 999000, "message_age_ms": 10, "status": "FRESH"},
        },
    }}}


def test_paired_event_produces_independent_directional_and_lottery_audits():
    rows = evaluate_market_event(event(), market(), venue(), now=1000.0)
    assert len(rows) == 4
    assert {row["strategy"] for row in rows} == {"late_window_directional_ev", "low_price_lottery_ev"}
    assert {row["outcome"] for row in rows} == {"Up", "Down"}
    assert all(row["event_id"].startswith("paired-1:") for row in rows)
    assert all(row["real_order_submissions"] == 0 for row in rows)


def test_probability_model_fails_closed_without_volatility_samples():
    rows = evaluate_market_event(event(), market(), venue(volatility=None), now=1000.0)
    assert all(row["decision"] == "REJECT" for row in rows)
    assert all(row["reason"] == "volatility_unavailable" for row in rows)


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
    model = {"BTC": {"volatility_per_sqrt_second": 0.001, "model_sample_count": 40}}
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
    markets = {"m1": {"market_id": "m1", "asset": "BTC", "start_ts": 1000}}
    venue_state = {"assets": {"BTC": {"chainlink_samples": [
        {"source_timestamp_ms": 999_900, "price": 99},
        {"source_timestamp_ms": 1_000_100, "price": 100},
        {"source_timestamp_ms": 1_000_200, "price": 101},
    ]}}}
    anchors = ev_shadow.capture_opening_prices(markets, venue_state, {}, now_ms=1_001_000)
    assert anchors["m1"]["price"] == 100
    assert anchors["m1"]["source_timestamp_ms"] == 1_000_100


def test_missed_chainlink_start_anchor_fails_closed():
    row = market()
    row.update(open_price=None, start_ts=900)
    rows = evaluate_market_event(event(), row, venue(), now=1000.0, opening_prices={})
    assert all(item["reason"] == "price_to_beat_capture_missed" for item in rows)
