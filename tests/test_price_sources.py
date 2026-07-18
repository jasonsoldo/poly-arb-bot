from poly_arb_bot.binance_source import BinanceSource
from poly_arb_bot.chainlink_source import ChainlinkSource
from poly_arb_bot.clob_client import PolymarketClobClient
from poly_arb_bot.http_utils import HttpResponse
from poly_arb_bot.live_signals import probability_from_distance


class FakeHttp:
    def __init__(self):
        self.calls = []

    def get_json(self, base_url, path, params=None):
        self.calls.append(("GET", base_url, path, params))
        if path == "/api/v3/ticker/price":
            return HttpResponse({"symbol": "BTCUSDT", "price": "61000.5"}, 3, "url")
        if path == "/api/v3/ticker/bookTicker":
            return HttpResponse(
                {"bidPrice": "61000.4", "bidQty": "2.5", "askPrice": "61000.6", "askQty": "1.5"},
                4,
                "url",
            )
        if path == "/book":
            return HttpResponse(
                {
                    "bids": [{"price": "0.40", "size": "10"}],
                    "asks": [{"price": "0.42", "size": "5"}, {"price": "0.44", "size": "20"}],
                    "min_order_size": "5",
                    "tick_size": "0.01",
                    "fee_schedule": {"rate": "0.07", "exponent": "1", "taker_only": True},
                },
                5,
                "url",
            )
        if path.startswith("/clob-markets/"):
            return HttpResponse({"t": [{"t": "123", "o": "Yes"}], "mos": 5, "mts": 0.01, "tbf": 7}, 5, "url")
        if path == "/sampling-markets":
            return HttpResponse({"data": [{"condition_id": "0x1"}], "next_cursor": None}, 5, "url")
        raise AssertionError(path)

    def post_json(self, base_url, path, payload):
        self.calls.append(("POST", base_url, path, payload))
        selector = payload["params"][0]["data"]
        if selector == "0x313ce567":
            return HttpResponse({"result": "0x" + "0" * 63 + "8"}, 2, "url")
        answer = hex(6100050000000)[2:].rjust(64, "0")
        updated_at = hex(1783406500)[2:].rjust(64, "0")
        words = ["0" * 64, answer, "0" * 64, updated_at, "0" * 64]
        return HttpResponse({"result": "0x" + "".join(words)}, 2, "url")


def test_binance_source_uses_official_market_endpoints():
    ticker = BinanceSource(http=FakeHttp()).ticker("BTCUSDT")

    assert ticker.price == 61000.5
    assert ticker.bid_price == 61000.4
    assert ticker.ask_price == 61000.6
    assert ticker.latency_ms == 7


def test_clob_book_expected_buy_price_from_asks():
    book = PolymarketClobClient(http=FakeHttp()).get_book("token")

    assert book.best_bid == 0.40
    assert book.best_ask == 0.42
    assert round(book.expected_buy_price(10), 3) == 0.43
    assert book.ask_liquidity(0.43) == 5
    assert book.min_order_size == 5
    assert book.tick_size == 0.01
    assert book.fee_rate == 0.07
    assert book.fee_exponent == 1
    assert book.fee_taker_only is True


def test_clob_market_info_uses_v2_endpoint():
    http = FakeHttp()
    info = PolymarketClobClient(http=http).get_market_info("0xcondition")
    assert info["mos"] == 5
    assert http.calls[-1] == ("GET", "https://clob.polymarket.com", "/clob-markets/0xcondition", None)


def test_clob_sampling_markets_uses_v2_endpoint():
    http = FakeHttp()
    assert PolymarketClobClient(http=http).sampling_markets() == [{"condition_id": "0x1"}]
    assert http.calls[-1] == ("GET", "https://clob.polymarket.com", "/sampling-markets", None)


def test_clob_sampling_stops_on_clob_terminal_cursor():
    class TerminalCursorHttp:
        def __init__(self):
            self.calls = []

        def get_json(self, base_url, path, params=None):
            self.calls.append(params)
            return HttpResponse({"data": [{"condition_id": "0x1"}], "next_cursor": "LTE="}, 1, "url")

    http = TerminalCursorHttp()
    assert PolymarketClobClient(http=http).sampling_markets() == [{"condition_id": "0x1"}]
    assert http.calls == [None]


def test_chainlink_latest_round_data_decode():
    price = ChainlinkSource("https://polygon-rpc.example", http=FakeHttp(), max_staleness_seconds=999999999).latest_price("0xfeed")

    assert price.price == 61000.5
    assert price.decimals == 8
    assert not price.stale


def test_probability_from_distance_is_monotonic():
    assert probability_from_distance(10, 30) > probability_from_distance(-10, 30)
