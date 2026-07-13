import json

from poly_arb_bot.web_monitor import build_status


def test_web_status_ignores_snapshot_signals_without_current_market(tmp_path):
    (tmp_path / "live_snapshot.json").write_text(json.dumps({"signals": [{"market_id": "stale"}]}), encoding="utf-8")
    (tmp_path / "live_markets.json").write_text(json.dumps({"markets": []}), encoding="utf-8")
    state = tmp_path / "orders.json"
    state.write_text(json.dumps({"client_order_ids": {"old": "id"}}), encoding="utf-8")

    status = build_status(tmp_path, tmp_path / "orders.jsonl", state)

    assert status["signals"] == []
    assert status["counts"]["executed_orders"] == 0
    assert status["counts"]["risk_decisions"] == 1


def test_web_status_separates_model_edge_from_risk_passed(tmp_path):
    signal = {
        "market_id": "current",
        "model_probability": 0.9,
        "expected_fill_price": 0.5,
        "market_price": 0.5,
        "seconds_to_close": 2050,
        "liquidity": 100,
        "orderbook_age_ms": 10,
        "settlement_source_ok": True,
        "max_allowed_price": 0.99,
    }
    (tmp_path / "live_snapshot.json").write_text(json.dumps({"signals": [signal]}), encoding="utf-8")
    (tmp_path / "live_markets.json").write_text(json.dumps({"markets": [{"market_id": "current"}]}), encoding="utf-8")

    status = build_status(tmp_path, tmp_path / "orders.jsonl", tmp_path / "state.json")

    assert status["counts"]["raw_signals"] == 1
    assert status["counts"]["model_edges"] == 1
    assert status["counts"]["risk_passed"] == 0
    assert status["blocked_reasons"] == {"time_window": 1}


def test_web_status_does_not_count_dry_run_attempt_as_executed(tmp_path):
    (tmp_path / "live_snapshot.json").write_text(json.dumps({"signals": []}), encoding="utf-8")
    (tmp_path / "live_markets.json").write_text(json.dumps({"markets": []}), encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"client_order_ids": {"a": {"status": "dry_run"}}}), encoding="utf-8")

    status = build_status(tmp_path, tmp_path / "orders.jsonl", state)

    assert status["counts"]["executed_orders"] == 0
    assert status["counts"]["shadow_attempts"] == 1


def test_web_status_summarizes_cpp_shadow_audit(tmp_path):
    (tmp_path / "live_snapshot.json").write_text(json.dumps({"signals": []}), encoding="utf-8")
    (tmp_path / "live_markets.json").write_text(json.dumps({"markets": [{"market_id": "m1"}]}), encoding="utf-8")
    log = tmp_path / "shadow.jsonl"
    log.write_text("\n".join([
        json.dumps({"event_type": "shadow_eval", "market_id": "m1", "reason": "no_edge", "fok": True}),
        json.dumps({"event_type": "shadow_opportunity", "market_id": "m1", "fok": True, "profit": 0.1}),
    ]), encoding="utf-8")
    status = build_status(tmp_path, log, tmp_path / "state.json")
    assert status["counts"]["shadow_evaluations"] == 1
    assert status["counts"]["fok_passed"] == 1
    assert status["counts"]["shadow_opportunities"] == 1
    assert status["shadow_markets"][0]["event_type"] == "shadow_opportunity"
