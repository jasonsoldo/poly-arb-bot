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


def test_dashboard_separates_complete_set_arbitrage_strategies():
    source = Path("web/index.html").read_text(encoding="utf-8")
    for strategy in (
        "INSTANT COMPLETE SET", "INVENTORY REBALANCE", "MAKER COMPLETE SET",
    ):
        assert strategy in source
    for element_id in ("inventoryCard", "makerCard"):
        assert f'id="{element_id}"' in source
    assert "NO FILLS INFERRED" in source
    assert "GEOMETRY" in source


def test_dashboard_header_uses_unambiguous_complete_set_metrics():
    source = Path("web/index.html").read_text(encoding="utf-8")
    for label in (
        "PAIRED EVALS", "INVENTORY ACTIONS", "MAKER GEOMETRY",
        "LOCKED COMPLETE", "REAL ORDERS",
    ):
        assert label in source
    assert "SIM OPENED" not in source
    assert "engine_session" in source
    assert "session_strategy_counts" in source
    assert "session.strategy_counts?.paired_lock?.evaluations" in source
    assert "session.strategy_counts?.inventory_rebalancing_arb?.accepts" in source
    assert "maker_quote_geometry_candidates" in source
    assert "locked_complete" in source


def test_dashboard_separates_current_session_history_and_legacy_inventory():
    source = Path("web/index.html").read_text(encoding="utf-8")
    for label in (
        "CURRENT CONFIG PERFORMANCE", "SESSION EVAL", "HISTORY EVAL",
        "LEGACY INVENTORY", "LEGACY COST / MAX LOSS", "CURRENT POSITIONS / COST",
    ):
        assert label in source
    assert "inventory_cohorts" in source
    assert "historical_completed_excluded" in source


def test_dashboard_renders_latest_asset_pnl_from_completed_shadow_data():
    source = Path("web/index.html").read_text(encoding="utf-8")
    assert "LATEST SIM PNL" in source
    assert "asset_latest_pnl" in source
    assert "assetPnlCell" in source
    assert "NO COMPLETED SHADOW TRADE" in source


def test_dashboard_shows_unknown_analytics_while_background_refresh_runs():
    source = Path("web/index.html").read_text(encoding="utf-8")
    assert "analytics_refreshing" in source
    assert "ANALYTICS REFRESHING" in source
