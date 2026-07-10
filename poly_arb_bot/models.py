from dataclasses import dataclass
from typing import Literal

Outcome = Literal["Up", "Down"]
TradingMode = Literal["dry_run", "simulation", "live"]


@dataclass(frozen=True)
class PositionCurve:
    market_id: str
    title: str
    up_shares: float = 0.0
    up_cost: float = 0.0
    down_shares: float = 0.0
    down_cost: float = 0.0

    @property
    def total_cost(self) -> float:
        return self.up_cost + self.down_cost


@dataclass(frozen=True)
class MarketSignal:
    market_id: str
    title: str
    outcome: Outcome
    market_price: float
    expected_fill_price: float
    model_probability: float
    seconds_to_close: int
    distance_to_price_to_beat: float
    liquidity: float
    orderbook_age_ms: int
    settlement_source_ok: bool
    max_allowed_price: float

    @property
    def edge(self) -> float:
        return self.model_probability - self.expected_fill_price


@dataclass(frozen=True)
class OrderIntent:
    market_id: str
    title: str
    outcome: Outcome
    size: float
    limit_price: float
    reason: str
    client_order_id: str

    @property
    def notional(self) -> float:
        return self.size * self.limit_price


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class ExecutionResult:
    accepted: bool
    status: str
    reason: str
    client_order_id: str
