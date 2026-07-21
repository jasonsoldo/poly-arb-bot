import json
import inspect
import mimetypes
import os
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .shadow_report import IncrementalReport
from .ev_shadow import directional_ev_enabled, lottery_ev_enabled
from .maker_shadow import maker_accumulate_enabled
from .reference_layer import reference_source_maximum_age_ms, reference_state_for_asset
from .strategy_config import StrategyConfig


_REPORT_CACHE = {}
_REPORT_ANALYTICS = {}
_REPORT_LOCK = threading.Lock()
_REPORT_JOBS = {}
_REPORT_JOB_LOCK = threading.Lock()
REPORT_ASYNC_THRESHOLD_BYTES = 10 * 1024 * 1024
_STRATEGY_COUNT_CACHE = {}
_STRATEGY_COUNT_LOCK = threading.Lock()
_STRATEGY_COUNT_JOBS = {}
_STRATEGY_COUNT_RESULTS = {}
_STRATEGY_JOB_LOCK = threading.Lock()
STRATEGY_ASYNC_THRESHOLD_BYTES = 10 * 1024 * 1024
ASSETS = ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE")
INTERVALS = ("5m", "15m", "1h", "4h")
PRIMARY_STRATEGIES = (
    "late_window_directional_ev",
    "low_price_lottery_ev",
    "paired_lock",
)
# 4th independent shadow strategy (maker_paired_accumulate); episode
# open/reject decisions are produced by poly_arb_bot.maker_shadow.
MAKER_ACCUMULATE_STRATEGIES = ("maker_paired_accumulate",)
# Focused strategy surface: only paired_lock + maker_paired_accumulate are
# tracked. split_sell_lock / maker_complete_set_arb / microstructure_reversion
# observers are env-disabled in the C++ engine (default off) and no longer
# surfaced on the dashboard.
STRATEGIES = (PRIMARY_STRATEGIES + MAKER_ACCUMULATE_STRATEGIES)
# maker_paired_accumulate episode open/reject decisions counted as
# evaluations (one per state-machine decision, deduped by event_id).
MAKER_ACCUMULATE_DECISION_EVENTS = frozenset({
    "maker_episode_opened",
    "maker_episode_rejected",
})


def _report_cache_key(path, execution_path=None):
    stat = path.stat()
    execution_stat = execution_path.stat() if execution_path and execution_path.exists() else None
    return (stat.st_size, stat.st_mtime_ns,
            execution_stat.st_size if execution_stat else 0,
            execution_stat.st_mtime_ns if execution_stat else 0)


def _analytics_state_path(path, prefix):
    root = path.parent.parent if path.parent.name == "logs" else path.parent
    return root / "state" / f"{prefix}-{path.name}.json"


def _cached_report(path, execution_path=None, current_complete_set_hashes=None):
    if not path.exists():
        return build_report_empty()
    with _REPORT_LOCK:
        analytics = _REPORT_ANALYTICS.get(str(path))
        if analytics is None:
            analytics = IncrementalReport(
                path, execution_path, _analytics_state_path(path, "web-shadow-report"),
            )
            _REPORT_ANALYTICS[str(path)] = analytics
        refresh_parameters = inspect.signature(analytics.refresh).parameters
        report = (
            analytics.refresh(current_complete_set_hashes)
            if refresh_parameters else analytics.refresh()
        )
        key = _report_cache_key(path, execution_path)
        _REPORT_CACHE[str(path)] = (key, report, time.monotonic())
        return report


def _report_worker(job_key, path, execution_path, current_complete_set_hashes):
    try:
        _cached_report(path, execution_path, current_complete_set_hashes)
    finally:
        with _REPORT_JOB_LOCK:
            _REPORT_JOBS.pop(job_key, None)


def _report_for_status(path, execution_path=None, current_complete_set_hashes=None):
    if not path.exists():
        return build_report_empty(), False
    job_key = (str(path.resolve()), str(execution_path.resolve()) if execution_path else "")
    current_key = _report_cache_key(path, execution_path)
    cached = _REPORT_CACHE.get(str(path))
    cache_fresh = bool(cached and cached[0] == current_key)
    total_size = path.stat().st_size
    if execution_path and execution_path.exists():
        total_size += execution_path.stat().st_size
    with _REPORT_JOB_LOCK:
        job = _REPORT_JOBS.get(job_key)
        if job and job.is_alive():
            return cached[1] if cached else build_report_empty(), True
        persisted = _analytics_state_path(path, "web-shadow-report").exists()
        if not cache_fresh and not persisted and total_size >= REPORT_ASYNC_THRESHOLD_BYTES:
            job = threading.Thread(
                target=_report_worker,
                args=(job_key, path, execution_path, current_complete_set_hashes),
                daemon=True,
            )
            _REPORT_JOBS[job_key] = job
            job.start()
            return cached[1] if cached else build_report_empty(), True
    return _cached_report(path, execution_path, current_complete_set_hashes), False


def build_report_empty():
    empty_performance = {"completed": 0, "wins": 0, "losses": 0, "simulated_pnl": None,
                         "win_rate": None, "sharpe": None, "sharpe_samples": 0}
    return {
        "markets_seen": 0, "evaluations": 0, "fok_passed": 0, "accepts": 0,
        "invalid_json": 0, "future_events": 0, "duplicate_events": 0,
        "accepted_evaluations": 0, "rejected_evaluations": 0,
        "rejection_reasons": {},
        "opportunity_duration_ms": {"p50": None, "p95": None, "max": None},
        "source_age_ms": {"latest": None, "p50": None, "p95": None, "p99": None,
                          "max": None, "samples": 0},
        "performance": dict(empty_performance),
        "performance_by_strategy": {
            strategy: dict(empty_performance) for strategy in STRATEGIES
        },
        "equity_curve": [], "trade_ledger": [], "asset_latest_pnl": {},
    }


def _json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (OSError, ValueError):
        return default


def _jsonl(path, limit=100):
    if not path.exists():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            chunks = []
            newlines = 0
            while position > 0 and newlines <= limit:
                size = min(65536, position)
                position -= size
                handle.seek(position)
                chunk = handle.read(size)
                chunks.append(chunk)
                newlines += chunk.count(b"\n")
        lines = b"".join(reversed(chunks)).decode("utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return list(reversed(rows))


def _empty_strategy_counts():
    counts = {
        name: {"evaluations": 0, "accepts": 0, "rejections": 0,
               "model_evaluations": 0, "latest_model_evaluated": False,
               "unique_opportunities": 0, "active_opportunities": 0}
        for name in STRATEGIES
    }
    return counts


def _new_strategy_state():
    return {
        "identity": None, "offset": 0, "size": 0, "seen": set(),
        "last_saved": 0.0,
        "counts": _empty_strategy_counts(),
        "active": {name: set() for name in _empty_strategy_counts()},
    }


def _load_strategy_state(path):
    summary_path = _analytics_state_path(path, "web-strategy-counts")
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _new_strategy_state()
    state = _new_strategy_state()
    state.update({
        key: payload.get(key, state[key])
        for key in ("identity", "offset", "size")
    })
    state["seen"] = set(payload.get("seen", []))
    for name in state["counts"]:
        state["counts"][name].update(payload.get("counts", {}).get(name, {}))
        state["active"][name] = {
            tuple(item) for item in payload.get("active", {}).get(name, [])
        }
    return state


def _probability_calibration_view(raw):
    result = {}
    for strategy in ("late_window_directional_ev", "low_price_lottery_ev"):
        source = raw.get(strategy, {})
        samples = int(source.get("samples", 0))
        buckets = {}
        for name, values in source.get("calibration_buckets", {}).items():
            count = int(values.get("samples", 0))
            buckets[name] = {
                "samples": count,
                "expected_up_rate": (
                    float(values.get("sum_probability", 0)) / count if count else None
                ),
                "realized_up_rate": (
                    float(values.get("actual_up", 0)) / count if count else None
                ),
            }
        result[strategy] = {
            "samples": samples,
            "expected_up_rate": (
                float(source.get("sum_expected_up_probability", 0)) / samples
                if samples else None
            ),
            "realized_up_rate": (
                float(source.get("sum_actual_up", 0)) / samples if samples else None
            ),
            "brier_score": (
                float(source.get("sum_brier_score", 0)) / samples if samples else None
            ),
            "log_loss": (
                float(source.get("sum_log_loss", 0)) / samples if samples else None
            ),
            "origin_accepted": int(source.get("origin_accepted", 0)),
            "origin_rejected": int(source.get("origin_rejected", 0)),
            "calibration_buckets": buckets,
        }
    return result


def _save_strategy_state(path, state):
    now = time.monotonic()
    if state["last_saved"] and now - state["last_saved"] < 30:
        return
    summary_path = _analytics_state_path(path, "web-strategy-counts")
    temporary = summary_path.with_suffix(summary_path.suffix + ".tmp")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "identity": state["identity"], "offset": state["offset"], "size": state["size"],
        "seen": list(state["seen"])[-50_000:], "counts": state["counts"],
        "active": {name: [list(item) for item in rows]
                   for name, rows in state["active"].items()},
    }
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, summary_path)
    state["last_saved"] = now


def _strategy_counts(paths):
    names = STRATEGIES
    total = _empty_strategy_counts()
    with _STRATEGY_COUNT_LOCK:
        for path in paths:
            key = str(path.resolve())
            state = _STRATEGY_COUNT_CACHE.setdefault(key, _load_strategy_state(path))
            try:
                stat = path.stat()
                size = stat.st_size
                identity = f"{stat.st_dev}:{stat.st_ino}"
            except OSError:
                size = 0
                identity = None
            changed = False
            if identity != state.get("identity") or size < state["offset"]:
                state["identity"] = identity
                state["offset"] = 0
                changed = True
            if size > state["offset"]:
                try:
                    with path.open("rb") as handle:
                        handle.seek(state["offset"])
                        while True:
                            line_start = handle.tell()
                            line = handle.readline()
                            if not line:
                                break
                            if not line.endswith(b"\n"):
                                try:
                                    row = json.loads(line)
                                except ValueError:
                                    handle.seek(line_start)
                                    break
                            elif not line.strip():
                                continue
                            else:
                                try:
                                    row = json.loads(line)
                                except ValueError:
                                    continue
                            strategy = row.get("strategy", "paired_lock")
                            event_type = row.get("event_type")
                            if strategy not in state["counts"] or event_type not in {
                                "shadow_eval",
                                *MAKER_ACCUMULATE_DECISION_EVENTS,
                            }:
                                continue
                            event_id = row.get("event_id")
                            if event_id and event_id in state["seen"]:
                                continue
                            if event_id:
                                state["seen"].add(event_id)
                            bucket = state["counts"][strategy]
                            bucket["evaluations"] += 1
                            accepted = row.get("decision") == "ACCEPT"
                            bucket["accepts"] += int(accepted)
                            bucket["rejections"] += int(not accepted)
                            opportunity_key = (row.get("market_id"), row.get("outcome", "paired"))
                            if accepted:
                                if opportunity_key not in state["active"][strategy]:
                                    bucket["unique_opportunities"] = bucket.get("unique_opportunities", 0) + 1
                                state["active"][strategy].add(opportunity_key)
                            else:
                                state["active"][strategy].discard(opportunity_key)
                            bucket["model_evaluations"] += int(
                                strategy != "paired_lock" and row.get("estimated_probability") is not None
                            )
                            if strategy != "paired_lock":
                                bucket["latest_model_evaluated"] = row.get("estimated_probability") is not None
                        offset = handle.tell()
                        if offset != state["offset"]:
                            state["offset"] = offset
                            changed = True
                except OSError:
                    pass
            state["size"] = size
            if len(state["seen"]) > 50_000:
                state["seen"] = set(list(state["seen"])[-50_000:])
            if changed:
                _save_strategy_state(path, state)
            for name in names:
                for field in ("evaluations", "accepts", "rejections", "model_evaluations"):
                    total[name][field] += state["counts"][name][field]
                total[name]["unique_opportunities"] += state["counts"][name].get("unique_opportunities", 0)
                total[name]["active_opportunities"] += len(state["active"][name])
                total[name]["latest_model_evaluated"] = state["counts"][name]["latest_model_evaluated"]
    return total


def _strategy_counts_worker(key, paths):
    try:
        result = _strategy_counts(paths)
        with _STRATEGY_JOB_LOCK:
            _STRATEGY_COUNT_RESULTS[key] = result
    finally:
        with _STRATEGY_JOB_LOCK:
            _STRATEGY_COUNT_JOBS.pop(key, None)


def _strategy_counts_for_status(paths):
    paths = tuple(paths)
    key = tuple(str(path.resolve()) for path in paths)
    size = sum(path.stat().st_size for path in paths if path.exists())
    initialized = all(
        not path.exists() or str(path.resolve()) in _STRATEGY_COUNT_CACHE or
        _analytics_state_path(path, "web-strategy-counts").exists()
        for path in paths
    )
    with _STRATEGY_JOB_LOCK:
        job = _STRATEGY_COUNT_JOBS.get(key)
        if job and job.is_alive():
            return _STRATEGY_COUNT_RESULTS.get(key, _empty_strategy_counts()), True
        if not initialized and size >= STRATEGY_ASYNC_THRESHOLD_BYTES:
            job = threading.Thread(target=_strategy_counts_worker, args=(key, paths), daemon=True)
            _STRATEGY_COUNT_JOBS[key] = job
            job.start()
            return _STRATEGY_COUNT_RESULTS.get(key, _empty_strategy_counts()), True
    result = _strategy_counts(paths)
    with _STRATEGY_JOB_LOCK:
        _STRATEGY_COUNT_RESULTS[key] = result
    return result, False


def _signal_block_reason(signal, config):
    if not signal.get("settlement_source_ok", False):
        return "settlement_source"
    if signal.get("orderbook_age_ms", config.stale_orderbook_ms + 1) > config.stale_orderbook_ms:
        return "stale_orderbook"
    seconds = signal.get("seconds_to_close", 0)
    if not config.min_seconds_to_close <= seconds <= config.max_seconds_to_close:
        return "time_window"
    if signal.get("model_probability", 0) - signal.get("expected_fill_price", 1) <= config.min_edge:
        return "model_edge"
    if signal.get("expected_fill_price", 1) > signal.get("max_allowed_price", 0.99):
        return "max_price"
    if signal.get("liquidity", 0) < config.min_liquidity:
        return "liquidity"
    if abs(signal.get("expected_fill_price", 1) - signal.get("market_price", 0)) > config.max_slippage:
        return "slippage"
    return None


def _strategy_score(event):
    blockers = {"books_not_synced", "up_depth", "down_depth", "fee_schedule_unavailable",
                "net_cost_above_threshold", "execution_value_below_threshold", "closing_window"}
    if not event:
        return {"total": 0, "blocked": True, "components": {}, "metrics": {}, "checks": {}}
    size = max(float(event.get("size", 0)), 1e-9)
    expected_value = event.get("expected_execution_value")
    eev = max(0.0, min(1.0, float(expected_value or 0) / 0.01))
    depth = max(0.0, min(1.0, min(float(event.get("up_fill", 0)), float(event.get("down_fill", 0))) / size))
    freshness = max(0.0, min(1.0, 1 - float(event.get("source_age_ms", 1000)) / 1000))
    book_skew_ms = abs(float(event.get("up_book_age_ms", 500)) - float(event.get("down_book_age_ms", 0)))
    skew = max(0.0, min(1.0, 1 - book_skew_ms / 500))
    leg_risk = max(0.0, min(1.0, float(event.get("leg_1_fill_probability", 0)) *
                            float(event.get("leg_2_fill_probability", 0)) *
                            (1 - min(1.0, float(event.get("orphan_leg_loss", 1))))))
    components = {"eev": eev, "depth": depth, "freshness": freshness, "book_skew": skew, "leg_risk": leg_risk}
    total = 100 * (0.35 * eev + 0.20 * depth + 0.15 * freshness + 0.15 * skew + 0.15 * leg_risk)
    blocked = event.get("reason") in blockers or event.get("decision") != "ACCEPT"
    checks = {
        "depth": "PASS" if event.get("fok") and depth >= 1 else "FAIL",
        "freshness": "PASS" if event.get("source_age_ms") is not None and freshness > 0 else "FAIL",
        "book_sync": "PASS" if event.get("books_synced") is True else "FAIL",
        "leg_risk": "PASS" if event.get("leg_1_fill_probability") is not None and event.get("leg_2_fill_probability") is not None else "N/A",
        "net_cost": "PASS" if float(event.get("locked_profit", 0)) > 0 else "FAIL",
    }
    metrics = {"expected_execution_value": expected_value, "depth_ratio": depth,
               "source_age_ms": event.get("source_age_ms"), "book_skew_ms": book_skew_ms,
               "leg_1_fill_probability": event.get("leg_1_fill_probability"),
               "leg_2_fill_probability": event.get("leg_2_fill_probability")}
    return {"total": 0 if blocked else round(total, 2), "blocked": blocked,
            "components": components, "metrics": metrics, "checks": checks}


# ---------------------------------------------------------------------------
# maker_paired_accumulate aggregation (4th strategy web panel)
# ---------------------------------------------------------------------------
_MAKER_STRATEGY = "maker_paired_accumulate"
_MAKER_ACTIVE_STATES = frozenset({
    "LEG1_WORKING", "LEG1_FILLED", "LEG2_WORKING",
    "HEDGING_DIRECTIONAL_EXIT", "EMERGENCY_FLATTEN",
})
_MAKER_STATE_ORDER = (
    "LEG1_WORKING", "LEG1_FILLED", "LEG2_WORKING",
    "HEDGING_DIRECTIONAL_EXIT", "EMERGENCY_FLATTEN",
    "COMPLETE", "LEG1_CANCELLED", "CLOSED_WITH_LOSS",
)
_MAKER_COST_CHAIN_FIELDS = (
    "event_type", "episode_id", "market_id", "condition_id", "asset",
    "timeframe", "window", "ts", "decision", "reason", "exit_path",
    "leg1_outcome", "leg2_outcome", "leg1_avg_price", "leg2_avg_price",
    "leg2_max_price", "leg1_filled_size", "leg2_filled_size",
    "gross_cost", "maker_fees", "hedge_taker_fee", "exit_taker_fee",
    "fee_rate", "fee_formula_version", "gas_cost_per_share",
    "buffer_per_share", "net_cost", "guaranteed_payout", "locked_profit",
    "locked_roi", "locked_size", "at_risk_size", "min_realized_margin",
    "estimated_rebate", "estimated_rebate_label",
    "estimated_liquidity_reward", "estimated_liquidity_reward_label",
    "realized_rebate", "exit_vwap", "orphan_seconds",
    "orphan_max_drawdown", "episode_realized_pnl", "seconds_to_close",
)


def _maker_episode_default(item):
    return {
        "episode_id": item.get("episode_id"),
        "market_id": item.get("market_id"),
        "condition_id": item.get("condition_id"),
        "asset": item.get("asset"),
        "timeframe": item.get("timeframe"),
        "state": None,
        "opened_ts": None,
        "last_ts": item.get("ts"),
        "seconds_to_close": item.get("seconds_to_close"),
        "leg1_outcome": None,
        "leg1_quote_price": None,
        "leg1_order_size": None,
        "leg1_avg_price": None,
        "leg1_filled_size": 0.0,
        "leg1_fill_ts": None,
        "leg2_quote_price": None,
        "leg2_max_price": None,
        "leg2_avg_price": None,
        "leg2_filled_size": 0.0,
        "locked_size": None,
        "at_risk_size": None,
        "expected_margin": None,
        "improve_attempt": None,
        "max_improves": None,
        "terminal": False,
        "terminal_reason": None,
        "exit_path": None,
    }


def _maker_accumulate_view(events, strategy_counts, session_strategy_counts,
                           strategy_recent, maker_state, now):
    """Aggregate the maker_paired_accumulate web panel purely from canonical
    audit events (strategy-audit.jsonl tail already loaded into ``events``)
    plus the maker-shadow bridge state file. Nothing is fabricated: every
    value traces to a maker_* audit event or to machine statistics persisted
    by poly_arb_bot.maker_shadow."""
    maker_events = [
        item for item in events if item.get("strategy") == _MAKER_STRATEGY
    ]
    episodes = {}
    fill_stats = {
        "samples": 0, "strict_would_fill": 0, "queue_would_fill": 0,
        "modes": {}, "shadow_fill_mode": None,
    }
    latest_terminal = None
    for item in sorted(maker_events, key=lambda row: float(row.get("ts", 0))):
        event_type = item.get("event_type")
        if item.get("shadow_fill_mode"):
            fill_stats["shadow_fill_mode"] = item["shadow_fill_mode"]
        episode_id = item.get("episode_id")
        episode = None
        if episode_id:
            episode = episodes.get(episode_id)
            if episode is None:
                episode = _maker_episode_default(item)
                episodes[episode_id] = episode
        if event_type == "maker_episode_opened" and episode is not None:
            episode.update({
                "state": item.get("state_to") or "LEG1_WORKING",
                "opened_ts": item.get("ts"),
                "leg1_outcome": item.get("leg1_outcome"),
                "leg1_quote_price": item.get("leg1_quote_price"),
                "leg1_order_size": item.get("leg1_order_size"),
                "expected_margin": item.get("expected_margin"),
            })
        elif event_type == "maker_leg_filled":
            strict = item.get("strict_would_fill")
            queue = item.get("queue_would_fill")
            if strict is not None or queue is not None:
                fill_stats["samples"] += 1
                fill_stats["strict_would_fill"] += int(strict is True)
                fill_stats["queue_would_fill"] += int(queue is True)
            mode = item.get("fill_mode") or "unknown"
            fill_stats["modes"][mode] = fill_stats["modes"].get(mode, 0) + 1
            if episode is not None:
                if item.get("leg") == 1 and item.get("side") != "sell":
                    if item.get("leg1_avg_price") is not None:
                        episode["leg1_avg_price"] = item["leg1_avg_price"]
                    if item.get("leg1_filled_size") is not None:
                        episode["leg1_filled_size"] = item["leg1_filled_size"]
                    if episode["leg1_fill_ts"] is None:
                        episode["leg1_fill_ts"] = item.get("ts")
                elif item.get("leg") == 2:
                    if item.get("leg2_avg_price") is not None:
                        episode["leg2_avg_price"] = item["leg2_avg_price"]
                    if item.get("leg2_filled_size") is not None:
                        episode["leg2_filled_size"] = item["leg2_filled_size"]
                    if item.get("leg2_max_price") is not None:
                        episode["leg2_max_price"] = item["leg2_max_price"]
        elif event_type == "maker_quote_updated" and episode is not None:
            if item.get("leg") == 2:
                episode["leg2_quote_price"] = item.get("new_quote_price")
                if item.get("leg2_max_price") is not None:
                    episode["leg2_max_price"] = item["leg2_max_price"]
                episode["improve_attempt"] = item.get("improve_attempt")
                episode["max_improves"] = item.get("max_improves")
        elif event_type == "maker_episode_state_change" and episode is not None:
            episode["state"] = item.get("state_to")
            if item.get("locked_size") is not None:
                episode["locked_size"] = item["locked_size"]
            if item.get("at_risk_size") is not None:
                episode["at_risk_size"] = item["at_risk_size"]
        elif event_type == "maker_leg1_cancelled" and episode is not None:
            episode["state"] = "LEG1_CANCELLED"
            episode["terminal"] = True
            episode["terminal_reason"] = item.get("reason")
        elif event_type in ("maker_episode_completed",
                            "maker_episode_closed_with_loss"):
            latest_terminal = item
            if episode is not None:
                episode["state"] = (
                    "COMPLETE" if event_type == "maker_episode_completed"
                    else "CLOSED_WITH_LOSS"
                )
                episode["terminal"] = True
                episode["terminal_reason"] = item.get("reason")
                episode["exit_path"] = item.get("exit_path")
        if episode is not None:
            episode["last_ts"] = item.get("ts")
            if item.get("seconds_to_close") is not None:
                episode["seconds_to_close"] = item["seconds_to_close"]

    state_counts = {name: 0 for name in _MAKER_STATE_ORDER}
    for episode in episodes.values():
        if episode["state"] in state_counts:
            state_counts[episode["state"]] += 1

    active_episodes = []
    for episode in episodes.values():
        if episode["terminal"] or episode["state"] not in _MAKER_ACTIVE_STATES:
            continue
        leg1_avg = episode["leg1_avg_price"]
        at_risk_size = episode["at_risk_size"]
        if at_risk_size is None and episode["leg1_filled_size"]:
            at_risk_size = max(
                0.0, episode["leg1_filled_size"] - episode["leg2_filled_size"])
        orphan_seconds = None
        if episode["leg1_fill_ts"] is not None:
            orphan_seconds = max(0.0, now - float(episode["leg1_fill_ts"]))
        active_episodes.append({
            "episode_id": episode["episode_id"],
            "episode_short": str(episode["episode_id"] or "")[-8:],
            "market_id": episode["market_id"],
            "asset": episode["asset"],
            "timeframe": episode["timeframe"],
            "state": episode["state"],
            "leg1_outcome": episode["leg1_outcome"],
            "leg1_quote_price": episode["leg1_quote_price"],
            "leg1_avg_price": leg1_avg,
            "leg1_filled_size": episode["leg1_filled_size"],
            "leg2_quote_price": episode["leg2_quote_price"],
            "leg2_max_price": episode["leg2_max_price"],
            "orphan_seconds": orphan_seconds,
            "at_risk_size": at_risk_size,
            "at_risk_usd": (
                at_risk_size * leg1_avg
                if at_risk_size is not None and leg1_avg is not None else None
            ),
            "seconds_to_close": episode["seconds_to_close"],
            "expected_margin": episode["expected_margin"],
            "improve_attempt": episode["improve_attempt"],
            "max_improves": episode["max_improves"],
        })
    active_episodes.sort(key=lambda row: float(row.get("seconds_to_close") or 0))

    # Event-window exposure fallback (used only when bridge statistics are
    # missing, e.g. an older maker-shadow state file).
    window_total = 0.0
    window_at_risk = 0.0
    for episode in episodes.values():
        if episode["terminal"] or episode["state"] not in _MAKER_ACTIVE_STATES:
            continue
        leg1_avg = float(episode["leg1_avg_price"] or 0.0)
        leg2_avg = float(episode["leg2_avg_price"] or 0.0)
        window_total += (episode["leg1_filled_size"] * leg1_avg
                         + episode["leg2_filled_size"] * leg2_avg)
        if episode["state"] == "LEG1_WORKING" and episode["leg1_order_size"]:
            window_total += max(
                0.0,
                (episode["leg1_order_size"] - episode["leg1_filled_size"])
                * float(episode["leg1_quote_price"] or 0.0),
            )
        window_at_risk += max(
            0.0, episode["leg1_filled_size"] - episode["leg2_filled_size"]
        ) * leg1_avg

    statistics = maker_state.get("statistics") or {}
    limits = statistics.get("limits") or {}
    decisions = strategy_counts.get(_MAKER_STRATEGY, {})
    session_decisions = session_strategy_counts.get(_MAKER_STRATEGY, {})
    recent = strategy_recent.get(_MAKER_STRATEGY, {})
    top_reasons = sorted(
        (recent.get("rejection_reasons") or {}).items(),
        key=lambda row: (-row[1], row[0]),
    )[:4]
    available = bool(
        statistics or episodes or decisions.get("evaluations")
        or latest_terminal is not None
    )
    return {
        "available": available,
        "state_updated_at": maker_state.get("updated_at"),
        "state_age_seconds": (
            max(0.0, now - float(maker_state["updated_at"]))
            if maker_state.get("updated_at") is not None else None
        ),
        "statistics": statistics or None,
        "state_counts": state_counts,
        "episodes_in_window": len(episodes),
        "active_episodes": active_episodes[:8],
        "cost_chain": (
            {key: latest_terminal.get(key) for key in _MAKER_COST_CHAIN_FIELDS}
            if latest_terminal is not None else None
        ),
        "fill_modes": fill_stats,
        "decisions": {
            "evaluations": decisions.get("evaluations", 0),
            "accepts": decisions.get("accepts", 0),
            "rejections": decisions.get("rejections", 0),
            "session_evaluations": session_decisions.get("evaluations", 0),
            "session_accepts": session_decisions.get("accepts", 0),
            "session_rejections": session_decisions.get("rejections", 0),
        },
        "top_reject_reasons": [
            {"reason": reason, "count": count} for reason, count in top_reasons
        ],
        "exposure": {
            "total": statistics.get("active_total_exposure", window_total),
            "at_risk": statistics.get("active_at_risk_exposure", window_at_risk),
            "daily_loss": statistics.get("daily_loss"),
            "consecutive_orphans": statistics.get("consecutive_orphans"),
            "circuit_breaker_open": statistics.get("circuit_breaker_open"),
            "limits": limits,
        },
        "semantics": "SHADOW_ONLY_NOT_ORDERS_OR_REAL_PNL",
    }


def _dynamic_position_issues(position):
    issues = []
    if position.get("sizing_mode") != "real_market_dynamic_v1":
        issues.append("sizing_mode")

    def positive(field):
        try:
            value = float(position.get(field))
            return value > 0 and value == value and abs(value) != float("inf")
        except (TypeError, ValueError):
            return False

    for field in (
        "target_size", "market_minimum_size", "entry_cost",
        "dynamic_maximum_loss", "capital_budget_usd",
    ):
        if not positive(field):
            issues.append(field)
    if positive("target_size") and positive("market_minimum_size"):
        if float(position["target_size"]) + 1e-9 < float(position["market_minimum_size"]):
            issues.append("target_size_below_market_minimum")
    if not position.get("size_binding_constraint"):
        issues.append("size_binding_constraint")
    return issues


def build_status(data_dir, log_file, state_file):
    snapshot = _json(data_dir / "live_snapshot.json", {"signals": [], "positions": []})
    markets = _json(data_dir / "live_markets.json", {"markets": []}).get("markets", [])
    market_ids = {item.get("market_id") for item in markets}
    signals = [item for item in snapshot.get("signals", []) if item.get("market_id") in market_ids]
    shadow_log = data_dir.parent / "logs" / "shadow-audit.jsonl"
    selected_log = shadow_log if shadow_log.exists() else log_file
    events = _jsonl(selected_log, limit=1000)
    strategy_log = data_dir.parent / "logs" / "strategy-audit.jsonl"
    events.extend(_jsonl(strategy_log, limit=1000))
    events.sort(key=lambda item: float(item.get("ts", 0)), reverse=True)
    events = [item for item in events if float(item.get("ts", 0)) <= time.time() + 300]
    execution_log = data_dir.parent / "logs" / "shadow-execution.jsonl"
    shadow_health_path = data_dir / "shadow-health.json"
    shadow_health = _json(shadow_health_path, {})
    current_complete_set_hashes = {
        "paired_lock": shadow_health.get("paired_config_hash"),
    }
    if not any(current_complete_set_hashes.values()):
        current_complete_set_hashes = None
    report, report_refreshing = _report_for_status(
        selected_log, execution_log, current_complete_set_hashes,
    )
    shadow_events = [item for item in events if item.get("event_type") in {
        "shadow_eval", "shadow_opportunity",
        "maker_episode_opened", "maker_episode_rejected",
        "maker_episode_completed", "maker_episode_closed_with_loss",
        "maker_leg_filled", "maker_leg1_cancelled",
    }]
    paired_events = [item for item in shadow_events if item.get("strategy", "paired_lock") == "paired_lock"]
    latest_shadow = {}
    for item in paired_events:
        if item.get("market_id") in market_ids:
            latest_shadow.setdefault(item.get("market_id"), item)
    state = _json(state_file, {"client_order_ids": {}})
    shadow_execution = _json(
        data_dir.parent / "state" / "shadow-execution.json",
        {"state": "IDLE", "processed": [], "audit_offset": 0,
         "real_order_submissions": None, "real_orders": None, "real_fills": None},
    )
    for field in ("real_order_submissions", "real_orders", "real_fills"):
        shadow_execution.setdefault(field, None)
    lifecycle_state = _json(
        data_dir.parent / "state" / "strategy-shadow.json",
        {"positions": {}, "completed": []},
    )
    maker_shadow_state = _json(
        data_dir.parent / "state" / "maker-shadow.json",
        {},
    )
    reference_prices = _json(data_dir / "venue-status.json", {})
    reference_age_ms = time.time() * 1000 - reference_prices.get("updated_at_ms", 0)
    reference_prices["stale"] = reference_age_ms > 10_000
    # Display layer must use each source's own freshness limit (strategy layer:
    # REFERENCE_MAX_AGE_MS default 3s, coinbase 10s, kraken 60s). Prefer the
    # per-source limit emitted by the engine; fall back to the local table.
    display_default_max_age_ms = float(os.getenv("REFERENCE_MAX_AGE_MS", "3000"))

    def _source_display_limit_ms(name, row):
        emitted = row.get("freshness_limit_ms")
        if emitted:
            return float(emitted)
        return reference_source_maximum_age_ms(name, display_default_max_age_ms)

    for asset in reference_prices.get("assets", {}).values():
        file_age = max(reference_age_ms, 0)
        for source in ("binance", "chainlink"):
            source_age = asset.get(f"{source}_source_age_ms", -1)
            source_limit_ms = reference_source_maximum_age_ms(source, display_default_max_age_ms)
            status_key = f"{source}_status"
            reported = asset.get(status_key)
            if asset.get("supported") is False:
                status = "UNSUPPORTED"
            elif reported in {"FRESH", "STALE", "DISCONNECTED", "NOT_RECEIVED", "UNSUPPORTED", "OUTLIER"}:
                status = "STALE" if reported == "FRESH" and (reference_age_ms > 10_000 or source_age + file_age > source_limit_ms) else reported
            else:
                status = "FRESH" if source_age >= 0 and reference_age_ms <= 10_000 and source_age + file_age <= source_limit_ms else "STALE"
            asset[status_key] = status
            stale = status == "STALE"
            asset[f"{source}_stale"] = stale
            if status != "FRESH":
                asset[source] = None
        asset["stale"] = asset["binance_stale"] and asset["chainlink_stale"]
        if asset.get("binance") is None or asset.get("chainlink") is None:
            asset["divergence_bps"] = None
        for name, source in asset.get("sources", {}).items():
            reported = source.get("status", "NOT_RECEIVED")
            age = source.get("message_age_ms")
            if age is not None:
                source["message_age_ms"] = max(0, float(age) + file_age)
            if reported == "FRESH" and (age is None or float(age) + file_age > _source_display_limit_ms(name, source)):
                source["status"] = "STALE"
            if source.get("status") != "FRESH":
                source["price"] = None
    if reference_prices["stale"]:
        for key in ("binance_btcusdt", "chainlink_btcusd", "divergence_usd", "divergence_bps"):
            reference_prices[key] = None
    # Analytics can take longer than the health freshness budget on a cold cache.
    # Re-read the small canonical health snapshot after that work so a healthy
    # engine is not marked stale because the Web request itself was expensive.
    shadow_health = _json(shadow_health_path, shadow_health)
    shadow_health_age = time.time() - shadow_health.get("updated_at", 0)
    shadow_health["age_seconds"] = max(0.0, shadow_health_age)
    shadow_health["stale"] = shadow_health_age > 5
    shadow_health["resyncs"] = int(shadow_health.get("full_resyncs", 0))
    if not shadow_health or shadow_health["stale"] or not shadow_health.get("ws_connected"):
        system_status = "BLOCKED"
    elif reference_prices["stale"]:
        system_status = "DEGRADED"
    else:
        system_status = "ONLINE"
    rejection_reasons = Counter(
        item.get("reason", "unknown") for item in shadow_events if item.get("decision") == "REJECT"
    )
    decisions = list(state.get("client_order_ids", {}).values())
    decision_records = [item for item in decisions if isinstance(item, dict)]
    config = StrategyConfig()
    model_edges = [item for item in signals if item.get("model_probability", 0) - item.get("expected_fill_price", 1) > config.min_edge]
    blocked = Counter(reason for item in signals if (reason := _signal_block_reason(item, config)))
    risk_passed = [item for item in signals if _signal_block_reason(item, config) is None]
    market_matrix = {
        asset: {
            interval: {
                "count": 0, "markets": [], "reference_ready": 0, "reference_blocked": 0,
            }
            for interval in INTERVALS
        }
        for asset in ASSETS
    }
    market_reference_states = {}
    maximum_reference_age_ms = float(os.getenv("REFERENCE_MAX_AGE_MS", "3000"))
    for market in sorted(markets, key=lambda item: item.get("close_ts", 0)):
        asset, interval = market.get("asset"), market.get("interval")
        if asset in market_matrix and interval in market_matrix[asset]:
            cell = market_matrix[asset][interval]
            entry = dict(market)
            entry["slot"] = "current" if cell["count"] == 0 else "next"
            reference = reference_state_for_asset(
                reference_prices.get("assets", {}).get(asset, {}),
                market.get("settlement_source"),
                maximum_reference_age_ms,
            )
            reference_row = {
                "market_id": market.get("market_id"),
                "asset": asset,
                "interval": interval,
                "settlement_source": market.get("settlement_source"),
                "settlement_verified": market.get("settlement_verified"),
                "settlement_block_reason": market.get("settlement_block_reason"),
                "fast_price": reference.fast_price,
                "consensus_price": reference.consensus_price,
                "settlement_reference": reference.settlement_reference,
                "fresh_exchange_source_count": reference.fresh_exchange_source_count,
                "fresh_usd_spot_source_count": reference.fresh_usd_spot_source_count,
                "cross_source_divergence_bps": reference.cross_source_divergence_bps,
                "reference_quorum_met": reference.reference_quorum_met,
                "reference_state": reference.reference_state,
                "reference_block_reason": reference.reference_block_reason,
            }
            # Scanner-marked unverified settlement (AGENTS.md 4.6): shadow
            # research only — display as blocked instead of a silent green REF.
            if market.get("settlement_verified") is False:
                reference_row["reference_quorum_met"] = False
                reference_row["reference_state"] = "REFERENCE_BLOCKED"
                reference_row["reference_block_reason"] = (
                    market.get("settlement_block_reason") or "settlement_reference_unverified"
                )
            market_reference_states[market.get("market_id")] = reference_row
            entry["reference"] = reference_row
            cell["markets"].append(entry)
            cell["count"] += 1
            if reference_row["reference_quorum_met"]:
                cell["reference_ready"] += 1
            else:
                cell["reference_blocked"] += 1
    latest_event = next(iter(latest_shadow.values()), None)
    strategy_score = _strategy_score(latest_event)
    strategy_count_result, strategy_refreshing = _strategy_counts_for_status(
        (selected_log, strategy_log)
    )
    empty_counts = _empty_strategy_counts()
    strategy_counts = {
        name: strategy_count_result.get(name, empty_counts[name])
        for name in STRATEGIES
    }
    raw_session_counts = shadow_health.get("session_strategy_counts", {})
    session_strategy_counts = {
        name: {
            field: int(raw_session_counts.get(name, {}).get(field, 0))
            for field in ("evaluations", "accepts", "rejections")
        }
        for name in STRATEGIES
    }
    session_strategy_evaluations = sum(
        row["evaluations"] for row in session_strategy_counts.values()
    )
    analytics_refreshing = report_refreshing or strategy_refreshing
    strategy_evaluations = sum(row["evaluations"] for row in strategy_counts.values())
    strategy_accepts = sum(row["accepts"] for row in strategy_counts.values())
    unique_opportunities = sum(row["unique_opportunities"] for row in strategy_counts.values())
    active_opportunities = sum(row["active_opportunities"] for row in strategy_counts.values())
    probability_strategy_evaluations = sum(
        strategy_counts[name]["evaluations"]
        for name in ("late_window_directional_ev", "low_price_lottery_ev")
    )
    paired_evaluations = strategy_counts["paired_lock"]["evaluations"]
    strategy_latest = {}
    for item in shadow_events:
        if item.get("event_type") not in {
            "shadow_eval",
            "maker_episode_opened", "maker_episode_rejected",
            "maker_episode_completed", "maker_episode_closed_with_loss",
        }:
            continue
        strategy = item.get("strategy", "paired_lock")
        strategy_latest.setdefault(strategy, item)
    strategy_recent = {}
    for strategy in STRATEGIES:
        recent = [item for item in shadow_events if item.get("strategy", "paired_lock") == strategy]
        strategy_recent[strategy] = {
            "by_asset": dict(Counter(item.get("asset", "UNKNOWN") for item in recent)),
            "rejection_reasons": dict(Counter(
                item.get("reason", "unknown") for item in recent if item.get("decision") == "REJECT"
            )),
        }
    maker_accumulate = _maker_accumulate_view(
        events, strategy_counts, session_strategy_counts,
        strategy_recent, maker_shadow_state, time.time(),
    )
    asset_latest_pnl = {asset: None for asset in ASSETS}
    asset_latest_pnl.update({
        asset: item for asset, item in report.get("asset_latest_pnl", {}).items()
        if asset in asset_latest_pnl
    })
    ready_markets = int(shadow_health.get("ready_markets", 0))
    clob_readiness = {
        "discovered_markets": len(markets), "paired_markets_ready": ready_markets,
        "not_ready": max(0, len(markets) - ready_markets),
        "waiting_up_snapshot": int(shadow_health.get("waiting_up_snapshot", 0)),
        "waiting_down_snapshot": int(shadow_health.get("waiting_down_snapshot", 0)),
    }
    pair_fields = (
        "market_id", "up_vwap", "down_vwap", "gross_cost", "up_fee", "down_fee",
        "buffer", "net_cost", "guaranteed_payout", "locked_profit",
        "expected_execution_value", "decision", "reason", "sizing_mode",
        "requested_max_size", "dynamic_target_size", "market_minimum_size",
        "executable_depth_size", "slippage_limited_size", "capital_limited_size",
        "shadow_capital_usd", "capital_budget_usd", "dynamic_fee",
        "dynamic_buffer", "dynamic_all_in_cost", "dynamic_all_in_price",
        "dynamic_expected_profit", "dynamic_maximum_loss",
        "size_binding_constraint",
    )
    current_pair = {key: latest_event.get(key) for key in pair_fields} if latest_event else {}

    current_hash = report.get("current_strategy_config_hash")
    current_hashes = report.get("current_strategy_config_hashes", {})
    current_paired_hash = report.get("current_paired_config_hash")
    current_positions = [
        position for position in lifecycle_state.get("positions", {}).values()
        if (
            position.get("strategy") == "paired_lock"
            and position.get("strategy_config_hash")
            and (
                current_paired_hash is None
                or position.get("strategy_config_hash") == current_paired_hash
            )
        ) or (
            position.get("strategy") != "paired_lock"
            and position.get("strategy_config_hash") in {
                current_hash, current_hashes.get(position.get("strategy")),
            }
        )
    ]
    active_shadow_positions = len(current_positions)
    all_active_positions = list(lifecycle_state.get("positions", {}).values())
    invalid_dynamic_position_details = []
    for key, position in lifecycle_state.get("positions", {}).items():
        issues = _dynamic_position_issues(position)
        if not issues:
            continue
        invalid_dynamic_position_details.append({
            "position_key": key,
            "strategy": position.get("strategy"),
            "asset": position.get("asset"),
            "timeframe": position.get("timeframe"),
            "outcome": position.get("outcome"),
            "entry_ts": position.get("entry_ts"),
            "strategy_config_hash": position.get("strategy_config_hash"),
            "issues": issues,
        })
    invalid_dynamic_positions = len(invalid_dynamic_position_details)
    invalid_dynamic_position_reasons = dict(Counter(
        issue
        for position in invalid_dynamic_position_details
        for issue in position["issues"]
    ))
    dynamic_sizing = {
        "active_positions": len(all_active_positions),
        "active_capital_usd": round(sum(
            float(position.get("entry_cost") or 0) for position in all_active_positions
        ), 12),
        "maximum_loss_usd": round(sum(
            float(position.get("dynamic_maximum_loss") or 0)
            for position in all_active_positions
        ), 12),
        "invalid_active_positions": invalid_dynamic_positions,
        "invalid_active_position_reasons": invalid_dynamic_position_reasons,
        "invalid_active_position_details": invalid_dynamic_position_details[:20],
        "semantics": "REAL_MARKET_BOOK_SIZED_SHADOW_NOT_ORDERS",
    }
    simulated_complete = report["performance"]["completed"]
    locked_complete = sum(
        report["performance_by_strategy"].get(name, {}).get("completed", 0)
        for name in ("paired_lock", "maker_paired_accumulate")
    )
    current_inventory_hash = shadow_health.get("inventory_config_hash")
    complete_set_inventory = []
    for item in lifecycle_state.get("complete_set_inventory", {}).values():
        row = dict(item)
        origin_hash = row.get("origin_config_hash") or row.get("config_hash")
        if current_inventory_hash and origin_hash:
            cohort = "CURRENT" if origin_hash == current_inventory_hash else "LEGACY"
        else:
            cohort = "UNKNOWN"
        row["cohort"] = cohort
        row["cost"] = float(row.get("up_cost") or 0) + float(row.get("down_cost") or 0)
        row["quantity"] = (
            float(row.get("up_quantity") or 0)
            + float(row.get("down_quantity") or 0)
        )
        row["seconds_to_close"] = (
            max(0.0, float(row.get("close_ts") or 0) - time.time())
            if row.get("close_ts") is not None else None
        )
        complete_set_inventory.append(row)

    def inventory_summary(cohort):
        rows = [item for item in complete_set_inventory if item["cohort"] == cohort]
        return {
            "positions": len(rows),
            "cost": round(sum(item["cost"] for item in rows), 12),
            "quantity": round(sum(item["quantity"] for item in rows), 12),
            "maximum_loss": round(sum(item["cost"] for item in rows), 12),
            "next_close_seconds": min(
                (item["seconds_to_close"] for item in rows
                 if item["seconds_to_close"] is not None),
                default=None,
            ),
        }

    inventory_cohorts = {
        cohort.lower(): inventory_summary(cohort)
        for cohort in ("CURRENT", "LEGACY", "UNKNOWN")
    }
    engine_latency = reference_prices.get("engine_latency_us")
    def age_snapshot(source):
        values = []
        for item in reference_prices.get("assets", {}).values():
            normalized = item.get("sources", {}).get(source, {})
            if normalized.get("message_age_ms") is not None:
                values.append(float(normalized["message_age_ms"]))
            elif item.get(f"{source}_source_age_ms", -1) >= 0:
                values.append(float(item[f"{source}_source_age_ms"]) + max(reference_age_ms, 0))
        values.sort()
        return {"latest": values[-1] if values else None,
                "p50": values[len(values) // 2] if values else None,
                "p95": values[min(len(values) - 1, round((len(values) - 1) * .95))] if values else None,
                "p99": values[-1] if values else None, "samples": len(values), "unit": "ms",
                "metric": "message_age"}
    clob_age = dict(report["source_age_ms"])
    clob_age.update({"unit": "ms", "metric": "message_age"})
    latency_rankings = {
        "polymarket": clob_age,
        "binance": age_snapshot("binance"),
        "coinbase": age_snapshot("coinbase"),
        "kraken": age_snapshot("kraken"),
        "chainlink": age_snapshot("chainlink"),
        "engine": {"latest": engine_latency, "p50": None, "p95": None, "p99": None,
                   "samples": int(engine_latency is not None), "unit": "us", "metric": "processing_time"},
    }
    return {
        "ts": int(time.time()),
        "mode": "DRY RUN",
        "snapshot": snapshot,
        "signals": signals,
        "events": events[:100],
        "counts": {
            "raw_signals": len(signals),
            "model_edges": len(model_edges),
            "risk_passed": len(risk_passed),
            "executed_orders": sum(item.get("status") in {"filled", "partially_filled", "submitted"} for item in decision_records),
            "risk_decisions": len(decisions),
            "shadow_attempts": sum(item.get("status") == "dry_run" for item in decision_records),
            "shadow_evaluations": strategy_evaluations,
            "total_strategy_evaluations": strategy_evaluations,
            "probability_strategy_evaluations": probability_strategy_evaluations,
            "paired_evaluations": paired_evaluations,
            "fok_passed": report["fok_passed"],
            "shadow_accepts": max(strategy_accepts, report["accepts"]),
            "model_accepts": (
                strategy_counts["late_window_directional_ev"]["accepts"]
                + strategy_counts["low_price_lottery_ev"]["accepts"]
            ),
            "simulated_opened": active_shadow_positions + simulated_complete,
            "active_shadow_positions": active_shadow_positions,
            "unique_opportunities": unique_opportunities,
            "active_opportunities": active_opportunities,
            "simulated_complete": simulated_complete,
            "locked_complete": locked_complete,
            "session_strategy_evaluations": session_strategy_evaluations,
            "session_paired_evaluations": session_strategy_counts["paired_lock"]["evaluations"],
        },
        "shadow_markets": list(latest_shadow.values()),
        "strategy_counts": strategy_counts,
        "session_strategy_counts": session_strategy_counts,
        "strategy_latest": strategy_latest,
        "dynamic_sizing": dynamic_sizing,
        "strategy_recent": strategy_recent,
        "reference_prices": reference_prices,
        "shadow_health": shadow_health,
        "shadow_execution": shadow_execution,
        "shadow_lifecycle": {
            "open_positions": active_shadow_positions,
            "historical_open_positions_excluded": (
                len(lifecycle_state.get("positions", {})) - active_shadow_positions
            ),
            "active_positions": sum(
                position.get("lifecycle_state", "ACTIVE") == "ACTIVE"
                for position in current_positions
            ),
            "settlement_pending": sum(
                position.get("lifecycle_state") == "SETTLEMENT_PENDING"
                for position in current_positions
            ),
            "orphaned_positions": len(lifecycle_state.get("orphaned_positions", [])),
            "positions": current_positions,
            "complete_set_inventory": complete_set_inventory,
            "inventory_cohorts": inventory_cohorts,
            "maker_quotes": list(lifecycle_state.get("maker_quotes", {}).values()),
            "completed_ids": simulated_complete,
            "historical_completed_excluded": report.get("excluded_other_strategy_config", 0),
            "pending_predictions": len(lifecycle_state.get("probability_predictions", {})),
            "completed_predictions": len(lifecycle_state.get("completed_predictions", [])),
            "portfolio_rejections": dict(Counter(lifecycle_state.get("portfolio_rejections", {}).values())),
            "current_risk_halts": lifecycle_state.get("current_risk_halts", {}),
            "would_halt_reasons": lifecycle_state.get("would_halt_reasons", {}),
            "calibration_bypasses": lifecycle_state.get("calibration_bypasses", {}),
            "calibration_mode": lifecycle_state.get("calibration_mode", False),
            "portfolio_limits_enforced": lifecycle_state.get("portfolio_limits_enforced", True),
            "risk_mode": lifecycle_state.get("risk_mode", "PORTFOLIO_LIMITS_ENFORCED"),
            "portfolio_limits": lifecycle_state.get("portfolio_limits", {}),
            "config_version": lifecycle_state.get("config_version"),
            "config_hash": lifecycle_state.get("config_hash"),
            "real_order_submissions": lifecycle_state.get("real_order_submissions"),
            "real_orders": lifecycle_state.get("real_orders"),
            "real_fills": lifecycle_state.get("real_fills"),
        },
        "probability_calibration": _probability_calibration_view(
            lifecycle_state.get("probability_calibration", {})
        ),
        "probability_observations": {
            "pending": len(lifecycle_state.get("probability_predictions", {})),
            "settled": len(lifecycle_state.get("completed_predictions", [])),
            "orphaned": len(lifecycle_state.get("orphaned_predictions", [])),
            "by_strategy": {
                strategy: {
                    "pending": sum(
                        row.get("strategy") == strategy
                        for row in lifecycle_state.get("probability_predictions", {}).values()
                    ),
                    "settled": int(
                        lifecycle_state.get("probability_calibration", {})
                        .get(strategy, {}).get("samples", 0)
                    ),
                }
                for strategy in (
                    "late_window_directional_ev", "low_price_lottery_ev",
                )
            },
            "semantics": "CALIBRATION_ONLY_NOT_ORDERS_OR_PNL",
        },
        "market_matrix": market_matrix,
        "market_reference_states": market_reference_states,
        "asset_latest_pnl": asset_latest_pnl,
        "analytics_refreshing": analytics_refreshing,
        "analytics_status": "REBUILDING" if analytics_refreshing else "READY",
        "engine_session": {
            "run_id": shadow_health.get("run_id"),
            "started_at": shadow_health.get("engine_started_at"),
            "age_seconds": (
                max(0.0, time.time() - float(shadow_health["engine_started_at"]))
                if shadow_health.get("engine_started_at") is not None else None
            ),
            "strategy_counts": session_strategy_counts,
            "evaluations": session_strategy_evaluations,
        },
        "system_status": system_status,
        "rejection_reasons": report["rejection_reasons"] or dict(rejection_reasons),
        "shadow_report": report,
        "performance": report["performance"],
        "performance_by_strategy": report["performance_by_strategy"],
        "equity_curve": report["equity_curve"],
        "trade_ledger": report["trade_ledger"],
        "pnl_meter": {"simulated_pnl": report["performance"]["simulated_pnl"], "realized_pnl": 0.0},
        "strategy_score": strategy_score,
        "current_pair": current_pair,
        "maker_accumulate": maker_accumulate,
        "strategy_enablement": {
            "late_window_directional_ev": directional_ev_enabled(),
            "low_price_lottery_ev": lottery_ev_enabled(),
            "paired_lock": True,
            "maker_paired_accumulate": maker_accumulate_enabled(),
        },
        "clob_readiness": clob_readiness,
        "pipeline_steps": {
            "ingest": "PASS" if markets else "BLOCKED",
            "clob_snap": "PASS" if shadow_health.get("ready_markets", 0) else "BLOCKED",
            "replay": "PASS" if report["evaluations"] else "N/A",
            "backtest": "N/A",
            "validate": "PASS" if latest_event and latest_event.get("decision") == "ACCEPT" else "BLOCKED" if latest_event else "N/A",
            "approve": "PASS" if latest_event and latest_event.get("event_type") == "shadow_opportunity" else "BLOCKED",
            "deploy": "BLOCKED",
        },
        "latency_rankings": latency_rankings,
        "blocked_reasons": dict(blocked),
        "risk_limits": {"max_seconds_to_close": config.max_seconds_to_close, "min_liquidity": config.min_liquidity},
        "latency_ms": {"polymarket": None, "binance": None, "chainlink": None, "engine": None},
        "sources": {"polymarket_clob": "configured", "binance": "configured", "chainlink": "validation-only"},
    }


def make_handler(web_dir, data_dir, log_file, state_file):
    class MonitorHandler(BaseHTTPRequestHandler):
        def _send(self, body, content_type, status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return

        def do_GET(self):  # noqa: N802
            route = self.path.split("?", 1)[0]
            if route == "/api/status":
                self._send(json.dumps(build_status(data_dir, log_file, state_file)).encode(), "application/json; charset=utf-8")
                return
            target = (web_dir / ("index.html" if route == "/" else route.lstrip("/"))).resolve()
            if web_dir.resolve() not in target.parents or not target.is_file():
                self._send(b"not found", "text/plain", 404)
                return
            self._send(target.read_bytes(), mimetypes.guess_type(str(target))[0] or "application/octet-stream")

        def log_message(self, format, *args):
            return

    return MonitorHandler


def serve(host, port, web_dir, data_dir, log_file, state_file):
    server = ThreadingHTTPServer((host, port), make_handler(web_dir, data_dir, log_file, state_file))
    print(f"WEB_MONITOR http://{host}:{port}")
    server.serve_forever()
