from poly_arb_bot.http_utils import HttpResponse
from poly_arb_bot.polymarket_data import PolymarketDataClient


class PagedHttp:
    def __init__(self):
        self.offsets = []

    def get_json(self, base_url, path, params):
        self.offsets.append(params["offset"])
        rows = [{"id": str(index)} for index in range(params["offset"], min(params["offset"] + params["limit"], 250))]
        return HttpResponse(rows, 1, "url")


def test_events_pages_past_gamma_single_page_limit():
    http = PagedHttp()
    rows = PolymarketDataClient(http=http).events(limit=250)
    assert len(rows) == 250
    assert http.offsets == [0, 100, 200]


class KeysetHttp:
    def __init__(self):
        self.cursors = []

    def get_json(self, base_url, path, params):
        self.cursors.append(params.get("after_cursor"))
        if not params.get("after_cursor"):
            return HttpResponse({"events": [{"id": "1"}], "next_cursor": "next"}, 1, "url")
        return HttpResponse({"events": [{"id": "2"}], "next_cursor": None}, 1, "url")


def test_events_keyset_uses_cursor_not_offset():
    http = KeysetHttp()
    rows = PolymarketDataClient(http=http).events_keyset(limit=200)
    assert [row["id"] for row in rows] == ["1", "2"]
    assert http.cursors == [None, "next"]
