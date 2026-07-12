from poly_arb_bot.market_scanner import MarketScanner
from poly_arb_bot.polymarket_data import parse_timestamp_seconds


def test_parse_timestamp_seconds_iso_utc():
    assert parse_timestamp_seconds("2026-07-11T13:30:00Z") == 1783776600


def test_scanner_builds_live_market_spec_from_gamma_market():
    market = {
        "conditionId": "0xcondition",
        "question": "Bitcoin Up or Down - July 11, 9:25PM-9:30PM ET",
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": '["111", "222"]',
        "priceToBeat": "118234.50",
        "endDate": "2026-07-11T13:30:00Z",
    }

    spec = MarketScanner().spec_from_market(market)

    assert spec is not None
    assert spec.market_id == "0xcondition"
    assert spec.open_price == 118234.50
    assert spec.close_ts == 1783776600
    assert spec.up_token_id == "111"
    assert spec.down_token_id == "222"
    assert spec.symbol == "BTCUSDT"


def test_scanner_reads_open_price_from_event_metadata():
    event = {"eventMetadata": {"priceToBeat": 63048.3179}}
    market = {
        "conditionId": "0xcondition",
        "question": "Bitcoin Up or Down - July 11, 9:25PM-9:30PM ET",
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": '["111", "222"]',
        "endDate": "2026-07-11T13:30:00Z",
    }

    spec = MarketScanner().spec_from_market(market, event)

    assert spec.open_price == 63048.3179


def test_scanner_generates_updown_slugs_for_interval_boundaries():
    slugs = MarketScanner().updown_slugs(["5m"], now_ts=1783405520, include_previous=False, include_next=False)

    assert "btc-updown-5m-1783405500" in slugs
    assert "eth-updown-5m-1783405500" in slugs


def test_scanner_skips_market_without_open_price():
    market = {
        "conditionId": "0xcondition",
        "question": "Bitcoin Up or Down - July 11, 9:25PM-9:30PM ET",
        "outcomes": ["Up", "Down"],
        "clobTokenIds": ["111", "222"],
        "endDate": "2026-07-11T13:30:00Z",
    }

    assert MarketScanner().spec_from_market(market) is None


def test_scanner_falls_back_to_tokens_objects_when_clob_ids_are_empty():
    market = {
        "conditionId": "0xcondition",
        "question": "Bitcoin Up or Down - July 11, 9:25PM-9:30PM ET",
        "outcomes": '["UP", "DOWN"]',
        "clobTokenIds": "[]",
        "tokens": [{"token_id": "111"}, {"token_id": "222"}],
        "priceToBeat": "118234.50",
        "endDate": "2026-07-11T13:30:00Z",
    }
    spec = MarketScanner().spec_from_market(market)
    assert spec.up_token_id == "111"
    assert spec.down_token_id == "222"


def test_scanner_reads_open_price_from_rules_text():
    market = {
        "conditionId": "0xcondition",
        "question": "Ethereum Up or Down - July 11, 9:25PM-9:30PM ET",
        "outcomes": ["Up", "Down"],
        "clobTokenIds": ["333", "444"],
        "rules": "The price to beat is $3,456.78.",
        "endDate": "2026-07-11T13:30:00Z",
    }

    spec = MarketScanner().spec_from_market(market)

    assert spec.open_price == 3456.78
    assert spec.symbol == "ETHUSDT"
