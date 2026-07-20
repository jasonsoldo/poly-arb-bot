"""paired_lock opportunity measurement over the canonical Shadow audit log.

Reads logs/shadow-audit.jsonl (strategy == "paired_lock", event_type ==
"shadow_eval") and measures how often the per-share net cost of the paired
Up/Down lock dropped below configured thresholds.

Per-share net cost follows the analysis convention:

    shares              = gross_cost / (up_vwap + down_vwap)
    net_cost_per_share  = net_cost / shares

A TRUE opportunity is only net_cost_per_share < 1.0 (net cost below the
guaranteed $1 payout). Values in [1.0, 1.01) are near-miss observations and
must never be reported as opportunities.

Only real audit data is used. The report always states the sample time
window and the evaluation base (valid vs excluded evaluations).
"""

import json
import os
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STATE_VERSION = 1
MAX_SEEN_EVENTS = 50_000
MAX_COMPLETED_RUNS = 20_000
MAX_STORED_COSTS = 2_000_000

# Cumulative measurement thresholds (per-share net cost).
COST_THRESHOLDS = (0.995, 1.0, 1.005, 1.01, 1.02)

EXCLUSION_CLOSING_WINDOW = "closing_window"
EXCLUSION_EMPTY_BOOK = "empty_book"
EXCLUSION_INCOMPLETE_DATA = "incomplete_data"


@dataclass(frozen=True)
class PairedReportConfig:
    min_seconds_to_close: float = 20.0
    opportunity_threshold: float = 1.0
    max_run_gap_seconds: float = 30.0
    top_reasons: int = 10


def percentile(values, fraction):
    if not values:
        return None
    rows = sorted(values)
    return rows[min(len(rows) - 1, round((len(rows) - 1) * fraction))]


def _utc(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _empty_state():
    return {
        "version": STATE_VERSION,
        "file": {"identity": None, "offset": 0},
        "lines_read": 0,
        "invalid_json": 0,
        "future_events": 0,
        "duplicate_events": 0,
        "events_total": 0,
        "valid": 0,
        "accepts_valid": 0,
        "excluded": {
            EXCLUSION_CLOSING_WINDOW: 0,
            EXCLUSION_EMPTY_BOOK: 0,
            EXCLUSION_INCOMPLETE_DATA: 0,
        },
        "costs": [],
        "rejections": {},
        "groups": {},
        "runs": [],
        "open_runs": {},
        "seen_event_ids": [],
        "first_ts": None,
        "last_ts": None,
        "valid_first_ts": None,
        "valid_last_ts": None,
    }


class PairedOpportunityAccumulator:
    """Incremental, offset-resumable accumulator for paired_lock audit rows."""

    def __init__(self, config=None, state=None):
        self.config = config or PairedReportConfig()
        self.state = state if state is not None else _empty_state()
        self._seen_order = deque(self.state["seen_event_ids"], maxlen=MAX_SEEN_EVENTS)
        self._seen = set(self._seen_order)
        self.last_bytes_read = 0

    @classmethod
    def load(cls, state_path, config=None):
        path = Path(state_path)
        if path.exists():
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = None
            if isinstance(state, dict) and state.get("version") == STATE_VERSION:
                return cls(config=config, state=state)
        return cls(config=config)

    def save(self, state_path):
        path = Path(state_path)
        self.state["seen_event_ids"] = list(self._seen_order)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(self.state, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, path)

    # ------------------------------------------------------------------ file

    @staticmethod
    def _identity(stat):
        return f"{stat.st_dev}:{stat.st_ino}"

    def consume_file(self, audit_path):
        """Stream new audit rows from the stored offset. Returns True if changed."""
        path = Path(audit_path)
        if not path.exists():
            return False
        bucket = self.state["file"]
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
                    # Partial trailing line: only consume if it already parses.
                    try:
                        row = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        handle.seek(line_start)
                        break
                    self.last_bytes_read += len(line)
                    self._consume_row(row)
                    changed = True
                    continue
                self.last_bytes_read += len(line)
                self.state["lines_read"] += 1
                if not line.strip():
                    changed = True
                    continue
                try:
                    row = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.state["invalid_json"] += 1
                else:
                    self._consume_row(row)
                changed = True
            offset = handle.tell()
            if offset != bucket.get("offset"):
                bucket["offset"] = offset
                changed = True
        return changed

    # ------------------------------------------------------------------ rows

    def _classify(self, row):
        """Return (exclusion_reason, net_cost_per_share)."""
        config = self.config
        seconds_to_close = row.get("seconds_to_close")
        if row.get("reason") == EXCLUSION_CLOSING_WINDOW or (
            seconds_to_close is not None
            and float(seconds_to_close) < config.min_seconds_to_close
        ):
            return EXCLUSION_CLOSING_WINDOW, None
        up_vwap = row.get("up_vwap")
        down_vwap = row.get("down_vwap")
        up_fill = row.get("up_fill")
        down_fill = row.get("down_fill")
        try:
            legs_filled = float(up_fill) > 0 and float(down_fill) > 0
        except (TypeError, ValueError):
            legs_filled = False
        try:
            vwap_sum = float(up_vwap) + float(down_vwap)
        except (TypeError, ValueError):
            vwap_sum = 0.0
        if not legs_filled or vwap_sum <= 0:
            return EXCLUSION_EMPTY_BOOK, None
        gross_cost = row.get("gross_cost")
        net_cost = row.get("net_cost")
        try:
            gross_cost = float(gross_cost)
            net_cost = float(net_cost)
        except (TypeError, ValueError):
            return EXCLUSION_INCOMPLETE_DATA, None
        if gross_cost <= 0 or net_cost <= 0:
            return EXCLUSION_INCOMPLETE_DATA, None
        shares = gross_cost / vwap_sum
        if shares <= 0:
            return EXCLUSION_INCOMPLETE_DATA, None
        return None, net_cost / shares

    def _consume_row(self, row):
        if not isinstance(row, dict):
            return
        if row.get("strategy") != "paired_lock" or row.get("event_type") != "shadow_eval":
            return
        try:
            ts = float(row.get("ts") or row.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts > time.time() + 300:
            self.state["future_events"] += 1
            return
        event_id = row.get("event_id")
        if event_id and event_id in self._seen:
            self.state["duplicate_events"] += 1
            return
        if event_id:
            if len(self._seen_order) == MAX_SEEN_EVENTS:
                self._seen.discard(self._seen_order[0])
            self._seen_order.append(event_id)
            self._seen.add(event_id)
        self.state["events_total"] += 1
        if ts > 0:
            self.state["first_ts"] = ts if self.state["first_ts"] is None else min(self.state["first_ts"], ts)
            self.state["last_ts"] = ts if self.state["last_ts"] is None else max(self.state["last_ts"], ts)
        if row.get("decision") != "ACCEPT":
            reason = str(row.get("reason") or "unknown")
            rejections = self.state["rejections"]
            rejections[reason] = rejections.get(reason, 0) + 1

        exclusion, cost = self._classify(row)
        if exclusion is not None:
            self.state["excluded"][exclusion] += 1
            return

        self.state["valid"] += 1
        if row.get("decision") == "ACCEPT":
            self.state["accepts_valid"] += 1
        if ts > 0:
            self.state["valid_first_ts"] = (
                ts if self.state["valid_first_ts"] is None else min(self.state["valid_first_ts"], ts)
            )
            self.state["valid_last_ts"] = (
                ts if self.state["valid_last_ts"] is None else max(self.state["valid_last_ts"], ts)
            )
        costs = self.state["costs"]
        if len(costs) < MAX_STORED_COSTS:
            costs.append(round(cost, 6))

        asset = str(row.get("asset") or "unknown")
        timeframe = str(row.get("timeframe") or "unknown")
        window = str(row.get("window") or "unknown")
        group_key = f"{asset}|{timeframe}|{window}"
        group = self.state["groups"].setdefault(group_key, {
            "asset": asset, "timeframe": timeframe, "window": window,
            "valid": 0, "opportunities": 0, "near_miss": 0, "min_cost": None,
        })
        group["valid"] += 1
        group["min_cost"] = cost if group["min_cost"] is None else min(group["min_cost"], cost)
        if cost < self.config.opportunity_threshold:
            group["opportunities"] += 1
        elif cost < 1.01:
            group["near_miss"] += 1

        if cost < self.config.opportunity_threshold and ts > 0:
            self._extend_run(row, ts, cost, asset, timeframe, window)

    def _extend_run(self, row, ts, cost, asset, timeframe, window):
        market_id = str(row.get("market_id") or "unknown")
        open_runs = self.state["open_runs"]
        run = open_runs.get(market_id)
        if run is not None and 0 <= ts - run["end_ts"] <= self.config.max_run_gap_seconds:
            run["end_ts"] = ts
            run["events"] += 1
            run["min_cost"] = min(run["min_cost"], cost)
            return
        if run is not None:
            self._close_run(run)
        open_runs[market_id] = {
            "market_id": market_id,
            "condition_id": row.get("condition_id"),
            "asset": asset,
            "timeframe": timeframe,
            "window": window,
            "start_ts": ts,
            "end_ts": ts,
            "events": 1,
            "min_cost": cost,
        }

    def _close_run(self, run):
        runs = self.state["runs"]
        runs.append(run)
        if len(runs) > MAX_COMPLETED_RUNS:
            del runs[: len(runs) - MAX_COMPLETED_RUNS]

    # ---------------------------------------------------------------- report

    def report(self):
        config = self.config
        costs = sorted(self.state["costs"])
        valid = self.state["valid"]

        buckets = []
        for threshold in COST_THRESHOLDS:
            count = sum(1 for cost in costs if cost < threshold)
            buckets.append({
                "below": threshold,
                "count": count,
                "share_of_valid": (count / valid) if valid else None,
            })
        tail = sum(1 for cost in costs if cost >= COST_THRESHOLDS[-1])

        opportunities = sum(1 for cost in costs if cost < config.opportunity_threshold)
        near_miss = sum(1 for cost in costs if config.opportunity_threshold <= cost < 1.01)

        # Close runs whose market went quiet beyond the gap tolerance.
        reference_ts = self.state["last_ts"] or 0
        runs = list(self.state["runs"])
        open_runs = []
        for run in self.state["open_runs"].values():
            if reference_ts and reference_ts - run["end_ts"] > config.max_run_gap_seconds:
                runs.append(run)
            else:
                open_runs.append(run)
        durations = [run["end_ts"] - run["start_ts"] for run in runs]
        run_rows = [
            {
                **run,
                "duration_seconds": round(run["end_ts"] - run["start_ts"], 3),
                "open": False,
            }
            for run in runs
        ] + [
            {
                **run,
                "duration_seconds": round(run["end_ts"] - run["start_ts"], 3),
                "open": True,
            }
            for run in open_runs
        ]
        run_rows.sort(key=lambda run: run["start_ts"])

        groups = sorted(
            self.state["groups"].values(),
            key=lambda group: (group["asset"], group["timeframe"], group["window"]),
        )

        excluded = self.state["excluded"]
        rejections = Counter(self.state["rejections"])
        first_ts, last_ts = self.state["first_ts"], self.state["last_ts"]
        valid_first, valid_last = self.state["valid_first_ts"], self.state["valid_last_ts"]
        return {
            "generated_at": time.time(),
            "strategy": "paired_lock",
            "sample_window": {
                "first_event_ts": first_ts,
                "last_event_ts": last_ts,
                "first_event_utc": _utc(first_ts),
                "last_event_utc": _utc(last_ts),
                "span_seconds": round(last_ts - first_ts, 3) if first_ts and last_ts else None,
                "valid_first_event_ts": valid_first,
                "valid_last_event_ts": valid_last,
                "valid_first_event_utc": _utc(valid_first),
                "valid_last_event_utc": _utc(valid_last),
            },
            "evaluation_base": {
                "paired_lock_shadow_eval_events": self.state["events_total"],
                "valid_evaluations": valid,
                "excluded": {
                    **excluded,
                    "total": sum(excluded.values()),
                },
                "duplicate_events": self.state["duplicate_events"],
                "future_events": self.state["future_events"],
                "invalid_json_lines": self.state["invalid_json"],
                "lines_read": self.state["lines_read"],
                "shadow_accepts_valid": self.state["accepts_valid"],
                "real_order_submissions": 0,
                "real_orders": 0,
            },
            "net_cost_per_share": {
                "samples": len(costs),
                "min": costs[0] if costs else None,
                "p1": percentile(costs, 0.01),
                "p5": percentile(costs, 0.05),
                "p25": percentile(costs, 0.25),
                "median": percentile(costs, 0.5),
                "p75": percentile(costs, 0.75),
                "p95": percentile(costs, 0.95),
            },
            "threshold_buckets": buckets,
            "at_or_above_max_threshold": {
                "gte": COST_THRESHOLDS[-1],
                "count": tail,
                "share_of_valid": (tail / valid) if valid else None,
            },
            "opportunities": {
                "threshold": config.opportunity_threshold,
                "count": opportunities,
                "share_of_valid": (opportunities / valid) if valid else None,
                "near_miss_1_0_to_1_01": near_miss,
                "note": (
                    "Only net_cost_per_share < threshold counts as a true "
                    "paired_lock opportunity (net cost below the $1 payout). "
                    "Values in [1.0, 1.01) are near-miss observations, NOT "
                    "opportunities."
                ),
            },
            "groups": groups,
            "opportunity_runs": {
                "completed": len(runs),
                "open": len(open_runs),
                "duration_seconds": {
                    "min": min(durations) if durations else None,
                    "median": percentile(durations, 0.5),
                    "max": max(durations) if durations else None,
                },
                "runs": run_rows,
            },
            "rejection_reasons_top": rejections.most_common(config.top_reasons),
            "config": {
                "min_seconds_to_close": config.min_seconds_to_close,
                "opportunity_threshold": config.opportunity_threshold,
                "max_run_gap_seconds": config.max_run_gap_seconds,
                "top_reasons": config.top_reasons,
            },
        }


def build_report(audit_path, config=None):
    """One-shot full read of the audit file."""
    accumulator = PairedOpportunityAccumulator(config=config)
    accumulator.consume_file(audit_path)
    return accumulator.report()


def _fmt(value, digits=6):
    return "N/A" if value is None else f"{value:.{digits}f}"


def _pct(value):
    return "N/A" if value is None else f"{value * 100:.4f}%"


def format_text(report, audit_path=None):
    base = report["evaluation_base"]
    window = report["sample_window"]
    cost = report["net_cost_per_share"]
    opp = report["opportunities"]
    lines = []
    lines.append("PAIRED_LOCK OPPORTUNITY REPORT (Shadow audit data only)")
    if audit_path:
        lines.append(f"audit_file: {audit_path}")
    lines.append(
        "sample_window: "
        f"{window['first_event_utc'] or 'N/A'} -> {window['last_event_utc'] or 'N/A'} "
        f"(span {_fmt(window['span_seconds'], 1)}s)"
    )
    lines.append(
        "evaluation_base: "
        f"paired_lock shadow_eval={base['paired_lock_shadow_eval_events']} "
        f"valid={base['valid_evaluations']} "
        f"excluded={base['excluded']['total']} "
        f"(closing_window={base['excluded']['closing_window']}, "
        f"empty_book={base['excluded']['empty_book']}, "
        f"incomplete_data={base['excluded']['incomplete_data']}) "
        f"duplicates={base['duplicate_events']} "
        f"invalid_json_lines={base['invalid_json_lines']} "
        f"shadow_accepts_valid={base['shadow_accepts_valid']} "
        f"real_orders=0 real_submissions=0"
    )
    lines.append("")
    lines.append(f"NET COST PER SHARE (valid evaluations, n={cost['samples']})")
    for key in ("min", "p1", "p5", "p25", "median", "p75", "p95"):
        lines.append(f"  {key:>7}: {_fmt(cost[key])}")
    lines.append("")
    lines.append("THRESHOLD BUCKETS (cumulative, per-share net cost)")
    for bucket in report["threshold_buckets"]:
        marker = "  <-- TRUE OPPORTUNITY LINE" if bucket["below"] == opp["threshold"] else ""
        lines.append(
            f"  < {bucket['below']:<6}: {bucket['count']:>8} "
            f"({_pct(bucket['share_of_valid'])}){marker}"
        )
    tail = report["at_or_above_max_threshold"]
    lines.append(f"  >= {tail['gte']:<5}: {tail['count']:>8} ({_pct(tail['share_of_valid'])})")
    lines.append("")
    lines.append(
        f"OPPORTUNITIES (net_cost_per_share < {opp['threshold']}): {opp['count']} "
        f"({_pct(opp['share_of_valid'])} of valid)"
    )
    lines.append(
        f"NEAR-MISS (1.0 <= cost < 1.01, NOT opportunities): {opp['near_miss_1_0_to_1_01']}"
    )
    lines.append("")
    lines.append("GROUPS (asset/timeframe/window)")
    if report["groups"]:
        for group in report["groups"]:
            lines.append(
                f"  {group['asset']:<5} {group['timeframe']:<4} {group['window']:<8} "
                f"valid={group['valid']:<7} opportunities={group['opportunities']:<5} "
                f"near_miss={group['near_miss']:<5} min_cost={_fmt(group['min_cost'])}"
            )
    else:
        lines.append("  (none)")
    lines.append("")
    runs = report["opportunity_runs"]
    duration = runs["duration_seconds"]
    lines.append(
        f"OPPORTUNITY RUNS: completed={runs['completed']} open={runs['open']} "
        f"duration min={_fmt(duration['min'], 1)}s median={_fmt(duration['median'], 1)}s "
        f"max={_fmt(duration['max'], 1)}s"
    )
    for run in runs["runs"][:20]:
        state = "OPEN" if run["open"] else "DONE"
        lines.append(
            f"  [{state}] {run['asset']} {run['timeframe']} {run['window']} "
            f"market={run['market_id'][:12]}... events={run['events']} "
            f"duration={_fmt(run['duration_seconds'], 1)}s min_cost={_fmt(run['min_cost'])} "
            f"start={_utc(run['start_ts'])}"
        )
    lines.append("")
    lines.append("REJECTION REASONS (top)")
    if report["rejection_reasons_top"]:
        for reason, count in report["rejection_reasons_top"]:
            lines.append(f"  {count:>8} {reason}")
    else:
        lines.append("  (none)")
    return "\n".join(lines)


def run_report(
    audit_path,
    config=None,
    watch=False,
    state_path=None,
    json_output=None,
):
    """CLI entry: stream audit data, print text, optionally write JSON/state."""
    config = config or PairedReportConfig()
    if watch:
        if not state_path:
            raise SystemExit("--watch requires --report-state")
        accumulator = PairedOpportunityAccumulator.load(state_path, config=config)
        accumulator.consume_file(audit_path)
        accumulator.save(state_path)
    else:
        accumulator = PairedOpportunityAccumulator(config=config)
        accumulator.consume_file(audit_path)
    report = accumulator.report()
    report["audit_file"] = str(audit_path)
    if watch:
        report["watch"] = {
            "state_file": str(state_path),
            "offset": accumulator.state["file"]["offset"],
        }
    print(format_text(report, audit_path=audit_path))
    if json_output:
        output_path = Path(json_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_suffix(output_path.suffix + ".tmp")
        temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
        os.replace(temporary, output_path)
        print(f"WROTE {output_path}")
    return 0
