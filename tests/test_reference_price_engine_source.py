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


def test_reference_engine_subscribes_to_official_bybit_and_okx_spot_tickers():
    assert 'stream.bybit.com' in SOURCE
    assert '/v5/public/spot' in SOURCE
    assert '"tickers.BTCUSDT"' in SOURCE
    assert 'get<double>("bid1Price"' in SOURCE
    assert 'get<double>("ask1Price"' in SOURCE
    assert 'get<double>("lastPrice"' in SOURCE
    assert 'ws.okx.com' in SOURCE
    assert '"8443"' in SOURCE
    assert '/ws/v5/public' in SOURCE
    assert '"instId":"BTC-USDT"' in SOURCE
    assert '"bidPx"' in SOURCE and '"askPx"' in SOURCE
    assert '"BNB", "bnbusdt", "bnb/usd", "BNB-USD", "BNB/USD", "BNBUSDT", "BNB-USDT"' in SOURCE
    assert '"DOGE", "dogeusdt", "doge/usd", "DOGE-USD", "DOGE/USD", "DOGEUSDT", "DOGE-USDT"' in SOURCE
    assert '"HYPE", "", "hype/usd", "HYPE-USD", "HYPE/USD", "", "HYPE-USDT"' in SOURCE
    for symbol in ("bnb/usd", "doge/usd", "hype/usd", "BNB-USD", "HYPE-USD", "BNB/USD", "HYPE/USD", "HYPE-USDT"):
        assert symbol in SOURCE

def test_reference_engine_emits_normalized_source_and_quorum_state():
    for field in (
        "market_type", "quote_currency", "price", "bid", "ask",
        "source_timestamp", "received_at", "message_age_ms", "status",
        "fresh_exchange_source_count", "fresh_usd_spot_source_count",
        "consensus_price", "fast_price", "settlement_reference",
        "cross_source_divergence_bps", "reference_quorum_met", "reference_state",
        "clock_skew_ms", "clock_skew_basis",
    ):
        assert field in SOURCE
    assert '"OUTLIER"' in SOURCE
    assert "quote_medians" in SOURCE


def test_chainlink_is_extracted_as_settlement_reference_not_spot_consensus():
    settlement_branch = SOURCE.split('if (item.first == "chainlink")', 1)[1].split("continue;", 1)[0]
    assert "settlement_reference = source.price" in settlement_branch


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


def test_reference_model_samples_are_bucketed_by_second_and_publish_coverage():
    assert "MODEL_SAMPLE_BUCKET_MS = 1000" in SOURCE
    assert "same_model_bucket" in SOURCE
    assert "row.samples.back().second = price" in SOURCE
    assert "model_sample_span_seconds" in SOURCE


def test_reference_engine_keeps_timestamped_settlement_anchor_samples():
    assert '"binance", "chainlink"' in SOURCE
    assert '"_samples\\":["' in SOURCE
    assert "source_timestamp_ms" in SOURCE
    assert "anchor_samples" in SOURCE


def test_binance_kline_stream_emits_hourly_and_four_hour_close_samples():
    assert "@kline_1h" in SOURCE
    assert "@kline_4h" in SOURCE
    assert "settlement_samples" in SOURCE
    assert 'get<bool>("x"' in SOURCE
    assert 'get<double>("c"' in SOURCE
    assert r'\"timeframe\"' in SOURCE

def test_reference_engine_uses_source_specific_freshness_limits():
    assert "DEFAULT_REFERENCE_FRESHNESS_MS = 3000" in SOURCE
    assert "COINBASE_REFERENCE_FRESHNESS_MS = 10000" in SOURCE
    assert "source_freshness_limit_ms(const std::string& source_name)" in SOURCE
    assert 'source_name == "coinbase"' in SOURCE
    assert "source_status(" in SOURCE
    assert "const std::string& source_name" in SOURCE
    assert "source_status(item.first, source, timestamp)" in SOURCE
    assert 'source_status("binance", binance, timestamp)' in SOURCE
    assert 'source_status("chainlink", chainlink, timestamp)' in SOURCE

