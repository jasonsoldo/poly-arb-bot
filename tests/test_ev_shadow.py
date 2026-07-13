from poly_arb_bot.ev_shadow import evaluate_market_event


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
    assert all(row["reason"] == "probability_model_unavailable" for row in rows)
