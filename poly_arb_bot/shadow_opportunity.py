import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from .clob_client import ClobLevel


@dataclass(frozen=True)
class VwapResult:
    requested_size: float
    filled_size: float
    vwap: Optional[float]
    notional: float
    complete: bool


@dataclass(frozen=True)
class PairOpportunity:
    ts: float
    market_id: str
    size: float
    up_vwap: Optional[float]
    down_vwap: Optional[float]
    up_fee: float
    down_fee: float
    total_cost: Optional[float]
    profit_if_up: Optional[float]
    profit_if_down: Optional[float]
    both_sides_have_depth: bool
    fok_both_fillable: bool
    profitable_after_fees: bool


def vwap(levels: Iterable[ClobLevel], size: float) -> VwapResult:
    remaining = max(0.0, float(size))
    notional = 0.0
    filled = 0.0
    for level in levels:
        take = min(remaining, max(0.0, level.size))
        notional += take * level.price
        filled += take
        remaining -= take
        if remaining <= 1e-9:
            break
    return VwapResult(size, filled, notional / filled if filled else None, notional, remaining <= 1e-9)


def taker_fee(shares: float, price: float, fee_rate: float) -> float:
    return max(0.0, shares * fee_rate * price * (1.0 - price))


def evaluate_pair(market_id: str, up_asks: List[ClobLevel], down_asks: List[ClobLevel], size: float, fee_rate: float = 0.07) -> PairOpportunity:
    up = vwap(up_asks, size)
    down = vwap(down_asks, size)
    complete = up.complete and down.complete
    up_fee = taker_fee(up.filled_size, up.vwap or 0.0, fee_rate)
    down_fee = taker_fee(down.filled_size, down.vwap or 0.0, fee_rate)
    total = up.notional + down.notional + up_fee + down_fee if complete else None
    profit = size - total if total is not None else None
    return PairOpportunity(
        time.time(), market_id, size, up.vwap, down.vwap, up_fee, down_fee, total,
        profit, profit, up.filled_size > 0 and down.filled_size > 0, complete, bool(profit is not None and profit > 0),
    )


class LocalOrderBook:
    def __init__(self, token_id: str):
        self.token_id = token_id
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.updated_at = 0.0
        self.last_event = ""

    def snapshot(self, bids, asks, event_type="book"):
        self.bids = {float(row["price"]): float(row["size"]) for row in bids}
        self.asks = {float(row["price"]): float(row["size"]) for row in asks}
        self.updated_at = time.time()
        self.last_event = event_type

    def price_change(self, changes):
        for row in changes:
            side = self.bids if str(row.get("side", "")).upper() == "BUY" else self.asks
            price, size = float(row["price"]), float(row["size"])
            if size <= 0:
                side.pop(price, None)
            else:
                side[price] = size
        self.updated_at = time.time()
        self.last_event = "price_change"

    def asks_for_vwap(self):
        return [ClobLevel(price, self.asks[price]) for price in sorted(self.asks)]
