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
    assert '"no_edge"' in SOURCE


def test_ws_engine_bootstraps_rest_books_before_ws_deltas():
    assert '"/book?token_id=" + token' in SOURCE
    assert 'book.initialized = true' in SOURCE
    assert '"BOOK_BOOTSTRAP_SUMMARY initialized="' in SOURCE
    assert '"book_uninitialized"' in SOURCE
