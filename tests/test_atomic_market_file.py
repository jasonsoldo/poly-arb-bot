import json

from poly_arb_bot.cli import scan_updown_markets, write_market_payload_atomic


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
