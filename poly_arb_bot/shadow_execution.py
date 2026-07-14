import json
import os
import time
from pathlib import Path

from .logger import JsonlLogger
from .strategy_shadow_lifecycle import StrategyShadowLifecycle, process_audit_once as process_strategy_audit_once


class ShadowExecutionStateMachine:
    def __init__(self, state_path, log_path):
        self.state_path = Path(state_path)
        self.logger = JsonlLogger(Path(log_path))
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self):
        if not self.state_path.exists():
            return {"state": "IDLE", "processed": [], "audit_offset": 0}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save(self):
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(temporary, self.state_path)

    def _transition(self, state, event_id, market_id, detail=None):
        self.data.update({"state": state, "event_id": event_id, "market_id": market_id, "updated_at": time.time()})
        self.logger.write("shadow_execution", {
            "strategy": "paired_lock", "state": state, "event_id": event_id,
            "market_id": market_id, "detail": detail or {}, "real_order_submitted": False,
        })
        self._save()

    def process(self, opportunity, leg1_result="filled", leg2_result="filled", orphan_action="hold"):
        event_id = opportunity.get("event_id") or f'{opportunity.get("market_id")}:{opportunity.get("ts")}'
        if event_id in self.data["processed"]:
            return False
        self.data["last_completed_event_id"] = None
        market_id = opportunity["market_id"]
        self._transition("PRECHECK", event_id, market_id)
        self._transition("LEG1_SUBMITTED", event_id, market_id, {"simulated": True})
        if leg1_result != "filled":
            self._transition("LEG1_REJECTED", event_id, market_id)
        else:
            self._transition("LEG1_FILLED", event_id, market_id)
            self._transition("LEG2_SUBMITTED", event_id, market_id, {"simulated": True})
            if leg2_result == "filled":
                self._transition("COMPLETE", event_id, market_id)
                self.data["last_completed_event_id"] = event_id
            else:
                self._transition("LEG2_REJECTED", event_id, market_id)
                self._transition("ORPHANED", event_id, market_id, {"orphan_leg_loss": opportunity.get("orphan_leg_loss")})
                self._transition(f"ORPHAN_{orphan_action.upper()}", event_id, market_id)
        self.data["processed"] = (self.data["processed"] + [event_id])[-10000:]
        self.data["state"] = "IDLE"
        self._save()
        return True


def process_audit_once(audit_path, machine, lifecycle=None, markets=None):
    audit_path = Path(audit_path)
    if not audit_path.exists():
        return 0
    if audit_path.stat().st_size < machine.data.get("audit_offset", 0):
        machine.data["audit_offset"] = 0
    processed = 0
    with audit_path.open(encoding="utf-8") as handle:
        handle.seek(machine.data.get("audit_offset", 0))
        while line := handle.readline():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event_type") == "shadow_opportunity" and row.get("strategy") == "paired_lock":
                handled = machine.process(
                    row,
                    os.getenv("SHADOW_LEG1_RESULT", "filled"),
                    os.getenv("SHADOW_LEG2_RESULT", "filled"),
                    os.getenv("SHADOW_ORPHAN_ACTION", "hold"),
                )
                processed += handled
                if (handled and lifecycle and machine.data.get("last_completed_event_id") == row.get("event_id")):
                    lifecycle.consume(row, markets or {})
        machine.data["audit_offset"] = handle.tell()
        machine._save()
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
    while True:
        markets = {row.get("market_id"): row for row in _json(market_path, {"markets": []}).get("markets", [])}
        process_audit_once(audit_path, machine, lifecycle, markets)
        process_strategy_audit_once(strategy_audit_path, lifecycle, markets)
        lifecycle.settle(markets, _json(venue_path, {}), time.time())
        time.sleep(poll_seconds)


def main():
    run("logs/shadow-audit.jsonl", "state/shadow-execution.json", "logs/shadow-execution.jsonl")


if __name__ == "__main__":
    main()
