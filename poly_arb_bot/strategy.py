import hashlib
from typing import Iterable, List

from .models import MarketSignal, OrderIntent, PositionCurve
from .pnl_curve import curve_after_fill
from .strategy_config import StrategyConfig


class UpDownStrategy:
    def __init__(self, config: StrategyConfig):
        self.config = config

    def build_order_intent(self, signal: MarketSignal, position: PositionCurve) -> OrderIntent:
        max_size = self.config.low_price_max_order_size if signal.expected_fill_price <= 0.20 else self.config.high_confidence_max_order_size
        size = min(max_size, signal.liquidity, self.config.max_order_size)
        next_curve = curve_after_fill(position, signal.outcome, size, signal.expected_fill_price)
        reason = (
            f"edge={signal.edge:.4f}; seconds_to_close={signal.seconds_to_close}; "
            f"after_fill={next_curve.classification}; pnl_up={next_curve.pnl_if_up:.2f}; "
            f"pnl_down={next_curve.pnl_if_down:.2f}"
        )
        raw_id = f"{signal.market_id}:{signal.outcome}:{signal.expected_fill_price}:{size}:{reason}"
        client_order_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:24]
        return OrderIntent(
            market_id=signal.market_id,
            title=signal.title,
            outcome=signal.outcome,
            size=size,
            limit_price=signal.expected_fill_price,
            reason=reason,
            client_order_id=client_order_id,
        )

    def candidates(self, signals: Iterable[MarketSignal]) -> List[MarketSignal]:
        return sorted(
            [signal for signal in signals if signal.edge > self.config.min_edge],
            key=lambda signal: signal.edge,
            reverse=True,
        )
