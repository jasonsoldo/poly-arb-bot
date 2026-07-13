from pathlib import Path


SOURCE = Path("cpp/market_ws_engine/market_ws_engine.cpp").read_text(encoding="utf-8")


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
