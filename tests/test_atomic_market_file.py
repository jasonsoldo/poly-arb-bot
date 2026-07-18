import json
from dataclasses import replace

from poly_arb_bot.cli import (
    enrich_open_prices,
    market_refresh_delay,
    merge_validated_market_metadata,
    restore_cached_open_prices,
    scan_updown_markets,
    write_market_payload_atomic,
)
from poly_arb_bot.live_signals import LiveMarketSpec


def test_market_payload_is_versioned_and_leaves_no_temporary_file(tmp_path):
    target = tmp_path / "live_markets.json"
    write_market_payload_atomic(target, {"markets": [{"market_id": "m1"}]}, 1783904400)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["version"] == 1783904400000
    assert payload["generated_at"] == 1783904400
    assert payload["markets"] == [{"market_id": "m1"}]
    assert not (tmp_path / "live_markets.json.tmp").exists()


def test_zero_market_scan_keeps_previous_verified_file(tmp_path, monkeypatch):
    target = tmp_path / "live_markets.json"
    target.write_text('{"version":1,"generated_at":1,"markets":[{"market_id":"verified"}]}', encoding="utf-8")
    monkeypatch.setattr("poly_arb_bot.cli.PolymarketDataClient.series_by_slugs", lambda *args: [])
    result = scan_updown_markets(target, "https://example.invalid", "5m", "current,next", base_ts=1783904400)
    assert result == 3
    assert json.loads(target.read_text(encoding="utf-8"))["markets"][0]["market_id"] == "verified"


def test_scan_keeps_previous_file_when_batch_series_request_fails(tmp_path, monkeypatch):
    target = tmp_path / "live_markets.json"
    target.write_text('{"markets":[{"market_id":"verified"}]}', encoding="utf-8")
    monkeypatch.setattr(
        "poly_arb_bot.cli.PolymarketDataClient.series_by_slugs",
        lambda *args: (_ for _ in ()).throw(TimeoutError("slow series")),
    )
    result = scan_updown_markets(target, "https://example.invalid", "5m", "current,next", base_ts=1783904400)
    assert result == 3
    assert json.loads(target.read_text(encoding="utf-8"))["markets"][0]["market_id"] == "verified"


def test_scan_keeps_previous_file_when_one_gamma_event_batch_fails(tmp_path, monkeypatch):
    target = tmp_path / "live_markets.json"
    target.write_text('{"markets":[{"market_id":"verified"}]}', encoding="utf-8")
    monkeypatch.setattr(
        "poly_arb_bot.cli.PolymarketDataClient.series_by_slugs",
        lambda *args: [{"id": "10114", "slug": "btc-up-or-down-5m"}],
    )
    monkeypatch.setattr(
        "poly_arb_bot.cli.PolymarketDataClient.events_by_slugs",
        lambda *args: (_ for _ in ()).throw(TimeoutError("one failed event batch")),
    )

    result = scan_updown_markets(
        target, "https://example.invalid", "5m", "current,next", base_ts=1783904400,
    )

    assert result == 3
    assert json.loads(target.read_text(encoding="utf-8"))["markets"][0]["market_id"] == "verified"


def test_hourly_scan_queries_events_by_series_window(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "poly_arb_bot.cli.PolymarketDataClient.series_by_slugs",
        lambda *args: [{"id": "10114", "slug": "btc-up-or-down-hourly"}],
    )
    monkeypatch.setattr(
        "poly_arb_bot.cli.PolymarketDataClient.events_by_series_window",
        lambda _self, series_id, start, end: calls.append((series_id, start, end)) or [],
    )
    monkeypatch.setattr(
        "poly_arb_bot.cli.PolymarketDataClient.events_by_slugs",
        lambda *args: (_ for _ in ()).throw(AssertionError("hourly must not synthesize event slugs")),
    )
    result = scan_updown_markets(
        tmp_path / "hourly.json", "https://example.invalid", "1h", "current,next",
        base_ts=1783904400,
    )
    assert result == 3
    assert calls == [("10114", 1783900800, 1783915200)]


def _spec(**changes):
    row = LiveMarketSpec(
        market_id="m1", title="Bitcoin Up or Down", asset="BTC", symbol="BTCUSDT",
        open_price=None, close_ts=1784099700, up_token_id="111", down_token_id="222",
        start_ts=1784099400, settlement_source="chainlink", interval="5m",
    )
    return replace(row, **changes)


def test_open_price_enrichment_uses_official_price_and_skips_next_market():
    calls = []

    class Client:
        def crypto_price(self, asset, start_ts, interval, close_ts):
            calls.append((asset, start_ts, interval, close_ts))
            return {"openPrice": 64765.026}

    rows, stats = enrich_open_prices(
        [_spec(), _spec(market_id="m2", start_ts=1784099700, close_ts=1784100000)],
        Client(), now_ts=1784099450, workers=2,
    )

    assert calls == [("BTC", 1784099400, "5m", 1784099700)]
    assert rows[0].open_price == 64765.026
    assert rows[0].open_price_source == "polymarket_crypto_price_api"
    assert rows[0].open_price_capture_mode == "official_open_price_api"
    assert rows[0].open_price_source_timestamp_ms == 1784099400000
    assert rows[1].open_price is None
    assert stats == {"requested": 1, "enriched": 1, "unavailable": 0, "errors": 0}


def test_cached_official_open_price_survives_transient_endpoint_failure(tmp_path):
    path = tmp_path / "live_markets.json"
    cached = _spec(
        open_price=64765.026,
        open_price_source="polymarket_crypto_price_api",
        open_price_capture_mode="official_open_price_api",
        open_price_source_timestamp_ms=1784099400000,
    )
    path.write_text(json.dumps({"markets": [cached.__dict__]}), encoding="utf-8")

    restored = restore_cached_open_prices([_spec()], path)

    assert restored[0].open_price == 64765.026
    assert restored[0].open_price_source == "polymarket_crypto_price_api"


def test_enrichment_merge_preserves_validated_clob_sizing_metadata():
    validated = _spec(
        fee_rate=0.07,
        min_order_size=5,
        tick_size=0.01,
        fee_exponent=1,
        fee_taker_only=True,
    )
    enriched = _spec(
        open_price=64765.026,
        open_price_source="polymarket_crypto_price_api",
        open_price_capture_mode="official_open_price_api",
        open_price_source_timestamp_ms=1784099400000,
    )

    merged = merge_validated_market_metadata([validated], [enriched])

    assert merged == [replace(
        validated,
        open_price=64765.026,
        open_price_source="polymarket_crypto_price_api",
        open_price_capture_mode="official_open_price_api",
        open_price_source_timestamp_ms=1784099400000,
    )]


def test_market_refresh_delay_scans_immediately_after_next_market_starts(tmp_path):
    path = tmp_path / "live_markets.json"
    path.write_text(json.dumps({"markets": [{
        "market_id": "current",
        "start_ts": 1784101200,
        "close_ts": 1784101500,
        "open_price": 64500,
    }, {
        "market_id": "next",
        "start_ts": 1784101500,
        "close_ts": 1784101800,
        "open_price": None,
    }]}), encoding="utf-8")

    delay = market_refresh_delay(path, 60, now=1784101470, boundary_grace_seconds=1)

    assert delay == 31


def test_market_refresh_delay_retries_missing_current_open_price(tmp_path):
    path = tmp_path / "live_markets.json"
    path.write_text(json.dumps({"markets": [{
        "market_id": "current",
        "start_ts": 1784101500,
        "close_ts": 1784101800,
        "open_price": None,
    }]}), encoding="utf-8")

    assert market_refresh_delay(path, 60, now=1784101512) == 5
