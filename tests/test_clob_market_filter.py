from types import SimpleNamespace

from poly_arb_bot.cli import filter_specs_with_orderbooks
from poly_arb_bot.clob_client import ClobBook, ClobLevel


class FakeClob:
    def __init__(self, books):
        self.books = books

    def get_book(self, token_id):
        return self.books[token_id]


def book(token_id, asks):
    return ClobBook(token_id, [], [ClobLevel(0.4, asks)] if asks else [], 1, 1)


def test_filter_requires_both_up_and_down_asks():
    specs = [SimpleNamespace(up_token_id="up", down_token_id="down", market_id="m1")]
    valid, rejected = filter_specs_with_orderbooks(specs, FakeClob({"up": book("up", 10), "down": book("down", 0)}))
    assert valid == []
    assert rejected == 1


def test_filter_keeps_market_with_both_orderbooks():
    specs = [SimpleNamespace(up_token_id="up", down_token_id="down", market_id="m1")]
    valid, rejected = filter_specs_with_orderbooks(specs, FakeClob({"up": book("up", 10), "down": book("down", 10)}))
    assert [item.market_id for item in valid] == ["m1"]
    assert rejected == 0
