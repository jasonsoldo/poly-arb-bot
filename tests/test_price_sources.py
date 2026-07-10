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
                },
                5,
                "url",
            )
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


def test_chainlink_latest_round_data_decode():
    price = ChainlinkSource("https://polygon-rpc.example", http=FakeHttp(), max_staleness_seconds=999999999).latest_price("0xfeed")

    assert price.price == 61000.5
    assert price.decimals == 8
    assert not price.stale


def test_probability_from_distance_is_monotonic():
    assert probability_from_distance(10, 30) > probability_from_distance(-10, 30)
