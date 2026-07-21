from pathlib import Path

import poly_arb_bot.web_monitor as web_monitor


HTML = Path("web/index.html").read_text(encoding="utf-8")
ACCEPTANCE = Path("poly_arb_bot/shadow_acceptance.py").read_text(encoding="utf-8")


def test_strategy_surface_focused_on_paired_lock_and_maker_accumulate():
    assert web_monitor.PRIMARY_STRATEGIES == (
        "late_window_directional_ev",
        "low_price_lottery_ev",
        "paired_lock",
    )
    # maker_paired_accumulate is the 4th independent shadow strategy; its
    # episode decisions are produced by poly_arb_bot.maker_shadow and counted
    # from logs/strategy-audit.jsonl.
    assert web_monitor.MAKER_ACCUMULATE_STRATEGIES == ("maker_paired_accumulate",)
    assert "maker_paired_accumulate" in web_monitor.STRATEGIES
    assert web_monitor.MAKER_ACCUMULATE_DECISION_EVENTS == frozenset({
        "maker_episode_opened", "maker_episode_rejected",
    })
    # Focused runtime surface: retired observers are no longer tracked.
    assert "inventory_rebalancing_arb" not in web_monitor.STRATEGIES
    assert "split_sell_lock" not in web_monitor.STRATEGIES
    assert "maker_complete_set_arb" not in web_monitor.STRATEGIES
    assert "microstructure_reversion" not in web_monitor.STRATEGIES
    assert not hasattr(web_monitor, "ARBITRAGE_OBSERVERS")
    assert not hasattr(web_monitor, "CLOB_REVERSION_STRATEGIES")


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
    # Arbitrage research panel removed with the observer surface.
    assert "ARBITRAGE RESEARCH" not in HTML
    assert "renderResearch" not in HTML
    assert "NOT ORDERS OR PNL" in HTML
    # Directional/lottery panels show explicit enablement state.
    assert 'id="dirEnableChip"' in HTML
    assert 'id="lotEnableChip"' in HTML
    assert "strategy_enablement" in HTML


def test_acceptance_tracks_enabled_strategies_without_observer_requirements():
    assert "complete_set_strategies_evaluated" not in ACCEPTANCE
    assert "arbitrage_book_evidence_integrity" not in ACCEPTANCE
    assert "arbitrage_research" not in ACCEPTANCE
    assert "enabled_strategy_evaluations" in ACCEPTANCE
    assert "disabled_strategies_silent" in ACCEPTANCE
    assert '"terminal_hedge_evaluated"' not in ACCEPTANCE
    assert '"inventory_rebalancing_arb"' not in ACCEPTANCE
