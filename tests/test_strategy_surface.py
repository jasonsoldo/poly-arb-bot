from pathlib import Path

import poly_arb_bot.web_monitor as web_monitor


HTML = Path("web/index.html").read_text(encoding="utf-8")
ACCEPTANCE = Path("poly_arb_bot/shadow_acceptance.py").read_text(encoding="utf-8")


def test_strategy_roles_separate_primary_models_from_research_observers():
    assert web_monitor.PRIMARY_STRATEGIES == (
        "late_window_directional_ev",
        "low_price_lottery_ev",
        "paired_lock",
    )
    assert web_monitor.ARBITRAGE_OBSERVERS == (
        "split_sell_lock",
        "maker_complete_set_arb",
    )
    assert "inventory_rebalancing_arb" not in web_monitor.STRATEGIES


def test_dashboard_has_three_primary_cards_and_no_retired_strategy_panels():
    # Rewritten dashboard (see docs/dashboard-data-map.md P3): the three
    # primary strategy panels are bound to dirGrid / lotGrid / costChain1.
    for element_id in ("dirGrid", "lotGrid", "costChain1"):
        assert f'id="{element_id}"' in HTML
    for element_id in (
        "inventoryCard", "hedgeAudit", "currentInventory",
        "directionalCard", "lotteryCard", "pairedCard",
    ):
        assert f'id="{element_id}"' not in HTML
    assert "ARBITRAGE RESEARCH" in HTML
    assert "RESEARCH ONLY" in HTML
    assert "NOT ORDERS OR PNL" in HTML


def test_acceptance_requires_observers_but_not_retired_runtime_modules():
    assert '"split_sell_lock", "maker_complete_set_arb"' in ACCEPTANCE
    assert '"terminal_hedge_evaluated"' not in ACCEPTANCE
    assert '"inventory_rebalancing_arb"' not in ACCEPTANCE
