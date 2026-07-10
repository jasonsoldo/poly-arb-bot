from poly_arb_bot.execution_engine import ExecutionEngine
from poly_arb_bot.models import MarketSignal, OrderIntent, PositionCurve
from poly_arb_bot.risk_manager import RiskManager
from poly_arb_bot.strategy_config import StrategyConfig


def make_signal(**overrides):
    data = {
        "market_id": "m1",
        "title": "Market",
        "outcome": "Up",
        "market_price": 0.08,
        "expected_fill_price": 0.081,
        "model_probability": 0.14,
        "seconds_to_close": 42,
        "distance_to_price_to_beat": 1.0,
        "liquidity": 100.0,
        "orderbook_age_ms": 100,
        "settlement_source_ok": True,
        "max_allowed_price": 0.1,
    }
    data.update(overrides)
    return MarketSignal(**data)


def make_order(**overrides):
    data = {
        "market_id": "m1",
        "title": "Market",
        "outcome": "Up",
        "size": 20.0,
        "limit_price": 0.081,
        "reason": "test",
        "client_order_id": "abc",
    }
    data.update(overrides)
    return OrderIntent(**data)


def test_risk_accepts_valid_order():
    decision = RiskManager(StrategyConfig()).check(
        make_signal(),
        make_order(),
        PositionCurve("m1", "Market"),
        total_exposure=0,
    )

    assert decision.allowed


def test_risk_rejects_low_price_without_edge():
    decision = RiskManager(StrategyConfig()).check(
        make_signal(model_probability=0.085),
        make_order(),
        PositionCurve("m1", "Market"),
        total_exposure=0,
    )

    assert not decision.allowed
    assert decision.reason == "edge below min_edge"


def test_risk_rejects_stale_orderbook():
    decision = RiskManager(StrategyConfig()).check(
        make_signal(orderbook_age_ms=2000),
        make_order(),
        PositionCurve("m1", "Market"),
        total_exposure=0,
    )

    assert not decision.allowed
    assert decision.reason == "stale_orderbook_check failed"


def test_live_execution_is_blocked_without_explicit_enable():
    result = ExecutionEngine(StrategyConfig(trading_mode="live", live_enabled=False)).submit(make_order())

    assert not result.accepted
    assert result.status == "blocked"
    assert "live_enabled" in result.reason
