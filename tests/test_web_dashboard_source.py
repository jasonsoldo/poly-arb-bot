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
    assert "binance_stale" in source
    assert "chainlink_stale" in source
    assert "BINANCE" in source
    assert "CHAINLINK" in source


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
