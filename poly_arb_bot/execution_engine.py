import os

from .models import ExecutionResult, OrderIntent
from .strategy_config import StrategyConfig


class ExecutionEngine:
    def __init__(self, config: StrategyConfig):
        self.config = config

    def submit(self, order: OrderIntent) -> ExecutionResult:
        if self.config.trading_mode != "live":
            return ExecutionResult(True, "dry_run", "order recorded but not sent", order.client_order_id)

        if not self.config.live_enabled:
            return ExecutionResult(False, "blocked", "live mode requires live_enabled=true", order.client_order_id)
        if not os.getenv("POLYMARKET_PRIVATE_KEY"):
            return ExecutionResult(False, "blocked", "missing POLYMARKET_PRIVATE_KEY", order.client_order_id)

        return ExecutionResult(False, "blocked", "real CLOB client is not configured in this build", order.client_order_id)
