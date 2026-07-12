import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .http_utils import HttpClient


class PolymarketDataClient:
    def __init__(self, http: HttpClient = None, base_url: str = "https://gamma-api.polymarket.com"):
        self.http = http or HttpClient(timeout=2.0)
        self.base_url = base_url

    def events(self, limit: int = 100, offset: int = 0, active: bool = True) -> List[Dict[str, Any]]:
        return self._paged("/events", limit, offset, active)

    def events_by_slug(self, slug: str) -> List[Dict[str, Any]]:
        response = self.http.get_json(self.base_url, "/events", {"slug": slug})
        return _as_list(response.data)

    def events_keyset(self, limit: int = 1000, active: bool = True) -> List[Dict[str, Any]]:
        return self._keyset("/events/keyset", "events", limit, active)

    def markets(self, limit: int = 100, offset: int = 0, active: bool = True) -> List[Dict[str, Any]]:
        return self._paged("/markets", limit, offset, active)

    def _paged(self, path: str, limit: int, offset: int, active: bool) -> List[Dict[str, Any]]:
        rows = []
        remaining = limit
        while remaining > 0:
            page_size = min(100, remaining)
            response = self.http.get_json(
                self.base_url,
                path,
                {"limit": page_size, "offset": offset + len(rows), "active": str(active).lower(), "closed": "false"},
            )
            page = _as_list(response.data)
            rows.extend(page)
            if len(page) < page_size:
                break
            remaining -= len(page)
        return rows

    def _keyset(self, path: str, key: str, limit: int, active: bool) -> List[Dict[str, Any]]:
        rows = []
        cursor = None
        while len(rows) < limit:
            params = {"limit": min(100, limit - len(rows)), "active": str(active).lower(), "closed": "false"}
            if cursor:
                params["after_cursor"] = cursor
            data = self.http.get_json(self.base_url, path, params).data
            page = data.get(key, []) if isinstance(data, dict) else []
            rows.extend(item for item in page if isinstance(item, dict))
            cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not page or not cursor:
                break
        return rows


def _as_list(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "events", "markets"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def parse_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def first_present(row: Dict[str, Any], names: Iterable[str]) -> Optional[Any]:
    for name in names:
        value = row.get(name)
        if value not in (None, "", []):
            return value
    return None


def parse_timestamp_seconds(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value / 1000) if value > 10_000_000_000 else int(value)
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(raw).timestamp())
    except ValueError:
        return None
