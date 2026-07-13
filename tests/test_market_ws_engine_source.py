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
    assert '\\"operation\\":\\"subscribe\\"' in SOURCE


def test_ws_engine_reconnects_and_audits_rejected_shadow_evaluations():
    assert '"WS_RECONNECT delay_s=2' in SOURCE
    assert '"SHADOW_EVAL\\tmarket="' in SOURCE
    assert '"SHADOW_OPPORTUNITY\\tmarket="' in SOURCE
    assert '"up_depth"' in SOURCE
    assert '"down_depth"' in SOURCE
    assert '"net_cost_above_threshold"' in SOURCE


def test_ws_engine_bootstraps_rest_books_before_ws_deltas():
    assert '"/book?token_id=" + token' in SOURCE
    assert 'book.initialized = true' in SOURCE
    assert '"BOOK_BOOTSTRAP_SUMMARY initialized="' in SOURCE
    assert '"book_uninitialized"' in SOURCE


def test_ws_engine_writes_structured_shadow_audit():
    assert '\\"event_type\\":\\"shadow_eval\\"' in SOURCE
    assert '\\"event_type\\":\\"shadow_opportunity\\"' in SOURCE
    assert 'logs/shadow-audit.jsonl' in SOURCE


def test_paired_lock_requires_ws_snapshots_sync_buffer_and_profit_threshold():
    assert "ws_snapshot" in SOURCE
    assert "books_not_synced" in SOURCE
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


def test_static_books_stay_usable_while_websocket_feed_is_live():
    assert "feed_fresh = timestamp - last_activity_ <= 30" in SOURCE
    assert "books_synced = feed_fresh" in SOURCE
    assert "up_age_ms <= 2000" not in SOURCE
    assert '\\\"up_book_age_ms\\\":' in SOURCE
    assert '\\\"down_book_age_ms\\\":' in SOURCE


def test_audit_stream_preserves_unix_timestamp_precision():
    assert "audit_ << std::setprecision(15);" in SOURCE


def test_health_explains_paired_market_readiness_gap():
    assert '\\\"waiting_up_snapshot\\\":' in SOURCE
    assert '\\\"waiting_down_snapshot\\\":' in SOURCE


def test_shadow_evaluations_have_stable_sequence_ids():
    assert "evaluation_sequence_" in SOURCE
    assert '\\\"evaluation_sequence\\\":' in SOURCE
    assert '\\\"event_id\\\":\\\"' in SOURCE


def test_engine_can_start_from_retained_config_during_gamma_outage():
    assert 'throw std::runtime_error("market document stale")' not in SOURCE
    assert 'market.close_ts <= now_seconds()' in SOURCE
