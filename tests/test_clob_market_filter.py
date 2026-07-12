from poly_arb_bot.cli import filter_specs_with_orderbooks
from poly_arb_bot.clob_client import ClobBook, ClobLevel
from poly_arb_bot.live_signals import LiveMarketSpec


class FakeClob:
    def __init__(self, books):
        self.books = books

    def get_book(self, token_id):
        return self.books[token_id]

    def get_market_info(self, market_id):
        return {"t": [{"t": "up", "o": "Up"}, {"t": "down", "o": "Down"}]}


def book(token_id, asks):
    return ClobBook(token_id, [], [ClobLevel(0.4, asks)] if asks else [], 1, 1)


def spec():
    return LiveMarketSpec("m1", "Bitcoin Up or Down", "Bitcoin", "BTCUSDT", 1.0, 1, "gamma-up", "gamma-down")


def test_filter_requires_both_up_and_down_asks():
    specs = [spec()]
    valid, rejected = filter_specs_with_orderbooks(specs, FakeClob({"up": book("up", 10), "down": book("down", 0)}))
    assert valid == []
    assert rejected == 1


def test_filter_keeps_market_with_both_orderbooks():
    specs = [spec()]
    valid, rejected = filter_specs_with_orderbooks(specs, FakeClob({"up": book("up", 10), "down": book("down", 10)}))
    assert [item.market_id for item in valid] == ["m1"]
    assert rejected == 0


def test_filter_reports_empty_asks_and_requires_buyable_depth():
    specs = [spec()]
    diagnostics = {}
    valid, rejected = filter_specs_with_orderbooks(
        specs,
        FakeClob({"up": book("up", 10), "down": book("down", 0)}),
        diagnostics,
    )
    assert not valid
    assert rejected == 1
    assert diagnostics == {"empty_asks": 1}


def test_http_404_is_a_missing_orderbook(monkeypatch):
    class MissingBook:
        def get_market_info(self, market_id):
            return {"t": [{"t": "up", "o": "Up"}, {"t": "down", "o": "Down"}]}

        def get_book(self, token_id):
            raise RuntimeError("HTTP GET 404 failed for https://clob.polymarket.com/book body={\"error\":\"No orderbook exists\"}")

    specs = [spec()]
    diagnostics = {}
    valid, rejected = filter_specs_with_orderbooks(specs, MissingBook(), diagnostics)
    assert not valid
    assert rejected == 1
    assert diagnostics == {"no_orderbook": 1}
