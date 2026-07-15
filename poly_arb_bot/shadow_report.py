import json
import math
import os
import statistics
import time
from collections import Counter, deque
from pathlib import Path

from .ev_shadow import strategy_config


def percentile(values, fraction):
    if not values:
        return None
    rows = sorted(values)
    return rows[min(len(rows) - 1, round((len(rows) - 1) * fraction))]


def _rows(path):
    if not path or not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield None


STRATEGIES = ("late_window_directional_ev", "low_price_lottery_ev", "paired_lock")
SUMMARY_VERSION = 1
MAX_RECENT_VALUES = 20_000
MAX_SEEN_EVENTS = 50_000
MAX_COMPLETED_TRADES = 20_000
SUMMARY_CHECKPOINT_SECONDS = 30


def _metrics(ledger):
    wins = sum(item["pnl"] > 0 for item in ledger)
    hourly = Counter()
    for item in ledger:
        hourly[int(item["ts"] // 3600)] += item["pnl"]
    samples = list(hourly.values())
    sharpe = None
    if len(samples) >= 24 and statistics.stdev(samples) > 0:
        sharpe = statistics.mean(samples) / statistics.stdev(samples) * math.sqrt(24 * 365)
    pnl = sum(item["pnl"] for item in ledger)
    return {"completed": len(ledger), "wins": wins, "losses": len(ledger) - wins,
            "simulated_pnl": round(pnl, 12) if ledger else None,
            "win_rate": wins / len(ledger) if ledger else None,
            "sharpe": sharpe, "sharpe_samples": len(samples)}


def _performance_from_rows(rows):
    ledger = []
    for row in rows:
        event_id = row.get("event_id")
        pnl = float(row["realized_simulated_pnl"])
        ledger.append({"ts": float(row.get("ts", 0)), "event_id": event_id,
                       "market_id": row.get("market_id"), "strategy": row.get("strategy"),
                       "asset": row.get("asset"), "timeframe": row.get("timeframe"),
                       "strategy_config_version": row.get("strategy_config_version"),
                       "strategy_config_hash": row.get("strategy_config_hash"),
                       "pnl": pnl, "state": "COMPLETE"})
    ledger.sort(key=lambda item: item["ts"])
    current_hash = strategy_config()[1]
    current_hashes = {
        strategy: strategy_config(strategy)[1]
        for strategy in ("late_window_directional_ev", "low_price_lottery_ev")
    }
    current = [item for item in ledger if item.get("strategy") == "paired_lock" or
               item.get("strategy_config_hash") in {
                   current_hash, current_hashes.get(item.get("strategy")),
               }]
    asset_latest_pnl = {}
    for item in current:
        if item.get("asset"):
            asset_latest_pnl[item["asset"]] = {
                key: item.get(key)
                for key in ("pnl", "strategy", "ts", "market_id", "timeframe")
            }
    equity = 0.0
    curve = []
    for item in current:
        equity += item["pnl"]
        curve.append({"ts": item["ts"], "pnl": item["pnl"], "equity": round(equity, 12),
                      "event_id": item["event_id"]})
    return {
        "performance": _metrics(current),
        "performance_by_strategy": {
            strategy: _metrics([item for item in current if item.get("strategy") == strategy])
            for strategy in STRATEGIES
        },
        "equity_curve": curve,
        "trade_ledger": list(reversed(current[-100:])),
        "asset_latest_pnl": asset_latest_pnl,
        "excluded_pre_rule_compliance": len(ledger) - len(current),
        "excluded_other_strategy_config": sum(
            item.get("strategy") != "paired_lock" and
            item.get("strategy_config_hash") not in {
                current_hash, current_hashes.get(item.get("strategy")),
            }
            for item in ledger
        ),
        "current_strategy_config_hash": current_hash,
        "current_strategy_config_hashes": current_hashes,
    }


def _performance(opportunities, execution_path):
    completed = {}
    for row in _rows(execution_path):
        if row and row.get("event_type") == "shadow_complete":
            completed.setdefault(row.get("event_id"), row)
    return _performance_from_rows(completed.values())


class IncrementalReport:
    def __init__(self, audit_path, execution_path=None, state_path=None):
        self.audit_path = Path(audit_path)
        self.execution_path = Path(execution_path) if execution_path else None
        self.state_path = Path(state_path) if state_path else None
        self.state = self._load_state()
        self._audit_seen_order = deque(
            self.state["audit"]["seen_event_ids"], maxlen=MAX_SEEN_EVENTS,
        )
        self._audit_seen = set(self._audit_seen_order)
        self._execution_seen_order = deque(
            self.state["execution"]["seen_event_ids"], maxlen=MAX_SEEN_EVENTS,
        )
        self._execution_seen = set(self._execution_seen_order)
        self._durations = deque(self.state["audit"]["durations"], maxlen=MAX_RECENT_VALUES)
        self._source_ages = deque(self.state["audit"]["source_ages"], maxlen=MAX_RECENT_VALUES)
        self._completed = deque(
            self.state["execution"]["completed"], maxlen=MAX_COMPLETED_TRADES,
        )
        self._last_save = 0.0
        self.last_bytes_read = 0

    @staticmethod
    def _empty_state():
        return {
            "version": SUMMARY_VERSION,
            "audit": {
                "identity": None, "offset": 0, "evaluations": 0,
                "fok_passed": 0, "accepts": 0, "invalid_json": 0,
                "future_events": 0, "duplicate_events": 0,
                "accepted_evaluations": 0, "rejected_evaluations": 0,
                "rejection_reasons": {}, "markets": [], "seen_event_ids": [],
                "durations": [], "source_ages": [],
            },
            "execution": {
                "identity": None, "offset": 0, "seen_event_ids": [],
                "completed": [], "invalid_json": 0,
            },
        }

    def _load_state(self):
        if not self.state_path or not self.state_path.exists():
            return self._empty_state()
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._empty_state()
        if state.get("version") != SUMMARY_VERSION:
            return self._empty_state()
        return state

    def _save(self):
        if not self.state_path:
            return
        now = time.monotonic()
        if self._last_save and now - self._last_save < SUMMARY_CHECKPOINT_SECONDS:
            return
        self.state["audit"]["seen_event_ids"] = list(self._audit_seen_order)
        self.state["audit"]["durations"] = list(self._durations)
        self.state["audit"]["source_ages"] = list(self._source_ages)
        self.state["execution"]["seen_event_ids"] = list(self._execution_seen_order)
        self.state["execution"]["completed"] = list(self._completed)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(self.state, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, self.state_path)
        self._last_save = now

    @staticmethod
    def _identity(stat):
        return f"{stat.st_dev}:{stat.st_ino}"

    def _consume_file(self, path, bucket_name, consume):
        if not path or not path.exists():
            return False
        bucket = self.state[bucket_name]
        stat = path.stat()
        identity = self._identity(stat)
        changed = False
        if bucket.get("identity") != identity or stat.st_size < int(bucket.get("offset", 0)):
            bucket["identity"] = identity
            bucket["offset"] = 0
            changed = True
        with path.open("rb") as handle:
            handle.seek(int(bucket.get("offset", 0)))
            while True:
                line_start = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    try:
                        row = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        handle.seek(line_start)
                        break
                    self.last_bytes_read += len(line)
                    consume(row)
                    changed = True
                    continue
                self.last_bytes_read += len(line)
                if not line.strip():
                    changed = True
                    continue
                try:
                    row = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    bucket["invalid_json"] = int(bucket.get("invalid_json", 0)) + 1
                else:
                    consume(row)
                changed = True
            offset = handle.tell()
            if offset != bucket.get("offset"):
                bucket["offset"] = offset
                changed = True
        return changed

    def _consume_audit(self, row):
        bucket = self.state["audit"]
        if float(row.get("ts", 0)) > time.time() + 300:
            bucket["future_events"] += 1
            return
        event_id = row.get("event_id")
        if event_id and event_id in self._audit_seen:
            bucket["duplicate_events"] += 1
            return
        if event_id:
            if len(self._audit_seen_order) == MAX_SEEN_EVENTS:
                self._audit_seen.discard(self._audit_seen_order[0])
            self._audit_seen_order.append(event_id)
            self._audit_seen.add(event_id)
        market_id = row.get("market_id")
        if market_id and market_id not in bucket["markets"]:
            bucket["markets"].append(market_id)
        if row.get("event_type") == "shadow_eval":
            bucket["evaluations"] += 1
            bucket["fok_passed"] += int(bool(row.get("fok")))
            accepted = row.get("decision") == "ACCEPT"
            bucket["accepted_evaluations"] += int(accepted)
            bucket["rejected_evaluations"] += int(not accepted)
            if not accepted:
                reason = row.get("reason", "unknown")
                bucket["rejection_reasons"][reason] = bucket["rejection_reasons"].get(reason, 0) + 1
            if row.get("source_age_ms") is not None:
                self._source_ages.append(float(row["source_age_ms"]))
        elif row.get("event_type") == "shadow_opportunity":
            bucket["accepts"] += 1
            if row.get("duration_ms") is not None:
                self._durations.append(float(row["duration_ms"]))

    def _consume_execution(self, row):
        bucket = self.state["execution"]
        if row.get("event_type") != "shadow_complete" or not row.get("event_id"):
            return
        event_id = row["event_id"]
        if event_id in self._execution_seen:
            return
        if len(self._execution_seen_order) == MAX_SEEN_EVENTS:
            self._execution_seen.discard(self._execution_seen_order[0])
        self._execution_seen_order.append(event_id)
        self._execution_seen.add(event_id)
        self._completed.append(row)

    def refresh(self):
        self.last_bytes_read = 0
        changed = self._consume_file(self.audit_path, "audit", self._consume_audit)
        changed = self._consume_file(
            self.execution_path, "execution", self._consume_execution,
        ) or changed
        if changed:
            self._save()
        return self.report()

    def report(self):
        bucket = self.state["audit"]
        durations = self._durations
        source_ages = self._source_ages
        result = {
            "markets_seen": len(bucket["markets"]),
            "evaluations": bucket["evaluations"],
            "fok_passed": bucket["fok_passed"],
            "accepts": bucket["accepts"],
            "invalid_json": bucket["invalid_json"] + self.state["execution"].get("invalid_json", 0),
            "future_events": bucket["future_events"],
            "duplicate_events": bucket["duplicate_events"],
            "accepted_evaluations": bucket["accepted_evaluations"],
            "rejected_evaluations": bucket["rejected_evaluations"],
            "rejection_reasons": dict(bucket["rejection_reasons"]),
            "opportunity_duration_ms": {
                "p50": percentile(durations, .5), "p95": percentile(durations, .95),
                "max": max(durations) if durations else None,
            },
            "source_age_ms": {
                "latest": source_ages[-1] if source_ages else None,
                "p50": percentile(source_ages, .5), "p95": percentile(source_ages, .95),
                "p99": percentile(source_ages, .99),
                "max": max(source_ages) if source_ages else None,
                "samples": len(source_ages),
            },
        }
        result.update(_performance_from_rows(self._completed))
        return result


def build_report(path: Path, execution_path: Path = None):
    reasons = Counter()
    evaluations = accepts = fok_passed = invalid = future = duplicates = 0
    accepted_evaluations = rejected_evaluations = 0
    seen_event_ids = set()
    durations = []
    source_ages = []
    markets = set()
    opportunities = {}
    for row in _rows(path):
            if row is None:
                invalid += 1
                continue
            if float(row.get("ts", 0)) > time.time() + 300:
                future += 1
                continue
            event_id = row.get("event_id")
            if event_id and event_id in seen_event_ids:
                duplicates += 1
                continue
            if event_id:
                seen_event_ids.add(event_id)
            markets.add(row.get("market_id"))
            if row.get("event_type") == "shadow_eval":
                evaluations += 1
                fok_passed += int(bool(row.get("fok")))
                accepted = row.get("decision") == "ACCEPT"
                accepted_evaluations += int(accepted)
                rejected_evaluations += int(not accepted)
                if not accepted:
                    reasons[row.get("reason", "unknown")] += 1
                if row.get("source_age_ms") is not None:
                    source_ages.append(float(row["source_age_ms"]))
            elif row.get("event_type") == "shadow_opportunity":
                accepts += 1
                event_id = row.get("event_id") or f'{row.get("market_id")}:{row.get("ts")}'
                opportunities[event_id] = row
                if row.get("duration_ms") is not None:
                    durations.append(float(row["duration_ms"]))
    result = {
        "markets_seen": len(markets - {None}),
        "evaluations": evaluations,
        "fok_passed": fok_passed,
        "accepts": accepts,
        "invalid_json": invalid,
        "future_events": future,
        "duplicate_events": duplicates,
        "accepted_evaluations": accepted_evaluations,
        "rejected_evaluations": rejected_evaluations,
        "rejection_reasons": dict(reasons),
        "opportunity_duration_ms": {"p50": percentile(durations, 0.5), "p95": percentile(durations, 0.95), "max": max(durations) if durations else None},
        "source_age_ms": {"latest": source_ages[-1] if source_ages else None,
                          "p50": percentile(source_ages, 0.5), "p95": percentile(source_ages, 0.95),
                          "p99": percentile(source_ages, 0.99), "max": max(source_ages) if source_ages else None,
                          "samples": len(source_ages)},
    }
    result.update(_performance(opportunities, execution_path))
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="logs/shadow-audit.jsonl")
    print(json.dumps(build_report(Path(parser.parse_args().path)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
