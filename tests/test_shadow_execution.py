import json

from poly_arb_bot.shadow_execution import ShadowExecutionStateMachine, process_audit_once
from poly_arb_bot.strategy_shadow_lifecycle import StrategyShadowLifecycle


def opportunity():
    return {"event_id": "stable-opportunity", "market_id": "m1", "ts": 123.0, "orphan_leg_loss": 0.2}


def test_shadow_execution_completes_both_simulated_legs_without_real_orders(tmp_path):
    machine = ShadowExecutionStateMachine(tmp_path / "state.json", tmp_path / "execution.jsonl")
    assert machine.process(opportunity()) is True
    assert machine.data["state"] == "IDLE"
    rows = [json.loads(line) for line in (tmp_path / "execution.jsonl").read_text().splitlines()]
    assert [row["state"] for row in rows] == ["PRECHECK", "LEG1_SUBMITTED", "LEG1_FILLED", "LEG2_SUBMITTED", "COMPLETE"]
    assert all(row["real_order_submitted"] is False for row in rows)
    assert machine.process(opportunity()) is False
    assert rows[-1]["event_id"] == "stable-opportunity"


def test_shadow_execution_records_orphan_action(tmp_path):
    machine = ShadowExecutionStateMachine(tmp_path / "state.json", tmp_path / "execution.jsonl")
    machine.process(opportunity(), leg2_result="rejected", orphan_action="hedge")
    rows = [json.loads(line) for line in (tmp_path / "execution.jsonl").read_text().splitlines()]
    states = [row["state"] for row in rows]
    assert states[-3:] == ["LEG2_REJECTED", "ORPHANED", "ORPHAN_HEDGE"]


def test_process_audit_once_persists_offset_and_does_not_repeat(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "execution.jsonl"
    audit_path.write_text(json.dumps({
        "event_type": "shadow_opportunity", "strategy": "paired_lock",
        "market_id": "m1", "ts": 123,
    }) + "\n", encoding="utf-8")
    machine = ShadowExecutionStateMachine(state_path, log_path)

    assert process_audit_once(audit_path, machine) == 1
    assert process_audit_once(audit_path, machine) == 0
    assert machine.data["audit_offset"] == audit_path.stat().st_size


def test_rejected_second_leg_does_not_open_paired_lifecycle_position(tmp_path, monkeypatch):
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text(json.dumps({
        "event_id": "pair-rejected", "event_type": "shadow_opportunity", "strategy": "paired_lock",
        "market_id": "m1", "target_size": 10, "net_cost": 9.7, "ts": 123,
    }) + "\n", encoding="utf-8")
    machine = ShadowExecutionStateMachine(tmp_path / "execution-state.json", tmp_path / "execution.jsonl")
    lifecycle = StrategyShadowLifecycle(tmp_path / "lifecycle-state.json", tmp_path / "complete.jsonl")
    monkeypatch.setenv("SHADOW_LEG2_RESULT", "rejected")
    process_audit_once(audit_path, machine, lifecycle, {
        "m1": {"market_id": "m1", "asset": "BTC", "interval": "5m", "close_ts": 200,
               "settlement_source": "chainlink"},
    })
    assert lifecycle.data["positions"] == {}
