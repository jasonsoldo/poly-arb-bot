import os

from .models import ExecutionResult, OrderIntent
from .strategy_config import StrategyConfig
from .state_store import JsonStateStore


class ExecutionEngine:
    def __init__(self, config: StrategyConfig, state_store: JsonStateStore = None):
        self.config = config
        self.state_store = state_store

    def submit(self, order: OrderIntent) -> ExecutionResult:
        if self.state_store and self.state_store.seen_order(order.client_order_id):
            return ExecutionResult(False, "blocked", "duplicate_order_guard blocked client_order_id", order.client_order_id)

        if self.config.trading_mode != "live":
            if self.state_store:
                self.state_store.record_order(order.client_order_id, {"status": "dry_run", "market_id": order.market_id})
            return ExecutionResult(True, "dry_run", "order recorded but not sent", order.client_order_id)

        if not self.config.live_enabled:
            return ExecutionResult(False, "blocked", "live mode requires live_enabled=true", order.client_order_id)
        if not os.getenv("POLYMARKET_PRIVATE_KEY"):
            return ExecutionResult(False, "blocked", "missing POLYMARKET_PRIVATE_KEY", order.client_order_id)

        return ExecutionResult(False, "blocked", "real CLOB client is not configured in this build", order.client_order_id)
