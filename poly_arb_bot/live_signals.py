import math
import time
from dataclasses import dataclass
from typing import Iterable, List

from .binance_source import BinanceSource
from .clob_client import PolymarketClobClient
from .models import MarketSignal, Outcome


@dataclass(frozen=True)
class LiveMarketSpec:
    market_id: str
    title: str
    asset: str
    symbol: str
    open_price: float
    close_ts: int
    up_token_id: str
    down_token_id: str
    max_allowed_price: float = 0.99


def probability_from_distance(distance: float, seconds_to_close: int) -> float:
    seconds = max(seconds_to_close, 1)
    scale = max(0.0005, math.sqrt(seconds) * 0.015)
    probability = 1.0 / (1.0 + math.exp(-distance / scale))
    return min(0.999, max(0.001, probability))


class LiveSignalBuilder:
    def __init__(self, binance: BinanceSource, clob: PolymarketClobClient, order_size: float = 25.0):
        self.binance = binance
        self.clob = clob
        self.order_size = order_size

    def build(self, markets: Iterable[LiveMarketSpec]) -> List[MarketSignal]:
        signals = []
        for market in markets:
            ticker = self.binance.ticker(market.symbol)
            seconds_to_close = int(market.close_ts - time.time())
            distance = ticker.price - market.open_price
            up_probability = probability_from_distance(distance, seconds_to_close)
            signals.append(self._signal_for(market, "Up", market.up_token_id, up_probability, seconds_to_close, distance))
            signals.append(self._signal_for(market, "Down", market.down_token_id, 1.0 - up_probability, seconds_to_close, -distance))
        return signals

    def _signal_for(
        self,
        market: LiveMarketSpec,
        outcome: Outcome,
        token_id: str,
        model_probability: float,
        seconds_to_close: int,
        distance: float,
    ) -> MarketSignal:
        book = self.clob.get_book(token_id)
        expected = book.expected_buy_price(self.order_size)
        if expected is None:
            expected = 1.0
        return MarketSignal(
            market_id=market.market_id,
            title=market.title,
            outcome=outcome,
            market_price=book.best_ask if book.best_ask is not None else expected,
            expected_fill_price=expected,
            model_probability=model_probability,
            seconds_to_close=seconds_to_close,
            distance_to_price_to_beat=distance,
            liquidity=book.ask_liquidity(market.max_allowed_price),
            orderbook_age_ms=book.latency_ms,
            settlement_source_ok=True,
            max_allowed_price=market.max_allowed_price,
        )
