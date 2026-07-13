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
