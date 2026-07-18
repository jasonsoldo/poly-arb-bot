from pathlib import Path


def test_dashboard_uses_paired_lock_execution_and_health_fields():
    source = Path("web/index.html").read_text(encoding="utf-8")
    assert "CRYPTO PAIRED LOCK / SHADOW" in source
    assert "EXPECTED EXEC VALUE" in source
    assert "CLOB READY" in source
    assert "SUBSCRIPTION GEN" in source
    assert "EXECUTION STATE" in source
    assert "expected_execution_value" in source
    assert "BTC UP / DOWN SHADOW SCALPER" not in source
    assert "market_matrix" in source
    assert "DIR REF" in source
    assert "reference_prices.assets" in source
    for asset in ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE"):
        assert asset in source
    assert 'id="btc5"' not in source


def test_dashboard_contains_real_analytics_modules_without_static_equity():
    source = Path("web/index.html").read_text(encoding="utf-8")
    for element_id in (
        "simPnl", "winRate", "sharpe", "sharpeSamples", "equityChart",
        "tradeLedger", "strategyScore", "scoreBreakdown", "pnlMeter",
        "rejectionReasons", "latencyRankings", "pipelineSteps",
    ):
        assert f'id="{element_id}"' in source
    assert "equity:after" not in source
    assert '<div class="step active">' not in source
    assert "NO COMPLETED SIMULATIONS" in source
    assert "REAL ORDERS" in source


def test_dashboard_renders_binance_and_chainlink_independently():
    source = Path("web/index.html").read_text(encoding="utf-8")
    assert "sourceState" in source
    assert "v.sources" in source
    for name in ("BINANCE", "COINBASE", "KRAKEN", "CHAINLINK"):
        assert name in source
    assert "NOT RECEIVED" in source


def test_dashboard_uses_consistent_pair_audit_and_status_labels():
    source = Path("web/index.html").read_text(encoding="utf-8")
    for label in (
        "GROSS COST", "FEES", "BUFFER", "NET COST", "GUARANTEED PAYOUT",
        "LOCKED PROFIT", "COMPLETED TRADES", "PAIRED MARKETS READY",
        "NOT READY", "MESSAGE AGE", "REFERENCE ONLY",
    ):
        assert label in source
    assert "current_pair" in source
    assert "expected_execution_value" in source
    assert "h.resyncs" in source
    assert "h.resync_count" not in source


def test_dashboard_renders_three_strategies_without_combining_acceptance():
    source = Path("web/index.html").read_text(encoding="utf-8")
    for strategy in ("DIRECTIONAL EV", "LOW-PRICE LOTTERY", "PAIRED LOCK"):
        assert strategy in source
    assert "strategy_counts" in source
    assert "strategy_latest" in source
    for element_id in ("directionalCard", "lotteryCard", "pairedCard"):
        assert f'id="{element_id}"' in source


def test_dashboard_renders_real_market_dynamic_position_evidence():
    source = Path("web/index.html").read_text(encoding="utf-8")
    for label in (
        "DYNAMIC SIZE", "CAPITAL BUDGET", "MAXIMUM LOSS",
        "MARKET MINIMUM", "SIZE LIMIT",
    ):
        assert label in source
    for field in (
        "dynamic_target_size", "dynamic_all_in_cost", "capital_budget_usd",
        "dynamic_maximum_loss", "market_minimum_size", "size_binding_constraint",
    ):
        assert field in source


def test_dashboard_separates_primary_strategy_cards_from_arbitrage_observers():
    source = Path("web/index.html").read_text(encoding="utf-8")
    for strategy in ("BUY BOTH + MERGE", "SPLIT + SELL BOTH", "MAKER COMPLETE SET"):
        assert strategy in source
    for element_id in ("splitSellCard", "inventoryCard", "makerCard"):
        assert f'id="{element_id}"' not in source
    assert "INVENTORY REBALANCE" not in source
    assert "RESEARCH ONLY / NOT ORDERS OR PNL" in source


def test_dashboard_header_uses_unambiguous_complete_set_metrics():
    source = Path("web/index.html").read_text(encoding="utf-8")
    for label in (
        "BUY+MERGE EVALS", "REPEATABLE PATTERNS", "MAKER GEOMETRY",
        "CURRENT LOCKED COMPLETE", "REAL ORDERS",
    ):
        assert label in source
    assert "SIM OPENED" not in source
    assert "engine_session" in source
    assert "session_strategy_counts" in source
    assert "session.strategy_counts?.paired_lock?.evaluations" in source
    assert "split_sell_near_misses" in source
    assert "required_gross_improvement_bps" in source
    assert "inventory_rebalancing_arb" not in source
    assert "maker_quote_geometry_candidates" in source
    assert "locked_complete" in source


def test_dashboard_separates_current_session_history_without_inventory_strategy():
    source = Path("web/index.html").read_text(encoding="utf-8")
    for label in (
        "CURRENT CONFIG PERFORMANCE", "SESSION EVAL", "HISTORY EVAL",
    ):
        assert label in source
    assert "LEGACY INVENTORY" not in source
    assert "historical_completed_excluded" in source


def test_dashboard_renders_latest_asset_pnl_from_completed_shadow_data():
    source = Path("web/index.html").read_text(encoding="utf-8")
    assert "LATEST SIM PNL" in source
    assert "asset_latest_pnl" in source
    assert "assetPnlCell" in source
    assert "NO COMPLETED SHADOW TRADE" in source


def test_dashboard_separates_real_book_reversion_from_probability_and_locked_arb():
    source = Path("web/index.html").read_text(encoding="utf-8")
    assert 'id="reversionCard"' in source
    assert "DISCOUNT BUY + PROFIT EXIT" in source
    assert "REAL ASK ENTRY / FUTURE REAL BID EXIT" in source
    assert "BOOK EXECUTABLE != FILL" in source
    assert "microstructure_reversion" in source


def test_dashboard_shows_unknown_analytics_while_background_refresh_runs():
    source = Path("web/index.html").read_text(encoding="utf-8")
    assert "analytics_refreshing" in source
    assert "ANALYTICS REFRESHING" in source


def test_dashboard_renders_real_arbitrage_funnel_and_repeatability_evidence():
    source = Path("web/index.html").read_text(encoding="utf-8")
    for element_id in (
        "arbitrageFunnels", "repeatablePatterns", "counterfactualPatterns",
    ):
        assert f'id="{element_id}"' in source
    for label in (
        "REPEATABLE ARBITRAGE RESEARCH", "REPEATABLE PATTERN RESEARCH",
        "INDEPENDENT EPISODES", "LATENCY SURVIVED", "SIZE + DELAY COUNTERFACTUALS",
        "RESEARCH ONLY / NOT ORDERS OR PNL",
    ):
        assert label in source
    assert "arbitrage_research" in source
    assert "repeatable_patterns" in source


def test_dashboard_separates_probability_observation_from_strict_execution():
    source = Path("web/index.html").read_text(encoding="utf-8")
    assert 'id="probabilityCalibration"' in source
    assert "PROBABILITY CALIBRATION / OBSERVATION ONLY" in source
    assert "CALIBRATION ONLY / NOT ORDERS OR PNL" in source
    assert "probability_observations" in source
    assert "origin_accepted" in source
    assert "origin_rejected" in source
