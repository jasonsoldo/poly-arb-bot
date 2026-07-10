from poly_arb_bot.models import PositionCurve
from poly_arb_bot.pnl_curve import calculate_curve, curve_after_fill


def test_both_profit_curve():
    curve = calculate_curve(
        PositionCurve("m1", "Market", up_shares=130, up_cost=60, down_shares=125, down_cost=55)
    )

    assert curve.total_cost == 115
    assert curve.pnl_if_up == 15
    assert curve.pnl_if_down == 10
    assert curve.classification == "both_profit"


def test_one_side_profit_curve():
    curve = calculate_curve(
        PositionCurve("m1", "Market", up_shares=80, up_cost=4, down_shares=240, down_cost=210)
    )

    assert curve.pnl_if_up == -134
    assert curve.pnl_if_down == 26
    assert curve.classification == "one_side_profit"


def test_both_loss_curve():
    curve = calculate_curve(
        PositionCurve("m1", "Market", up_shares=20, up_cost=15, down_shares=10, down_cost=12)
    )

    assert curve.pnl_if_up == -7
    assert curve.pnl_if_down == -17
    assert curve.classification == "both_loss"


def test_curve_after_fill_updates_selected_side():
    position = PositionCurve("m1", "Market", up_shares=10, up_cost=5, down_shares=0, down_cost=0)
    curve = curve_after_fill(position, "Down", size=10, price=0.4)

    assert curve.total_cost == 9
    assert curve.pnl_if_up == 1
    assert curve.pnl_if_down == 1
    assert curve.classification == "both_profit"
