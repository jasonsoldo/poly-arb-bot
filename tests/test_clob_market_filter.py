from poly_arb_bot.cli import filter_specs_with_orderbooks
from poly_arb_bot.clob_client import ClobBook, ClobLevel
from poly_arb_bot.live_signals import LiveMarketSpec


class FakeClob:
    def __init__(self, books):
        self.books = books

    def get_book(self, token_id):
        return self.books[token_id]

    def get_market_info(self, market_id):
        return {
            "t": [{"t": "up", "o": "Up"}, {"t": "down", "o": "Down"}],
            "mos": 5,
            "mts": 0.01,
            "fd": {"r": 0.07, "e": 1, "to": True},
        }


def book(token_id, asks):
    return ClobBook(
        token_id, [], [ClobLevel(0.4, asks)] if asks else [], 1, 1,
        min_order_size=5, tick_size=0.01,
    )


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
    assert valid[0].fee_rate == 0.07
    assert valid[0].min_order_size == 5
    assert valid[0].tick_size == 0.01
    assert valid[0].fee_exponent == 1
    assert valid[0].fee_taker_only is True


def test_filter_rejects_market_without_official_fee_schedule():
    class NoFee(FakeClob):
        def get_market_info(self, market_id):
            return {"t": [{"t": "up", "o": "Up"}, {"t": "down", "o": "Down"}]}
    diagnostics = {}
    valid, rejected = filter_specs_with_orderbooks([spec()], NoFee({"up": book("up", 10), "down": book("down", 10)}), diagnostics)
    assert not valid
    assert rejected == 1
    assert diagnostics == {"fee_schedule_unavailable": 1}


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
            return {
                "t": [{"t": "up", "o": "Up"}, {"t": "down", "o": "Down"}],
                "mos": 5, "mts": 0.01, "fd": {"r": 0.07},
            }

        def get_book(self, token_id):
            raise RuntimeError("HTTP GET 404 failed for https://clob.polymarket.com/book body={\"error\":\"No orderbook exists\"}")

    specs = [spec()]
    diagnostics = {}
    valid, rejected = filter_specs_with_orderbooks(specs, MissingBook(), diagnostics)
    assert not valid
    assert rejected == 1
    assert diagnostics == {"no_orderbook": 1}


def test_invalid_token_is_not_reported_as_missing_orderbook():
    class InvalidToken:
        def get_market_info(self, market_id):
            return {
                "t": [{"t": "up", "o": "Up"}, {"t": "down", "o": "Down"}],
                "mos": 5, "mts": 0.01, "fd": {"r": 0.07},
            }

        def get_book(self, token_id):
            raise RuntimeError("CLOB book rejected token up: Invalid token id")

    diagnostics = {}
    valid, rejected = filter_specs_with_orderbooks([spec()], InvalidToken(), diagnostics)
    assert not valid
    assert rejected == 1
    assert diagnostics == {"invalid_token": 1}


def test_filter_uses_one_batch_book_request_when_available():
    class BatchClob:
        def __init__(self):
            self.calls = []

        def get_books(self, token_ids):
            self.calls.append(token_ids)
            return {
                "gamma-up": ClobBook(
                    "gamma-up", [], [ClobLevel(0.4, 10)], 1, 1, 0.07,
                    min_order_size=5, tick_size=0.01, fee_exponent=1, fee_taker_only=True,
                ),
                "gamma-down": ClobBook(
                    "gamma-down", [], [ClobLevel(0.5, 10)], 1, 1, 0.07,
                    min_order_size=6, tick_size=0.001, fee_exponent=1, fee_taker_only=True,
                ),
            }

    clob = BatchClob()
    valid, rejected = filter_specs_with_orderbooks([spec()], clob)
    assert rejected == 0
    assert valid[0].fee_rate == 0.07
    assert valid[0].min_order_size == 6
    assert valid[0].tick_size == 0.01
    assert valid[0].fee_exponent == 1
    assert valid[0].fee_taker_only is True
    assert clob.calls == [["gamma-up", "gamma-down"]]


def test_batch_filter_uses_official_market_info_for_sizing_and_fee_schedule():
    class BatchClob:
        def __init__(self):
            self.market_info_calls = []

        def get_books(self, token_ids):
            return {
                token_id: ClobBook(
                    token_id, [], [ClobLevel(0.4, 10)], 1, 1, 0.07,
                    min_order_size=5, tick_size=0.01,
                )
                for token_id in token_ids
            }

        def get_market_info(self, market_id):
            self.market_info_calls.append(market_id)
            return {
                "t": [
                    {"t": "gamma-up", "o": "Up"},
                    {"t": "gamma-down", "o": "Down"},
                ],
                "mos": 7,
                "mts": 0.001,
                "fd": {"r": 0.07, "e": 1, "to": True},
            }

    clob = BatchClob()
    valid, rejected = filter_specs_with_orderbooks([spec()], clob)

    assert rejected == 0
    assert clob.market_info_calls == ["m1"]
    assert valid[0].min_order_size == 7
    assert valid[0].tick_size == 0.001
    assert valid[0].fee_rate == 0.07
    assert valid[0].fee_exponent == 1
    assert valid[0].fee_taker_only is True


def test_filter_rejects_batch_market_without_minimum_order_metadata():
    class BatchClob:
        def get_books(self, token_ids):
            return {
                "gamma-up": ClobBook("gamma-up", [], [ClobLevel(0.4, 10)], 1, 1, 0.07),
                "gamma-down": ClobBook("gamma-down", [], [ClobLevel(0.5, 10)], 1, 1, 0.07),
            }

    diagnostics = {}
    valid, rejected = filter_specs_with_orderbooks([spec()], BatchClob(), diagnostics)
    assert valid == []
    assert rejected == 1
    assert diagnostics == {"market_size_metadata_unavailable": 1}
