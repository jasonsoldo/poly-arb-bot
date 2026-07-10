from dataclasses import dataclass

from .models import Outcome, PositionCurve


@dataclass(frozen=True)
class PnlCurve:
    market_id: str
    title: str
    total_cost: float
    pnl_if_up: float
    pnl_if_down: float
    classification: str


def classify_curve(pnl_if_up: float, pnl_if_down: float) -> str:
    if pnl_if_up > 0 and pnl_if_down > 0:
        return "both_profit"
    if pnl_if_up > 0 or pnl_if_down > 0:
        return "one_side_profit"
    return "both_loss"


def calculate_curve(position: PositionCurve) -> PnlCurve:
    total_cost = position.total_cost
    pnl_if_up = position.up_shares - total_cost
    pnl_if_down = position.down_shares - total_cost
    return PnlCurve(
        market_id=position.market_id,
        title=position.title,
        total_cost=total_cost,
        pnl_if_up=pnl_if_up,
        pnl_if_down=pnl_if_down,
        classification=classify_curve(pnl_if_up, pnl_if_down),
    )


def curve_after_fill(position: PositionCurve, outcome: Outcome, size: float, price: float) -> PnlCurve:
    if outcome == "Up":
        next_position = PositionCurve(
            market_id=position.market_id,
            title=position.title,
            up_shares=position.up_shares + size,
            up_cost=position.up_cost + size * price,
            down_shares=position.down_shares,
            down_cost=position.down_cost,
        )
    else:
        next_position = PositionCurve(
            market_id=position.market_id,
            title=position.title,
            up_shares=position.up_shares,
            up_cost=position.up_cost,
            down_shares=position.down_shares + size,
            down_cost=position.down_cost + size * price,
        )
    return calculate_curve(next_position)
