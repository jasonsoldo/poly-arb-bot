import json
from collections import Counter
from pathlib import Path


def percentile(values, fraction):
    if not values:
        return None
    rows = sorted(values)
    return rows[min(len(rows) - 1, round((len(rows) - 1) * fraction))]


def build_report(path: Path):
    reasons = Counter()
    evaluations = accepts = fok_passed = invalid = 0
    durations = []
    source_ages = []
    markets = set()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
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
                if row.get("duration_ms") is not None:
                    durations.append(float(row["duration_ms"]))
    return {
        "markets_seen": len(markets - {None}),
        "evaluations": evaluations,
        "fok_passed": fok_passed,
        "accepts": accepts,
        "invalid_json": invalid,
        "rejection_reasons": dict(reasons),
        "opportunity_duration_ms": {"p50": percentile(durations, 0.5), "p95": percentile(durations, 0.95), "max": max(durations) if durations else None},
        "source_age_ms": {"p50": percentile(source_ages, 0.5), "p95": percentile(source_ages, 0.95), "max": max(source_ages) if source_ages else None},
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="logs/shadow-audit.jsonl")
    print(json.dumps(build_report(Path(parser.parse_args().path)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
