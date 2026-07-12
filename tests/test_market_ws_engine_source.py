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
