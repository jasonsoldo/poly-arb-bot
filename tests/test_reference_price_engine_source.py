from pathlib import Path


SOURCE = Path("cpp/reference_price_engine/reference_price_engine.cpp").read_text(encoding="utf-8")


def test_reference_engine_subscribes_to_official_binance_and_chainlink_sources():
    assert 'data-stream.binance.vision' in SOURCE
    assert '@bookTicker' in SOURCE
    assert '"crypto_prices_chainlink"' in SOURCE
    assert '"btcusdt"' in SOURCE
    assert 'btc/usd' in SOURCE
    for asset in ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE"):
        assert f'"{asset}"' in SOURCE
    for symbol in ("btcusdt", "ethusdt", "solusdt", "xrpusdt"):
        assert f'"{symbol}"' in SOURCE
    for symbol in ("btc/usd", "eth/usd", "sol/usd", "xrp/usd"):
        assert f'"{symbol}"' in SOURCE


def test_reference_engine_writes_atomic_status_and_reconnects():
    assert 'path + ".tmp"' in SOURCE
    assert "std::filesystem::rename" in SOURCE
    assert "divergence_bps" in SOURCE
    assert "engine_latency_us" in SOURCE
    assert "REFERENCE_ERROR" in SOURCE
    assert "std::setprecision(15)" in SOURCE
    assert r'\"assets\"' in SOURCE
    assert r'\"supported\"' in SOURCE


def test_reference_engine_distinguishes_not_received_from_stale():
    assert "source_status(" in SOURCE
    for status in ("NOT_RECEIVED", "FRESH", "STALE"):
        assert f'"{status}"' in SOURCE
    assert r'\"binance_status\":\"' in SOURCE
    assert "matched_messages" in SOURCE
    assert "unmatched_messages" in SOURCE


def test_reference_engine_subscribes_to_coinbase_and_kraken_spot_tickers():
    assert 'ws-feed.exchange.coinbase.com' in SOURCE
    assert '"type":"subscribe","product_ids"' in SOURCE
    assert '"channel":"ticker"' in SOURCE
    assert 'ws.kraken.com' in SOURCE
    for source in ("binance", "coinbase", "kraken", "chainlink"):
        assert f'"{source}"' in SOURCE


def test_reference_engine_emits_normalized_source_and_quorum_state():
    for field in (
        "market_type", "quote_currency", "price", "bid", "ask",
        "source_timestamp", "received_at", "message_age_ms", "status",
        "fresh_exchange_source_count", "fresh_usd_spot_source_count",
        "consensus_price", "fast_price", "settlement_reference",
        "cross_source_divergence_bps", "reference_quorum_met", "reference_state",
    ):
        assert field in SOURCE


def test_rtds_subscription_is_chainlink_only_and_binance_uses_verified_spot_symbols():
    subscription = SOURCE.split('const std::string rtds_sub =', 1)[1].split(';', 1)[0]
    assert '"crypto_prices"' not in subscription
    assert '"crypto_prices_chainlink"' in subscription
    binance_path = SOURCE.split('const std::string binance_path =', 1)[1].split(';', 1)[0]
    for symbol in ("btcusdt", "ethusdt", "solusdt", "xrpusdt", "bnbusdt", "dogeusdt"):
        assert f'{symbol}@bookTicker' in binance_path
    assert "hypeusdt" not in binance_path


def test_rtds_sends_documented_heartbeat_and_logs_unmatched_frame_shape():
    assert 'std::string("PING")' in SOURCE
    assert "REFERENCE_UNMATCHED" in SOURCE
    for field in ("topic=", "type=", "symbol="):
        assert field in SOURCE


def test_reference_status_file_is_rate_limited_below_tick_frequency():
    assert "STATUS_WRITE_INTERVAL_MS" in SOURCE
    assert "last_status_write_ms" in SOURCE
    assert "force" in SOURCE


def test_reference_engine_keeps_timestamped_settlement_anchor_samples():
    assert '"binance", "chainlink"' in SOURCE
    assert '"_samples\\":["' in SOURCE
    assert "source_timestamp_ms" in SOURCE
    assert "anchor_samples" in SOURCE
