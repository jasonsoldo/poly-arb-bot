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


class WindowHttp:
    def __init__(self):
        self.params = []

    def get_json(self, base_url, path, params):
        self.params.append(params)
        return HttpResponse([], 1, "url")


def test_markets_window_uses_official_end_date_filters():
    http = WindowHttp()
    PolymarketDataClient(http=http).markets_in_window(1783856700, 1783860300)
    assert http.params[0]["end_date_min"] == "2026-07-12T11:45:00Z"
    assert http.params[0]["end_date_max"] == "2026-07-12T12:45:00Z"


def test_markets_by_condition_ids_uses_repeated_query_values():
    http = WindowHttp()
    PolymarketDataClient(http=http).markets_by_condition_ids(["0xa", "0xb"])
    assert http.params[0]["condition_ids"] == ["0xa", "0xb"]


class SeriesHttp:
    def __init__(self):
        self.calls = []

    def get_json(self, base_url, path, params=None):
        self.calls.append((path, params))
        if path == "/series":
            return HttpResponse([{"id": "10684", "events": []}], 1, "url")
        if path == "/events/693216":
            return HttpResponse({"id": "693216", "markets": []}, 1, "url")
        raise AssertionError(path)


def test_series_and_event_use_official_gamma_endpoints():
    http = SeriesHttp()
    client = PolymarketDataClient(http=http)
    assert client.series_by_slug("btc-up-or-down-5m")[0]["id"] == "10684"
    assert client.event_by_id("693216")["id"] == "693216"
    assert http.calls == [
        ("/series", {"slug": "btc-up-or-down-5m", "closed": "false", "exclude_events": "false"}),
        ("/events/693216", None),
    ]


def test_events_by_series_window_uses_official_time_filters():
    http = WindowHttp()
    PolymarketDataClient(http=http).events_by_series_window("10684", 1783900800, 1783904400)
    params = http.params[0]
    assert params["series_id"] == "10684"
    assert params["end_date_min"] == "2026-07-13T00:00:00Z"
    assert params["end_date_max"] == "2026-07-13T01:00:00Z"
    assert params["order"] == "endDate"
