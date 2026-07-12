import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .http_utils import HttpClient


class PolymarketDataClient:
    def __init__(self, http: HttpClient = None, base_url: str = "https://gamma-api.polymarket.com"):
        self.http = http or HttpClient(timeout=2.0)
        self.base_url = base_url

    def events(self, limit: int = 100, offset: int = 0, active: bool = True) -> List[Dict[str, Any]]:
        response = self.http.get_json(
            self.base_url,
            "/events",
            {"limit": limit, "offset": offset, "active": str(active).lower()},
        )
        return _as_list(response.data)

    def events_by_slug(self, slug: str) -> List[Dict[str, Any]]:
        response = self.http.get_json(self.base_url, "/events", {"slug": slug})
        return _as_list(response.data)

    def markets(self, limit: int = 100, offset: int = 0, active: bool = True) -> List[Dict[str, Any]]:
        response = self.http.get_json(
            self.base_url,
            "/markets",
            {"limit": limit, "offset": offset, "active": str(active).lower()},
        )
        return _as_list(response.data)


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
