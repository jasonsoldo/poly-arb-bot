import poly_arb_bot.reference_layer as reference_layer
from poly_arb_bot.reference_layer import ReferenceQuote, aggregate_reference


def quote(source, price, quote="USD", status="FRESH", market_type="spot"):
    return ReferenceQuote(source, "BTC", f"BTC-{quote}", market_type, quote, price, price - 1,
                          price + 1, 1000, 1005, 5, status)


def test_consensus_uses_fresh_spot_median_and_quorum():
    state = aggregate_reference([
        quote("binance", 101, "USDT"), quote("coinbase", 100), quote("kraken", 99),
    ], settlement_reference=100, settlement_verified=True, max_divergence_bps=300)
    assert state.consensus_price == 99.5
    assert state.fast_price == 101
    assert state.fresh_exchange_source_count == 3
    assert state.fresh_usd_spot_source_count == 2
    assert state.reference_quorum_met
    assert state.reference_state == "REFERENCE_READY"


def test_reference_rejects_single_exchange_and_marks_outlier():
    state = aggregate_reference([
        quote("coinbase", 100), quote("kraken", 140), quote("binance", 100.1, "USDT", "STALE"),
    ], settlement_reference=100, settlement_verified=True, max_divergence_bps=100)
    assert state.reference_quorum_met is False
    assert state.reference_block_reason == "insufficient_reference_sources"
    assert any(row.status == "OUTLIER" for row in state.sources)


def test_missing_price_is_not_stale():
    row = ReferenceQuote("binance", "BTC", "BTCUSDT", "spot", "USDT", None, None, None,
                         None, None, None, "NOT_RECEIVED")
    state = aggregate_reference([row], settlement_reference=None, settlement_verified=False)
    assert state.sources[0].status == "NOT_RECEIVED"
    assert state.reference_state == "REFERENCE_BLOCKED"

def test_reference_reports_missing_required_usd_spot_source():
    state = aggregate_reference([
        quote("binance", 100, "USDT"), quote("bybit", 100.01, "USDT"),
    ], settlement_reference=100, settlement_verified=True, max_divergence_bps=100)
    assert state.reference_quorum_met is False
    assert state.reference_block_reason == "required_usd_spot_source_unavailable"


def test_market_settlement_source_changes_reference_readiness():
    assert hasattr(reference_layer, "reference_state_for_asset")
    asset = {"sources": {
        "binance": {
            "symbol": "bnbusdt", "market_type": "spot", "quote_currency": "USDT",
            "price": 582.0, "message_age_ms": 100, "status": "FRESH",
        },
        "coinbase": {
            "symbol": "BNB-USD", "market_type": "spot", "quote_currency": "USD",
            "price": 581.9, "message_age_ms": 100, "status": "FRESH",
        },
        "chainlink": {
            "symbol": "bnb/usd", "market_type": "oracle", "quote_currency": "USD",
            "price": 581.8, "message_age_ms": 50_000, "status": "FRESH",
        },
    }}

    hourly = reference_layer.reference_state_for_asset(asset, "binance", 3_000)
    short_window = reference_layer.reference_state_for_asset(asset, "chainlink", 3_000)

    assert hourly.reference_state == "REFERENCE_READY"
    assert hourly.settlement_reference == 582.0
    assert short_window.reference_state == "REFERENCE_BLOCKED"
    assert short_window.reference_block_reason == "settlement_reference_unavailable"


def test_coinbase_uses_its_source_specific_freshness_limit(monkeypatch):
    monkeypatch.setenv("COINBASE_REFERENCE_MAX_AGE_MS", "10000")
    asset = {"sources": {
        "binance": {
            "market_type": "spot", "quote_currency": "USDT", "price": 582.0,
            "message_age_ms": 100, "status": "FRESH",
        },
        "coinbase": {
            "market_type": "spot", "quote_currency": "USD", "price": 581.9,
            "message_age_ms": 8_000, "status": "FRESH",
        },
        "chainlink": {
            "market_type": "oracle", "quote_currency": "USD", "price": 581.8,
            "message_age_ms": 100, "status": "FRESH",
        },
    }}

    state = reference_layer.reference_state_for_asset(asset, "chainlink", 3_000)

    assert state.reference_state == "REFERENCE_READY"
    assert state.fresh_usd_spot_source_count == 1
