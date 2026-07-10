from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyConfig:
    trading_mode: str = "dry_run"
    live_enabled: bool = False
    max_position_per_market: float = 5000.0
    max_order_size: float = 250.0
    max_total_exposure: float = 20000.0
    max_loss_per_market: float = 300.0
    max_daily_loss: float = 1000.0
    min_edge: float = 0.015
    min_liquidity: float = 20.0
    max_slippage: float = 0.01
    stale_orderbook_ms: int = 750
    max_clock_skew_ms: int = 250
    min_seconds_to_close: int = -30
    max_seconds_to_close: int = 900
    low_price_max_order_size: float = 25.0
    high_confidence_max_order_size: float = 250.0
