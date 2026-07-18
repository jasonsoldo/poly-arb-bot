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


def test_market_channel_uses_official_full_initial_subscription_and_heartbeat():
    handshake = SOURCE.split("void on_handshake", 1)[1].split(
        "void do_read", 1
    )[0]
    ping = SOURCE.split("void schedule_ping()", 1)[1].split(
        "void schedule_reload()", 1
    )[0]

    assert 'queue_write(subscription(assets, ""))' in handshake
    assert "first_count" not in handshake
    assert 'subscription(std::vector<std::string>(assets.begin() + offset' not in handshake
    assert "expires_after(std::chrono::seconds(10))" in ping


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


def test_paired_evaluation_carries_canonical_market_identity():
    paired_eval = SOURCE.split(
        '\\"event_type\\":\\"shadow_eval\\",\\"strategy\\":\\"paired_lock\\"', 1
    )[1].split('record_session_strategy("paired_lock"', 1)[0]
    for field in ("condition_id", "asset", "timeframe", "window", "close_ts"):
        assert f'\\"{field}\\":' in paired_eval


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
    assert "stale_price_changes_ignored" in SOURCE
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


def test_paired_evaluations_explicitly_keep_real_execution_counters_zero():
    paired_evaluation = SOURCE.split(
        '\\"event_type\\":\\"shadow_eval\\"', 1
    )[1].split("record_session_strategy", 1)[0]
    assert '\\"real_order_submissions\\":0' in paired_evaluation
    assert '\\"real_orders\\":0' in paired_evaluation
    assert '\\"real_fills\\":0' in paired_evaluation


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


def test_split_sell_lock_uses_bid_vwap_fees_buffer_and_independent_audit():
    assert "sell_vwap" in SOURCE
    assert "complete_set::evaluate_split_sell" in SOURCE
    assert '\\"strategy\\":\\"split_sell_lock\\"' in SOURCE
    assert "SPLIT_AND_SELL_BOTH" in SOURCE
    assert "shadow_split_sell_eval" in SOURCE
    assert "shadow_split_sell_opportunity" in SOURCE
    assert "up_sell_vwap" in SOURCE
    assert "down_sell_vwap" in SOURCE
    assert "gross_proceeds" in SOURCE
    assert "split_collateral_cost" in SOURCE
    assert "net_proceeds" in SOURCE
    assert "split_sell_buffer_per_share_" in SOURCE
    assert "pre_split_complete_set_available" in SOURCE
    assert '\\"split_sell_config_hash\\":' in SOURCE
    assert "split_sell_was_active" in SOURCE
    assert "split_sell_good && !split_sell_was_active" in SOURCE
    assert "split-sell-shadow-v2" in SOURCE
    assert '\\"profit_threshold_shortfall\\":' in SOURCE
    assert '\\"required_gross_improvement_bps\\":' in SOURCE


def test_websocket_failure_invalidates_all_snapshot_readiness():
    fail_body = SOURCE.split(
        "void fail(const char* stage, beast::error_code ec)", 1
    )[1].split("const std::string host_", 1)[0]
    assert "item.second.ws_snapshot = false" in fail_body
    assert "item.second.bids.clear()" in fail_body
    assert "item.second.asks.clear()" in fail_body
    assert "maker_quote_observations_.clear()" in fail_body
    assert "write_health(false)" in fail_body


def test_price_change_batch_is_applied_before_integrity_resync():
    price_change_body = SOURCE.split(
        '} else if (type == "price_change") {', 1
    )[1].split('} else if (type == "last_trade_price"', 1)[0]
    assert "std::set<std::string> touched_tokens" in price_change_body
    assert "std::map<std::string, std::string> resync_reasons" in price_change_body
    assert price_change_body.index("touched_tokens.insert(token)") < price_change_body.index(
        "for (const auto& token : touched_tokens)"
    )
    assert "books_[token].crossed_since = batch_applied_at" in price_change_body
    assert "source_timestamp <= books_[token].snapshot_timestamp_ms" in price_change_body
    assert "++stale_price_changes_ignored_" in price_change_body
    assert 'resync_reasons[token] = "timestamp_rollback"' not in price_change_body
    assert 'resync_reasons[token] = "crossed_book"' not in price_change_body


def test_live_books_drive_bounded_delayed_arbitrage_observations():
    assert '#include "../strategy/observed_arb.hpp"' in SOURCE
    assert "pending_arb_attempts_" in SOURCE
    assert "active_arb_episodes_" in SOURCE
    assert "counterfactual_sizes_{{1, 2, 5, 10}}" in SOURCE
    assert "counterfactual_delays_us_{{0, 50000, 100000, 250000}}" in SOURCE
    assert "observed_arb::LegOrder::UP_THEN_DOWN" in SOURCE
    assert "observed_arb::LegOrder::DOWN_THEN_UP" in SOURCE
    assert "steady_now_us()" in SOURCE
    assert "queue_arb_audit" in SOURCE
    assert "flush_arb_audit_queue" in SOURCE
    assert "max_arb_audit_queue_" in SOURCE
    assert "arb_audit_backpressure_" in SOURCE
    observation = SOURCE.split("void emit_arb_observation(", 1)[1].split(
        "\n    void update_observed_arbitrage", 1
    )[0]
    assert "queue_arb_audit(record.str())" in observation
    counterfactual = SOURCE.split("void emit_arbitrage_counterfactual(", 1)[1].split(
        "\n    void evaluate()", 1
    )[0]
    assert "audit_ <<" not in counterfactual
    assert "queue_arb_audit(record.str())" in counterfactual
    for event_type in (
        "arb_episode_started", "arb_episode_ended", "arb_shadow_attempt",
        "arb_shadow_leg_result", "arb_shadow_book_executable",
        "arb_shadow_orphaned", "arb_shadow_invalidated",
        "arb_research_summary",
    ):
        assert f'\\"event_type\\":\\"{event_type}\\"' in SOURCE
    for field in (
        "leg_order", "delay_ms", "target_size", "initial_net_cost",
        "delayed_net_cost", "book_executable_quantity", "orphan_pnl",
        "generation", "session", "real_order_submissions", "real_orders",
        "real_fills",
    ):
        assert f'\\"{field}\\":' in SOURCE


def test_engine_emits_dedicated_non_trading_probability_observations():
    assert '"shadow_prediction_observation"' in SOURCE
    assert '\\"opens_position\\":false' in SOURCE
    assert '\\"observation_semantics\\":\\"PROBABILITY_CALIBRATION_NOT_ORDER\\"' in SOURCE
    assert "probability_observations_emitted_" in SOURCE
    assert "calibration_horizon_seconds" in SOURCE


def test_disconnect_and_market_reload_invalidate_pending_arbitrage_attempts():
    reload_body = SOURCE.split("void reload_markets()", 1)[1].split(
        "void queue_write", 1
    )[0]
    fail_body = SOURCE.split(
        "void fail(const char* stage, beast::error_code ec)", 1
    )[1].split("const std::string host_", 1)[0]
    assert "invalidate_arb_attempts" in reload_body
    assert "invalidate_arb_attempts" in fail_body
    assert "pending_arb_attempts_.clear()" in SOURCE
    assert "active_arb_episodes_.clear()" in SOURCE


def test_zero_size_delete_is_idempotent_and_resync_is_debounced():
    update_body = SOURCE.split("bool update_level(Book& book", 1)[1].split(
        "bool crossed", 1
    )[0]
    assert "size == 0) side.erase(price)" in update_body
    assert "!side.count(price)" not in update_body

    resync_body = SOURCE.split("void resync_token", 1)[1].split(
        "void reload_markets", 1
    )[0]
    assert "if (!found->second.ws_snapshot) return" in resync_body
    assert "found->second.bids.clear()" in resync_body
    assert "found->second.asks.clear()" in resync_body
    assert "found->second.crossed_since = 0" in resync_body
    assert "item.second.active_since = 0" in resync_body
    assert "item.second.split_sell_active_since = 0" in resync_body


def test_crossed_book_fails_closed_before_deferred_resync():
    evaluate_body = SOURCE.split("void evaluate()", 1)[1].split(
        "void record_session_strategy", 1
    )[0]
    assert "bool crossed_book_pending = false" in evaluate_body
    assert "if (crossed_book_pending) continue" in evaluate_body
    assert "timestamp - book.crossed_since >= 0.5" in evaluate_body
    assert 'resync_token(token, "crossed_book")' in evaluate_body


def test_opportunity_episode_state_does_not_cross_session_or_generation():
    fail_body = SOURCE.split(
        "void fail(const char* stage, beast::error_code ec)", 1
    )[1].split("const std::string host_", 1)[0]
    assert "item.second.active_since = 0" in fail_body
    assert "item.second.split_sell_active_since = 0" in fail_body

    reload_body = SOURCE.split("void reload_markets()", 1)[1].split(
        "std::string subscription", 1
    )[0]
    assert "item.second.active_since = old->second.active_since" not in reload_body
    assert "item.second.split_sell_active_since =" not in reload_body


def test_paired_lock_emits_one_opportunity_per_continuous_episode():
    evaluate = SOURCE.split("void evaluate()", 1)[1].split(
        "void record_session_strategy", 1
    )[0]

    assert "const bool paired_was_active = item.second.active_since > 0" in evaluate
    assert "if (good && !paired_was_active && audit_)" in evaluate


def test_engine_emits_bounded_multi_size_latency_counterfactual_research():
    assert "shadow_arb_counterfactual" in SOURCE
    assert "counterfactual_sizes_" in SOURCE
    assert "counterfactual_delays_us_" in SOURCE
    assert "evaluate_execution_stress" in SOURCE
    assert '\\"research_only\\":true' in SOURCE
    assert "arb_research_qualified" in SOURCE
    assert "qualification_changed" in SOURCE
    assert "!qualification_changed && !periodic_audit" in SOURCE
    assert "arb_research_up_version" in SOURCE
    assert "up_book.version == market.arb_research_up_version" in SOURCE


def test_book_evaluation_is_event_driven_and_counterfactual_grid_is_rate_limited():
    assert "last_book_evaluation_up_version" in SOURCE
    assert "last_book_evaluation_down_version" in SOURCE
    assert "last_book_evaluation_time_bucket" in SOURCE
    evaluate = SOURCE.split("void evaluate()", 1)[1].split(
        "void record_session_strategy", 1
    )[0]
    skip = "time_bucket == item.second.last_book_evaluation_time_bucket"
    assert "if (!book_changed &&" in evaluate
    assert skip in evaluate
    assert evaluate.index(skip) < evaluate.index("emit_arbitrage_counterfactual(")
    assert evaluate.index(skip) < evaluate.index("update_observed_arbitrage(")

    counterfactual = SOURCE.split("void emit_arbitrage_counterfactual(", 1)[1].split(
        "\n    void evaluate()", 1
    )[0]
    assert "arb_research_last_evaluated" in counterfactual
    assert "counterfactual_min_interval_seconds_" in counterfactual
