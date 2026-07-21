from pathlib import Path


# NOTE (2026 rewrite verification): web/index.html was rewritten as a
# Neobrutalist terminal dashboard (~916 lines) following
# docs/dashboard-data-map.md panel plan P1-P10. Assertions below were
# updated from the previous panel ids/labels to the new structure while
# preserving each test's original compliance intent (three-strategy
# separation, real=0 invariants, N/A semantics, per-source reference
# rendering, RESEARCH/CALIBRATION ONLY labeling).


def test_dashboard_uses_paired_lock_execution_and_health_fields():
    source = Path("web/index.html").read_text(encoding="utf-8")
    assert "PAIRED LOCK · FULL COST CHAIN" in source
    assert "EEV · CONFIG MODEL" in source
    assert "expected_execution_value" in source
    assert "BTC UP / DOWN SHADOW SCALPER" not in source
    assert "market_matrix" in source
    assert "REF DIVERGENCE" in source
    assert "reference_prices.assets" in source
    assert "PAIRED READY" in source
    assert "subscription_generation" in source
    assert "shadow_execution" in source
    for asset in ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE"):
        assert asset in source
    assert 'id="btc5"' not in source


def test_dashboard_contains_real_analytics_modules_without_static_equity():
    source = Path("web/index.html").read_text(encoding="utf-8")
    for element_id in (
        "simPnl", "kWinRate", "kSharpe", "kSharpeN", "equityChart",
        "tradeLedger", "scoreLbl", "scoreChecks", "exposureFill",
        "rejectionBars", "latencyRows", "pipelineSteps",
    ):
        assert f'id="{element_id}"' in source
    assert "equity:after" not in source
    assert '<div class="step active">' not in source
    assert "NO COMPLETED SIMULATIONS" in source
    assert "REAL ORDERS" in source


def test_dashboard_renders_reference_sources_dynamically_per_asset():
    # dashboard-data-map P6: source columns must be derived from
    # reference_prices.assets.{asset}.sources (dynamic, incl bybit/okx),
    # not a hardcoded binance/coinbase/kraken/chainlink table.
    source = Path("web/index.html").read_text(encoding="utf-8")
    assert "renderReference" in source
    assert "statusSpan" in source
    assert ".sources" in source
    assert "reference_prices" in source
    assert "message_age_ms" in source
    assert "market_type" in source
    assert "quote_currency" in source
    # NOT_RECEIVED semantics: status shown without a price (never STALE).
    assert "NOT_RECEIVED" in source
    assert "statusSpan('NOT_RECEIVED')" in source
    # no hardcoded exchange column names in the frontend source
    assert "BINANCE" not in source
    assert "COINBASE" not in source
    assert "KRAKEN" not in source


def test_dashboard_uses_consistent_pair_audit_and_status_labels():
    source = Path("web/index.html").read_text(encoding="utf-8")
    for label in (
        "GROSS COST", "TOTAL FEES", "EXEC BUFFER", "NET COST",
        "GUARANTEED PAYOUT", "LOCKED PROFIT", "COMPLETED TRADES",
        "PAIRED READY", "NOT READY", "MESSAGE AGE", "REFERENCE ONLY",
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
    for element_id in ("dirGrid", "lotGrid", "costChain1"):
        assert f'id="{element_id}"' in source


def test_dashboard_renders_real_market_dynamic_position_evidence():
    source = Path("web/index.html").read_text(encoding="utf-8")
    for label in (
        "TARGET SIZE", "CAPITAL BUDGET", "MAXIMUM LOSS",
        "MARKET MIN", "BINDING",
    ):
        assert label in source
    for field in (
        "dynamic_target_size", "capital_budget_usd",
        "dynamic_maximum_loss", "market_minimum_size",
        "size_binding_constraint", "sizing_mode",
    ):
        assert field in source
    # POSITION SIZER must not be labeled Kelly (dashboard-data-map P4).
    assert "KELLY" not in source.upper()
    assert "POSITION SIZER · REAL MARKET BOOK SIZED" in source


def test_dashboard_separates_primary_strategy_cards_from_arbitrage_observers():
    source = Path("web/index.html").read_text(encoding="utf-8")
    for strategy in ("SPLIT+SELL", "MAKER COMPLETE SET"):
        assert strategy in source
    for element_id in ("splitSellCard", "inventoryCard", "makerCard"):
        assert f'id="{element_id}"' not in source
    assert "INVENTORY REBALANCE" not in source
    assert "RESEARCH ONLY" in source
    assert "NOT ORDERS OR PNL" in source


def test_dashboard_header_uses_unambiguous_real_counters():
    source = Path("web/index.html").read_text(encoding="utf-8")
    for label in (
        "TOTAL EVALUATIONS", "ACCEPTS", "DUPLICATES", "RESYNCS",
        "FOK PASSED", "REAL ORDERS", "REAL SUBMISSIONS",
    ):
        assert label in source
    assert "SIM OPENED" not in source
    assert "engine_session" in source
    assert "session_strategy_counts" in source
    assert "shadow_accepts" in source
    assert "inventory_rebalancing_arb" not in source


def test_dashboard_separates_current_session_history_without_inventory_strategy():
    source = Path("web/index.html").read_text(encoding="utf-8")
    for label in (
        "SIMULATED PNL · CURRENT CONFIG", "SESSION E", "HISTORY E",
        "HISTORICAL CONFIGS EXCLUDED",
    ):
        assert label in source
    assert "LEGACY INVENTORY" not in source


def test_dashboard_renders_latest_asset_pnl_from_completed_shadow_data():
    source = Path("web/index.html").read_text(encoding="utf-8")
    assert "LATEST SIM PNL" in source
    assert "asset_latest_pnl" in source
    assert "pnlcell" in source
    assert "NO MARKET" in source


def test_dashboard_keeps_research_book_semantics_separate_from_strategies():
    source = Path("web/index.html").read_text(encoding="utf-8")
    assert "BOOK EXECUTABLE" in source
    assert "ARBITRAGE RESEARCH" in source
    assert "RESEARCH ONLY" in source
    assert 'id="reversionCard"' not in source
    assert "microstructure_reversion" not in source


def test_dashboard_shows_unknown_analytics_while_background_refresh_runs():
    source = Path("web/index.html").read_text(encoding="utf-8")
    assert "analytics_refreshing" in source
    assert "ANALYTICS REFRESHING" in source


def test_dashboard_renders_real_arbitrage_funnel_evidence():
    source = Path("web/index.html").read_text(encoding="utf-8")
    assert 'id="research"' in source
    for label in (
        "ARBITRAGE RESEARCH", "EPISODES", "ATTEMPTS", "COMPLETED",
        "ORPHAN", "RESEARCH ONLY", "BOOK EXECUTABLE",
    ):
        assert label in source
    assert "arbitrage_research" in source
    assert "funnels" in source


def test_dashboard_separates_probability_observation_from_strict_execution():
    source = Path("web/index.html").read_text(encoding="utf-8")
    assert 'id="calibration"' in source
    assert "PROBABILITY CALIBRATION" in source
    assert "CALIBRATION ONLY" in source
    assert "NOT ORDERS OR PNL" in source
    assert "STRICT ACCEPT REMAINS SEPARATE" in source
    assert "probability_observations" in source
    assert "probability_calibration" in source
    assert "brier_score" in source


def test_dashboard_renders_maker_paired_accumulate_panel():
    # 4th strategy panel (design §10): real maker_* audit bindings, N/A empty
    # state, ESTIMATED REBATE and configured-queue-model labeling.
    source = Path("web/index.html").read_text(encoding="utf-8")
    assert "MAKER PAIRED ACCUMULATE · SHADOW" in source
    assert "STRATEGY 4/4" in source
    assert "maker_accumulate" in source
    assert "renderMaker" in source
    for element_id in (
        "makerPanel", "makerStateGrid", "makerEpiStats", "makerLimits",
        "makerEpisodes", "makerChain1", "makerChain2", "makerCounts",
    ):
        assert f'id="{element_id}"' in source
    for label in (
        "EPISODE STATE MACHINE", "LEG1 WORKING", "LEG2 WORKING",
        "CLOSED LOSS", "ACTIVE EPISODES", "NO ACTIVE EPISODES",
        "LEG1 AVG", "LEG2 MAX PRICE", "MAKER FEES", "BUFFER / SHARE",
        "GAS / SHARE", "LOCKED MARGIN", "ESTIMATED REBATE",
        "估计返佣", "未到账不计入利润", "CONFIGURED QUEUE MODEL",
        "TOTAL EXPOSURE", "AT-RISK EXPOSURE", "DAILY LOSS",
        "CONSEC ORPHANS", "ORPHAN CIRCUIT", "REALIZED SHADOW PNL",
        "N/A · NO MAKER AUDIT DATA",
    ):
        assert label in source
    # dual fill-mode accounting fields come from maker_leg_filled events
    for field in (
        "strict_would_fill", "queue_would_fill", "shadow_fill_mode",
        "episode_realized_pnl", "estimated_rebate", "orphan_seconds",
        "max_total_exposure", "max_at_risk_exposure", "max_daily_loss",
        "circuit_breaker_open", "consecutive_orphans",
    ):
        assert field in source
    # §16 / §10.3 compliance: no forbidden status words, MESSAGE AGE naming
    assert "SAFE" not in source
    assert "VERIFIED" not in source
    assert ">LATENCY<" not in source
