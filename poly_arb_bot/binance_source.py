import time
from dataclasses import dataclass
from typing import Dict, Iterable, List

from .http_utils import HttpClient


BINANCE_SYMBOLS = {
    "Bitcoin": "BTCUSDT",
    "Ethereum": "ETHUSDT",
    "Solana": "SOLUSDT",
    "XRP": "XRPUSDT",
    "Dogecoin": "DOGEUSDT",
    "BNB": "BNBUSDT",
}


@dataclass(frozen=True)
class BinanceTicker:
    symbol: str
    price: float
    bid_price: float
    bid_qty: float
    ask_price: float
    ask_qty: float
    latency_ms: int
    timestamp_ms: int


class BinanceSource:
    def __init__(self, http: HttpClient = None, base_url: str = "https://data-api.binance.vision"):
        self.http = http or HttpClient(timeout=1.5)
        self.base_url = base_url

    def ticker(self, symbol: str) -> BinanceTicker:
        price_response = self.http.get_json(self.base_url, "/api/v3/ticker/price", {"symbol": symbol})
        book_response = self.http.get_json(self.base_url, "/api/v3/ticker/bookTicker", {"symbol": symbol})
        price_data = price_response.data
        book_data = book_response.data
        return BinanceTicker(
            symbol=symbol,
            price=float(price_data["price"]),
            bid_price=float(book_data["bidPrice"]),
            bid_qty=float(book_data["bidQty"]),
            ask_price=float(book_data["askPrice"]),
            ask_qty=float(book_data["askQty"]),
            latency_ms=price_response.elapsed_ms + book_response.elapsed_ms,
            timestamp_ms=int(time.time() * 1000),
        )

    def tickers(self, symbols: Iterable[str]) -> Dict[str, BinanceTicker]:
        return {symbol: self.ticker(symbol) for symbol in symbols}

    def order_book(self, symbol: str, limit: int = 100) -> Dict:
        response = self.http.get_json(self.base_url, "/api/v3/depth", {"symbol": symbol, "limit": limit})
        return {
            "symbol": symbol,
            "latency_ms": response.elapsed_ms,
            "last_update_id": response.data.get("lastUpdateId"),
            "bids": response.data.get("bids", []),
            "asks": response.data.get("asks", []),
        }


class BinanceFailoverSource:
    def __init__(self, http: HttpClient = None, base_urls: List[str] = None):
        self.http = http or HttpClient(timeout=1.5)
        self.base_urls = base_urls or [
            "https://data-api.binance.vision",
            "https://api1.binance.com",
            "https://api2.binance.com",
            "https://api3.binance.com",
            "https://api4.binance.com",
            "https://api-gcp.binance.com",
        ]

    def ticker(self, symbol: str) -> BinanceTicker:
        errors = []
        for base_url in self.base_urls:
            try:
                return BinanceSource(http=self.http, base_url=base_url).ticker(symbol)
            except RuntimeError as exc:
                errors.append(f"{base_url}: {exc}")
        raise RuntimeError("all Binance official endpoints failed: " + " | ".join(errors))
