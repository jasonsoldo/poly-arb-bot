import json

from poly_arb_bot.shadow_execution import ShadowExecutionStateMachine, process_audit_once


def test_shadow_execution_persists_real_order_invariants_on_initialization(tmp_path):
    state = tmp_path / "state.json"
    ShadowExecutionStateMachine(state, tmp_path / "audit.jsonl")
    stored = json.loads(state.read_text(encoding="utf-8"))
    assert stored["real_order_submissions"] == 0
    assert stored["real_orders"] == 0
    assert stored["real_fills"] == 0


def test_execution_checkpoints_are_dirty_and_coalesced(tmp_path):
    machine = ShadowExecutionStateMachine(
        tmp_path / "state.json", tmp_path / "events.jsonl",
        checkpoint_interval_seconds=5,
    )
    writes = []
    machine._write_state = lambda: writes.append(dict(machine.data))
    machine._mark_dirty()
    machine._save()
    machine._mark_dirty()
    machine._save()
    assert writes == []
    machine.flush()
    assert len(writes) == 1
    machine.flush()
    assert len(writes) == 1


def test_execution_audit_inode_change_resets_cursor(tmp_path):
    audit = tmp_path / "audit.jsonl"
    audit.write_text("{}\n", encoding="utf-8")
    machine = ShadowExecutionStateMachine(tmp_path / "state.json", tmp_path / "events.jsonl")
    process_audit_once(audit, machine)
    old_identity = machine.data["audit_file_identity"]
    moved = tmp_path / "old.jsonl"
    audit.replace(moved)
    audit.write_text("{}\n{}\n", encoding="utf-8")
    process_audit_once(audit, machine)
    assert machine.data["audit_file_identity"] != old_identity
    assert machine.data["audit_offset"] == audit.stat().st_size
def test_legacy_opportunity_is_not_converted_to_synthetic_completion(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "execution.jsonl"
    audit_path.write_text(json.dumps({
        "event_type": "shadow_opportunity", "strategy": "paired_lock",
        "market_id": "m1", "ts": 123,
    }) + "\n", encoding="utf-8")
    machine = ShadowExecutionStateMachine(state_path, log_path)

    assert process_audit_once(audit_path, machine) == 0
    assert process_audit_once(audit_path, machine) == 0
    assert machine.data["audit_offset"] == audit_path.stat().st_size
    assert machine.data["real_fills"] == 0
    assert machine.data["arb_book_observations"]["book_executable"] == 0
    assert not log_path.exists()


def test_canonical_book_observations_preserve_producer_identity(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    rows = [
        {"event_id": "attempt-1", "attempt_id": "a1",
         "event_type": "arb_shadow_attempt", "strategy": "paired_lock",
         "market_id": "m1", "ts": 123},
        {"event_id": "book-1", "attempt_id": "a1",
         "event_type": "arb_shadow_book_executable", "strategy": "paired_lock",
         "market_id": "m1", "book_executable_quantity": 10,
         "observation_semantics": "BOOK_EXECUTABLE_NOT_FILL", "ts": 124},
    ]
    audit_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    machine = ShadowExecutionStateMachine(
        tmp_path / "execution-state.json", tmp_path / "execution.jsonl"
    )

    assert process_audit_once(audit_path, machine) == 2
    assert machine.data["arb_book_observations"] == {
        "attempts": 1, "book_executable": 1, "orphaned": 0,
        "invalidated": 0,
    }
    assert machine.data["real_order_submissions"] == 0
    assert machine.data["real_orders"] == 0
    assert machine.data["real_fills"] == 0
    written = [
        json.loads(line)
        for line in (tmp_path / "execution.jsonl").read_text().splitlines()
    ]
    assert [row["producer_event_id"] for row in written] == [
        "attempt-1", "book-1",
    ]
    assert all(row["observation_semantics"] == "BOOK_EXECUTABLE_NOT_FILL"
               for row in written)


def test_orphan_and_invalidation_are_research_outcomes_not_fills(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text("".join(json.dumps(row) + "\n" for row in (
        {"event_id": "o1", "attempt_id": "a1",
         "event_type": "arb_shadow_orphaned", "strategy": "paired_lock",
         "market_id": "m1", "orphan_pnl": -0.2, "ts": 1},
        {"event_id": "i1", "attempt_id": "a2",
         "event_type": "arb_shadow_invalidated", "strategy": "paired_lock",
         "market_id": "m2", "reason": "session_changed", "ts": 2},
    )), encoding="utf-8")
    machine = ShadowExecutionStateMachine(
        tmp_path / "execution-state.json", tmp_path / "execution.jsonl"
    )

    assert process_audit_once(audit_path, machine) == 2
    assert machine.data["arb_book_observations"]["orphaned"] == 1
    assert machine.data["arb_book_observations"]["invalidated"] == 1
    assert machine.data["real_fills"] == 0
