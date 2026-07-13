from pathlib import Path


SOURCE = Path("cpp/reference_price_engine/reference_price_engine.cpp").read_text(encoding="utf-8")


def test_reference_engine_subscribes_to_official_binance_and_chainlink_topics():
    assert '"crypto_prices"' in SOURCE
    assert '"crypto_prices_chainlink"' in SOURCE
    assert '"btcusdt"' in SOURCE
    assert 'btc/usd' in SOURCE


def test_reference_engine_writes_atomic_status_and_reconnects():
    assert 'path + ".tmp"' in SOURCE
    assert "std::filesystem::rename" in SOURCE
    assert "divergence_bps" in SOURCE
    assert "engine_latency_us" in SOURCE
    assert "REFERENCE_ERROR" in SOURCE
