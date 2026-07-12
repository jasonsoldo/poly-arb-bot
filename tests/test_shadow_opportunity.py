from poly_arb_bot.clob_client import ClobLevel
from poly_arb_bot.shadow_opportunity import evaluate_pair, vwap


def test_vwap_requires_full_depth():
    result = vwap([ClobLevel(0.2, 2), ClobLevel(0.4, 2)], 3)
    assert result.complete
    assert result.vwap == 0.26666666666666666


def test_pair_profit_after_fee_and_fok():
    result = evaluate_pair("m", [ClobLevel(0.4, 10)], [ClobLevel(0.5, 10)], 10, fee_rate=0)
    assert result.fok_both_fillable
    assert result.profitable_after_fees
    assert result.profit_if_up == 1


def test_pair_not_opportunity_when_one_side_lacks_depth():
    result = evaluate_pair("m", [ClobLevel(0.4, 10)], [ClobLevel(0.5, 1)], 10, fee_rate=0)
    assert not result.fok_both_fillable
    assert not result.profitable_after_fees
