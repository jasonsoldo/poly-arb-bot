import json
import os
import time
from pathlib import Path

from .logger import JsonlLogger
from .strategy_shadow_lifecycle import StrategyShadowLifecycle, process_audit_once as process_strategy_audit_once


class ShadowExecutionStateMachine:
    def __init__(self, state_path, log_path, checkpoint_interval_seconds=5):
        self.state_path = Path(state_path)
        self.logger = JsonlLogger(Path(log_path))
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()
        self.checkpoint_interval_seconds = float(checkpoint_interval_seconds)
        self._dirty = False
        self._last_checkpoint = time.monotonic()
        for field in ("real_order_submissions", "real_orders", "real_fills"):
            if field not in self.data:
                self.data[field] = 0
        self.data.setdefault("arb_book_observations", {
            "attempts": 0,
            "book_executable": 0,
            "orphaned": 0,
            "invalidated": 0,
        })
        self._mark_dirty()
        self._save(force=True)

    def _load(self):
        if not self.state_path.exists():
            return {"state": "IDLE", "processed": [], "audit_offset": 0,
                    "real_order_submissions": 0, "real_orders": 0, "real_fills": 0}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _write_state(self):
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(temporary, self.state_path)

    def _mark_dirty(self):
        self._dirty = True

    def _save(self, force=False):
        if not self._dirty:
            return False
        if not force and time.monotonic() - self._last_checkpoint < self.checkpoint_interval_seconds:
            return False
        self._write_state()
        self._dirty = False
        self._last_checkpoint = time.monotonic()
        return True

    def flush(self):
        return self._save(force=True)

    def record_arb_observation(self, row):
        event_id = row.get("event_id")
        event_type = row.get("event_type")
        counters = {
            "arb_shadow_attempt": "attempts",
            "arb_shadow_book_executable": "book_executable",
            "arb_shadow_orphaned": "orphaned",
            "arb_shadow_invalidated": "invalidated",
        }
        if not event_id or event_type not in counters:
            return False
        if event_id in self.data["processed"]:
            return False
        counter = counters[event_type]
        self.data["arb_book_observations"][counter] += 1
        self.data["last_arb_observation"] = {
            "producer_event_id": event_id,
            "producer_event_type": event_type,
            "attempt_id": row.get("attempt_id"),
            "market_id": row.get("market_id"),
            "reason": row.get("reason"),
            "updated_at": time.time(),
        }
        self.logger.write("shadow_arb_observation", {
            "producer_event_id": event_id,
            "producer_event_type": event_type,
            "attempt_id": row.get("attempt_id"),
            "strategy": row.get("strategy"),
            "market_id": row.get("market_id"),
            "reason": row.get("reason"),
            "orphan_pnl": row.get("orphan_pnl"),
            "book_executable_quantity": row.get("book_executable_quantity", 0),
            "observation_semantics": "BOOK_EXECUTABLE_NOT_FILL",
            "real_order_submissions": 0,
            "real_orders": 0,
            "real_fills": 0,
        })
        self.data["processed"] = (self.data["processed"] + [event_id])[-10000:]
        self._mark_dirty()
        return True


def process_audit_once(audit_path, machine, lifecycle=None, markets=None):
    audit_path = Path(audit_path)
    if not audit_path.exists():
        return 0
    stat = audit_path.stat()
    identity = f"{stat.st_dev}:{stat.st_ino}"
    previous_identity = machine.data.get("audit_file_identity")
    if (previous_identity and previous_identity != identity) or stat.st_size < machine.data.get("audit_offset", 0):
        machine.data["audit_offset"] = 0
        machine._mark_dirty()
    if previous_identity != identity:
        machine.data["audit_file_identity"] = identity
        machine._mark_dirty()
    processed = 0
    with audit_path.open(encoding="utf-8") as handle:
        handle.seek(machine.data.get("audit_offset", 0))
        while line := handle.readline():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event_type") in {
                "arb_shadow_attempt",
                "arb_shadow_book_executable",
                "arb_shadow_orphaned",
                "arb_shadow_invalidated",
            }:
                processed += machine.record_arb_observation(row)
        offset = handle.tell()
        if offset != machine.data.get("audit_offset"):
            machine.data["audit_offset"] = offset
            machine._mark_dirty()
        machine._save(force=bool(processed))
    return processed


def _json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def run(audit_path, state_path, log_path, poll_seconds=0.5,
        strategy_audit_path="logs/strategy-audit.jsonl",
        strategy_state_path="state/strategy-shadow.json",
        market_path="data/live_markets.json", venue_path="data/venue-status.json"):
    audit_path = Path(audit_path)
    machine = ShadowExecutionStateMachine(state_path, log_path)
    lifecycle = StrategyShadowLifecycle(strategy_state_path, log_path)
    try:
        while True:
            markets = {row.get("market_id"): row for row in _json(market_path, {"markets": []}).get("markets", [])}
            process_audit_once(audit_path, machine, lifecycle, markets)
            process_strategy_audit_once(strategy_audit_path, lifecycle, markets)
            lifecycle.settle(markets, _json(venue_path, {}), time.time())
            lifecycle.refresh_risk_status()
            time.sleep(poll_seconds)
    finally:
        machine.flush()
        lifecycle.flush()


def main():
    run("logs/shadow-audit.jsonl", "state/shadow-execution.json", "logs/shadow-execution.jsonl")


if __name__ == "__main__":
    main()
