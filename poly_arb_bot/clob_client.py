import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .http_utils import HttpClient


@dataclass(frozen=True)
class ClobLevel:
    price: float
    size: float


@dataclass(frozen=True)
class ClobBook:
    token_id: str
    bids: List[ClobLevel]
    asks: List[ClobLevel]
    latency_ms: int
    timestamp_ms: int

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    def ask_liquidity(self, max_price: float) -> float:
        return sum(level.size for level in self.asks if level.price <= max_price)

    def expected_buy_price(self, size: float) -> Optional[float]:
        remaining = size
        notional = 0.0
        filled = 0.0
        for level in self.asks:
            take = min(remaining, level.size)
            notional += take * level.price
            filled += take
            remaining -= take
            if remaining <= 1e-9:
                return notional / filled
        return None


class PolymarketClobClient:
    def __init__(self, http: HttpClient = None, base_url: str = "https://clob.polymarket.com"):
        self.http = http or HttpClient(timeout=1.5)
        self.base_url = base_url

    def get_book(self, token_id: str) -> ClobBook:
        response = self.http.get_json(self.base_url, "/book", {"token_id": token_id})
        data = response.data
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"CLOB book rejected token {token_id}: {data['error']}")
        bids = self._levels(data.get("bids", []), reverse=True)
        asks = self._levels(data.get("asks", []), reverse=False)
        return ClobBook(
            token_id=token_id,
            bids=bids,
            asks=asks,
            latency_ms=response.elapsed_ms,
            timestamp_ms=int(time.time() * 1000),
        )

    def get_market_price(self, token_id: str, side: str = "BUY") -> float:
        response = self.http.get_json(self.base_url, "/price", {"token_id": token_id, "side": side})
        data = response.data
        return float(data["price"])

    def get_market_info(self, condition_id: str) -> Dict:
        response = self.http.get_json(self.base_url, f"/clob-markets/{condition_id}")
        if not isinstance(response.data, dict) or response.data.get("error"):
            raise RuntimeError(f"CLOB market rejected condition {condition_id}: {response.data}")
        return response.data

    @staticmethod
    def _levels(rows: List[Dict], reverse: bool) -> List[ClobLevel]:
        levels = []
        for row in rows:
            price = row.get("price") if isinstance(row, dict) else row[0]
            size = row.get("size") if isinstance(row, dict) else row[1]
            levels.append(ClobLevel(float(price), float(size)))
        return sorted(levels, key=lambda level: level.price, reverse=reverse)
