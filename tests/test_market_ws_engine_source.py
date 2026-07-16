from pathlib import Path


SOURCE = Path("cpp/market_ws_engine/market_ws_engine.cpp").read_text(encoding="utf-8")


def test_market_engine_accepts_seven_assets_current_and_next_across_four_timeframes():
    assert "markets.size() > 56" in SOURCE
    assert "markets.size() > 4" not in SOURCE


def test_ws_engine_uses_async_read_and_serialized_heartbeat_writes():
    assert "async_read" in SOURCE
    assert "async_write" in SOURCE
    assert 'queue_write("PING")' in SOURCE
    assert "std::deque<std::string> writes_" in SOURCE
    assert "ws_.read(" not in SOURCE


def test_ws_engine_keeps_initial_and_dynamic_subscriptions_distinct():
    assert 'operation.empty()' in SOURCE
    assert '\\"type\\":\\"market\\"' in SOURCE
    assert 'else message += "],\\"operation\\":\\"" + operation + "\\"}";' in SOURCE
    assert 'subscription(added, "subscribe")' in SOURCE
    assert 'subscription(removed, "unsubscribe")' in SOURCE


def test_ws_engine_reconnects_and_audits_rejected_shadow_evaluations():
    assert '"WS_RECONNECT delay_s=2' in SOURCE
    assert '"SHADOW_EVAL\\tmarket="' in SOURCE
    assert '"SHADOW_OPPORTUNITY\\tmarket="' in SOURCE
    assert '"up_depth"' in SOURCE
    assert '"down_depth"' in SOURCE
    assert '"net_cost_above_threshold"' in SOURCE


def test_ws_engine_does_not_block_websocket_on_rest_bootstrap():
    assert '"/book?token_id=" + token' not in SOURCE
    assert '"BOOK_BOOTSTRAP_SKIPPED reason=ws_snapshot_required tokens="' in SOURCE
    assert '"book_uninitialized"' in SOURCE


def test_ws_engine_writes_structured_shadow_audit():
    assert '\\"event_type\\":\\"shadow_eval\\"' in SOURCE
    assert '\\"event_type\\":\\"shadow_opportunity\\"' in SOURCE
    assert 'logs/shadow-audit.jsonl' in SOURCE


def test_paired_opportunity_carries_canonical_identity_cost_and_risk_fields():
    for field in (
        "condition_id", "asset", "timeframe", "window", "generation", "session",
        "evaluation_sequence", "up_cost", "down_cost", "total_fees",
        "execution_buffer", "up_depth_ok", "down_depth_ok", "book_skew_ms",
        "config_version", "config_hash", "real_order_submissions", "real_fills",
    ):
        assert f'\\"{field}\\":' in SOURCE
    assert "paired-lock-shadow-v2" in SOURCE
    assert "paired_config_hash" in SOURCE


def test_paired_lock_requires_ws_snapshots_sync_buffer_and_profit_threshold():
    assert "ws_snapshot" in SOURCE
    assert "clob_book_stale" in SOURCE
    assert "buffer_per_share_" in SOURCE
    assert "min_profit_" in SOURCE
    assert '\\"strategy\\":\\"paired_lock\\"' in SOURCE
    assert '\\"decision\\":\\"' in SOURCE
    assert "leg_1_fill_probability" in SOURCE
    assert "leg_2_fill_probability" in SOURCE
    assert "time_between_legs_us" in SOURCE
    assert "orphan_leg_loss" in SOURCE
    assert "expected_execution_value" in SOURCE
    assert "execution_value_below_threshold" in SOURCE
    assert 'subscription(added, "subscribe")' in SOURCE
    assert 'subscription(removed, "unsubscribe")' in SOURCE
    assert '"MARKET_RELOAD markets="' in SOURCE
    assert "subscription_generation" in SOURCE
    assert "ws_session_id" in SOURCE
    assert "timestamp_rollback" in SOURCE
    assert "invalid_level_update" in SOURCE
    assert "crossed_book" in SOURCE
    assert "BOOK_RESYNC token=" in SOURCE
    assert "clock_skew_ms" in SOURCE
    assert "source_age_ms" in SOURCE
    assert '\\"fee_rate\\":' in SOURCE
    assert "shadow-health.json" in SOURCE
    assert "ready_markets" in SOURCE
    assert "write_health(false)" in SOURCE
    assert "std::setprecision(15)" in SOURCE


def test_static_books_use_feed_continuity_without_hiding_disconnects():
    assert "book_state_age_ms" in SOURCE
    assert "clob_feed_age_ms" in SOURCE
    assert "effective_book_age_ms = std::min(book_state_age_ms, clob_feed_age_ms)" in SOURCE
    assert "books_synced = effective_book_age_ms <= 750" in SOURCE
    assert '\\"book_age_ms\\":' in SOURCE
    assert '\\"book_age_basis\\":\\"min(book_state_age_ms,clob_feed_age_ms)\\"' in SOURCE
    assert '\\"up_book_age_ms\\":' in SOURCE
    assert '\\"down_book_age_ms\\":' in SOURCE
    assert "schedule_evaluation" in SOURCE
    assert "std::chrono::milliseconds(250)" in SOURCE

def test_audit_stream_preserves_unix_timestamp_precision():
    assert "audit_ << std::setprecision(15);" in SOURCE


def test_directional_inputs_use_real_clob_best_ask_slippage_and_imbalance():
    for field in ("up_best_ask", "down_best_ask", "up_slippage_per_share",
                  "down_slippage_per_share", "up_book_imbalance", "down_book_imbalance",
                  "up_available_depth", "down_available_depth"):
        assert field in SOURCE
    assert "clob_source_timestamp_age_diagnostic" in SOURCE
    assert "source_timestamp_age_ms" in SOURCE


def test_health_explains_paired_market_readiness_gap():
    assert '\\\"waiting_up_snapshot\\\":' in SOURCE
    assert '\\\"waiting_down_snapshot\\\":' in SOURCE


def test_shadow_evaluations_have_stable_sequence_ids():
    assert "evaluation_sequence_" in SOURCE
    assert '\\\"evaluation_sequence\\\":' in SOURCE
    assert '\\\"event_id\\\":\\\"' in SOURCE


def test_shadow_event_ids_are_unique_across_process_restarts():
    assert "run_id_" in SOURCE
    assert "run_id" in SOURCE


def test_engine_can_start_from_retained_config_during_gamma_outage():
    assert 'throw std::runtime_error("market document stale")' not in SOURCE
    assert 'market.close_ts <= now_seconds()' in SOURCE


def test_directional_evaluation_is_gated_by_input_versions():
    assert "version = 0" in SOURCE
    assert "last_strategy_up_version" in SOURCE
    assert "last_strategy_down_version" in SOURCE
    assert "last_strategy_reference_revision" in SOURCE
    assert "last_strategy_time_bucket" in SOURCE
    assert "++books_[asset].version" in SOURCE
    assert "++books_[token].version" in SOURCE
    assert "inputs_unchanged" in SOURCE
    assert "asset->revision" in SOURCE


def test_health_exposes_bounded_local_pipeline_percentiles():
    assert "RollingMetric" in SOURCE
    assert '\\"reference_ipc_receive_age_ms_p95\\"' in SOURCE
    assert '\\"clob_to_strategy_evaluation_us_p95\\"' in SOURCE
    assert '\\"reference_ipc_receive_age_samples\\"' in SOURCE
    assert '\\"clob_to_strategy_evaluation_samples\\"' in SOURCE
    assert '\\"reference_coalesced_frames\\"' in SOURCE


def test_health_exposes_current_engine_session_strategy_counts():
    assert "SessionStrategyCount" in SOURCE
    assert "record_session_strategy" in SOURCE
    assert '\\"run_id\\"' in SOURCE
    assert '\\"engine_started_at\\"' in SOURCE
    assert '\\"session_strategy_counts\\"' in SOURCE


def test_maker_shadow_observes_official_trade_through_without_claiming_fill():
    assert 'type == "last_trade_price"' in SOURCE
    assert "observe_maker_trade" in SOURCE
    assert "maker_quote_geometry_candidates" in SOURCE
    assert "maker_single_leg_trade_throughs" in SOURCE
    assert "maker_both_leg_trade_throughs" in SOURCE
    assert "price_reached_quote_not_queue_fill" in SOURCE
    assert '\\"simulated_fill\\":false' in SOURCE
    assert "maker_quote_observations_.clear()" in SOURCE
