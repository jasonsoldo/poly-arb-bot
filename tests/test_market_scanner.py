from poly_arb_bot.market_scanner import MarketScanner
from poly_arb_bot.cli import is_crypto_market
from poly_arb_bot.cli import current_series_events
from poly_arb_bot.cli import tradable_markets
from types import SimpleNamespace
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
    assert spec.fee_rate is None


def test_scanner_reads_market_fee_schedule():
    market = {
        "conditionId": "0xcondition",
        "question": "Bitcoin Up or Down - July 11, 9:25PM-9:30PM ET",
        "outcomes": ["Up", "Down"],
        "clobTokenIds": ["111", "222"],
        "endDate": "2026-07-11T13:30:00Z",
        "feeSchedule": {"rate": "0.05"},
    }
    assert MarketScanner().spec_from_market(market).fee_rate == 0.05


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


def test_scanner_keeps_orderbook_market_without_directional_open_price():
    market = {
        "conditionId": "0xcondition",
        "question": "Bitcoin Up or Down - July 11, 9:25PM-9:30PM ET",
        "outcomes": ["Up", "Down"],
        "clobTokenIds": ["111", "222"],
        "endDate": "2026-07-11T13:30:00Z",
    }

    assert MarketScanner().spec_from_market(market).open_price is None


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


def test_crypto_discovery_uses_market_identity_not_description_keywords():
    assert is_crypto_market({"question": "Bitcoin all time high by September 30, 2026?", "slug": "bitcoin-ath"})
    assert not is_crypto_market({"question": "Will Seth Moulton win?", "slug": "seth-moulton", "description": "Bitcoin policy"})


def test_current_series_events_requires_active_current_event():
    now = 1783857600
    events = [
        {"id": "live", "endDate": "2026-07-12T12:05:00Z", "active": True, "closed": False},
        {"id": "old", "endDate": "2026-07-12T11:00:00Z", "active": True, "closed": True},
    ]
    assert [event["id"] for event in current_series_events(events, now)] == ["live"]


def test_current_series_events_limits_each_series_to_current_and_next():
    now = 1783857600
    events = [
        {"id": "third", "endDate": "2026-07-12T12:15:00Z", "active": True, "closed": False},
        {"id": "current", "endDate": "2026-07-12T12:05:00Z", "active": True, "closed": False},
        {"id": "next", "endDate": "2026-07-12T12:10:00Z", "active": True, "closed": False},
    ]
    assert [event["id"] for event in current_series_events(events, now, limit=2)] == ["current", "next"]


def test_tradable_markets_requires_orderbook_and_accepting_orders():
    markets = [
        {"conditionId": "live", "enableOrderBook": True, "acceptingOrders": True},
        {"conditionId": "stopped", "enableOrderBook": True, "acceptingOrders": False},
    ]
    assert [market["conditionId"] for market in tradable_markets(markets)] == ["live"]


def test_scanner_generates_official_recurring_series_slugs():
    slugs = MarketScanner().updown_series_slugs(["5m", "15m"])
    assert slugs == ["btc-up-or-down-5m", "btc-up-or-down-15m"]
