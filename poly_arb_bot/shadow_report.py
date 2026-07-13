import json
import math
import statistics
from collections import Counter
from pathlib import Path


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


def _performance(opportunities, execution_path):
    completed = {}
    for row in _rows(execution_path):
        if row and row.get("event_type") == "shadow_execution" and row.get("state") == "COMPLETE":
            completed.setdefault(row.get("event_id"), row)
    ledger = []
    for event_id, row in completed.items():
        opportunity = opportunities.get(event_id)
        if not opportunity:
            continue
        pnl = float(opportunity.get("realized_simulated_pnl", opportunity.get("expected_execution_value", 0)))
        ledger.append({"ts": float(row.get("ts", 0)), "event_id": event_id,
                       "market_id": row.get("market_id"), "pnl": pnl, "state": "COMPLETE"})
    ledger.sort(key=lambda item: item["ts"])
    equity = 0.0
    curve = []
    for item in ledger:
        equity += item["pnl"]
        curve.append({"ts": item["ts"], "pnl": item["pnl"], "equity": round(equity, 12),
                      "event_id": item["event_id"]})
    wins = sum(item["pnl"] > 0 for item in ledger)
    hourly = Counter()
    for item in ledger:
        hourly[int(item["ts"] // 3600)] += item["pnl"]
    samples = list(hourly.values())
    sharpe = None
    if len(samples) >= 24 and statistics.stdev(samples) > 0:
        sharpe = statistics.mean(samples) / statistics.stdev(samples) * math.sqrt(24 * 365)
    return {
        "performance": {"completed": len(ledger), "wins": wins, "losses": len(ledger) - wins,
                        "simulated_pnl": round(equity, 12),
                        "win_rate": wins / len(ledger) if ledger else None,
                        "sharpe": sharpe, "sharpe_samples": len(samples)},
        "equity_curve": curve,
        "trade_ledger": list(reversed(ledger[-100:])),
    }


def build_report(path: Path, execution_path: Path = None):
    reasons = Counter()
    evaluations = accepts = fok_passed = invalid = 0
    durations = []
    source_ages = []
    markets = set()
    opportunities = {}
    for row in _rows(path):
            if row is None:
                invalid += 1
                continue
            markets.add(row.get("market_id"))
            if row.get("event_type") == "shadow_eval":
                evaluations += 1
                fok_passed += int(bool(row.get("fok")))
                reasons[row.get("reason", "unknown")] += 1
                if row.get("source_age_ms") is not None:
                    source_ages.append(float(row["source_age_ms"]))
            elif row.get("event_type") == "shadow_opportunity":
                accepts += 1
                event_id = f'{row.get("market_id")}:{row.get("ts")}'
                opportunities[event_id] = row
                if row.get("duration_ms") is not None:
                    durations.append(float(row["duration_ms"]))
    result = {
        "markets_seen": len(markets - {None}),
        "evaluations": evaluations,
        "fok_passed": fok_passed,
        "accepts": accepts,
        "invalid_json": invalid,
        "rejection_reasons": dict(reasons),
        "opportunity_duration_ms": {"p50": percentile(durations, 0.5), "p95": percentile(durations, 0.95), "max": max(durations) if durations else None},
        "source_age_ms": {"p50": percentile(source_ages, 0.5), "p95": percentile(source_ages, 0.95), "max": max(source_ages) if source_ages else None},
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
