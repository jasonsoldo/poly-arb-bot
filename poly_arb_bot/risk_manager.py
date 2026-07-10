from .models import MarketSignal, OrderIntent, PositionCurve, RiskDecision
from .pnl_curve import curve_after_fill
from .strategy_config import StrategyConfig


class RiskManager:
    def __init__(self, config: StrategyConfig, current_daily_loss: float = 0.0):
        self.config = config
        self.current_daily_loss = current_daily_loss

    def check(
        self,
        signal: MarketSignal,
        order: OrderIntent,
        position: PositionCurve,
        total_exposure: float,
        clock_skew_ms: int = 0,
    ) -> RiskDecision:
        if self.current_daily_loss >= self.config.max_daily_loss:
            return RiskDecision(False, "max_daily_loss reached")
        if clock_skew_ms > self.config.max_clock_skew_ms:
            return RiskDecision(False, "clock_sync_check failed")
        if not signal.settlement_source_ok:
            return RiskDecision(False, "settlement_source_check failed")
        if signal.orderbook_age_ms > self.config.stale_orderbook_ms:
            return RiskDecision(False, "stale_orderbook_check failed")
        if not (self.config.min_seconds_to_close <= signal.seconds_to_close <= self.config.max_seconds_to_close):
            return RiskDecision(False, "seconds_to_close outside allowed window")
        if signal.edge <= self.config.min_edge:
            return RiskDecision(False, "edge below min_edge")
        if signal.expected_fill_price > signal.max_allowed_price:
            return RiskDecision(False, "expected_fill_price above max_allowed_price")
        if signal.liquidity < self.config.min_liquidity:
            return RiskDecision(False, "liquidity below min_liquidity")
        if abs(signal.expected_fill_price - signal.market_price) > self.config.max_slippage:
            return RiskDecision(False, "max_slippage exceeded")
        if order.size > self.config.max_order_size:
            return RiskDecision(False, "max_order_size exceeded")
        if order.notional + position.total_cost > self.config.max_position_per_market:
            return RiskDecision(False, "max_position_per_market exceeded")
        if order.notional + total_exposure > self.config.max_total_exposure:
            return RiskDecision(False, "max_total_exposure exceeded")

        next_curve = curve_after_fill(position, order.outcome, order.size, order.limit_price)
        worst_loss = max(0.0, -min(next_curve.pnl_if_up, next_curve.pnl_if_down))
        if worst_loss > self.config.max_loss_per_market:
            return RiskDecision(False, "max_loss_per_market exceeded")

        return RiskDecision(True, "risk checks passed")
