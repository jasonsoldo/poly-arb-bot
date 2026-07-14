import json
import mimetypes
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .shadow_report import build_report
from .strategy_config import StrategyConfig


_REPORT_CACHE = {}
_REPORT_LOCK = threading.Lock()
_STRATEGY_COUNT_CACHE = {}
_STRATEGY_COUNT_LOCK = threading.Lock()
ASSETS = ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE")
INTERVALS = ("5m", "15m", "1h", "4h")


def _cached_report(path, execution_path=None):
    if not path.exists():
        return build_report_empty()
    stat = path.stat()
    execution_stat = execution_path.stat() if execution_path and execution_path.exists() else None
    key = (stat.st_size, stat.st_mtime_ns,
           execution_stat.st_size if execution_stat else 0,
           execution_stat.st_mtime_ns if execution_stat else 0)
    cached = _REPORT_CACHE.get(str(path))
    if cached and (cached[0] == key or time.monotonic() - cached[2] < 5):
        return cached[1]
    with _REPORT_LOCK:
        cached = _REPORT_CACHE.get(str(path))
        if cached and (cached[0] == key or time.monotonic() - cached[2] < 5):
            return cached[1]
        report = build_report(path, execution_path)
        _REPORT_CACHE[str(path)] = (key, report, time.monotonic())
        return report


def build_report_empty():
    empty_performance = {"completed": 0, "wins": 0, "losses": 0, "simulated_pnl": None,
                         "win_rate": None, "sharpe": None, "sharpe_samples": 0}
    return {
        "markets_seen": 0, "evaluations": 0, "fok_passed": 0, "accepts": 0,
        "invalid_json": 0, "rejection_reasons": {},
        "opportunity_duration_ms": {"p50": None, "p95": None, "max": None},
        "source_age_ms": {"latest": None, "p50": None, "p95": None, "p99": None,
                          "max": None, "samples": 0},
        "performance": dict(empty_performance),
        "performance_by_strategy": {
            strategy: dict(empty_performance)
            for strategy in ("late_window_directional_ev", "low_price_lottery_ev", "paired_lock")
        },
        "equity_curve": [], "trade_ledger": [],
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


def _strategy_counts(paths):
    names = ("late_window_directional_ev", "low_price_lottery_ev", "paired_lock")
    total = {name: {"evaluations": 0, "accepts": 0, "rejections": 0,
                    "model_evaluations": 0, "latest_model_evaluated": False,
                    "unique_opportunities": 0, "active_opportunities": 0} for name in names}
    with _STRATEGY_COUNT_LOCK:
        for path in paths:
            key = str(path.resolve())
            state = _STRATEGY_COUNT_CACHE.setdefault(key, {
                "offset": 0, "size": 0, "seen": set(),
                "counts": {name: {"evaluations": 0, "accepts": 0, "rejections": 0,
                                  "model_evaluations": 0, "latest_model_evaluated": False} for name in names},
                "active": {name: set() for name in names},
            })
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if size < state["offset"]:
                state.update({
                    "offset": 0, "size": 0, "seen": set(),
                    "counts": {name: {"evaluations": 0, "accepts": 0, "rejections": 0,
                                      "model_evaluations": 0, "latest_model_evaluated": False} for name in names},
                    "active": {name: set() for name in names},
                })
            if size > state["offset"]:
                try:
                    with path.open(encoding="utf-8") as handle:
                        handle.seek(state["offset"])
                        while line := handle.readline():
                            try:
                                row = json.loads(line)
                            except ValueError:
                                continue
                            strategy = row.get("strategy", "paired_lock")
                            if row.get("event_type") != "shadow_eval" or strategy not in state["counts"]:
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
                        state["offset"] = handle.tell()
                except OSError:
                    pass
            state["size"] = size
            for name in names:
                for field in ("evaluations", "accepts", "rejections", "model_evaluations"):
                    total[name][field] += state["counts"][name][field]
                total[name]["unique_opportunities"] += state["counts"][name].get("unique_opportunities", 0)
                total[name]["active_opportunities"] += len(state["active"][name])
                total[name]["latest_model_evaluated"] = state["counts"][name]["latest_model_evaluated"]
    return total


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
    report = _cached_report(selected_log, execution_log)
    shadow_events = [item for item in events if item.get("event_type") in {"shadow_eval", "shadow_opportunity"}]
    paired_events = [item for item in shadow_events if item.get("strategy", "paired_lock") == "paired_lock"]
    latest_shadow = {}
    for item in paired_events:
        if item.get("market_id") in market_ids:
            latest_shadow.setdefault(item.get("market_id"), item)
    state = _json(state_file, {"client_order_ids": {}})
    shadow_execution = _json(
        data_dir.parent / "state" / "shadow-execution.json",
        {"state": "IDLE", "processed": [], "audit_offset": 0},
    )
    shadow_execution["real_order_submissions"] = 0
    lifecycle_state = _json(
        data_dir.parent / "state" / "strategy-shadow.json",
        {"positions": {}, "completed": []},
    )
    reference_prices = _json(data_dir / "venue-status.json", {})
    reference_age_ms = time.time() * 1000 - reference_prices.get("updated_at_ms", 0)
    reference_prices["stale"] = reference_age_ms > 10_000
    for asset in reference_prices.get("assets", {}).values():
        file_age = max(reference_age_ms, 0)
        for source in ("binance", "chainlink"):
            source_age = asset.get(f"{source}_source_age_ms", -1)
            status_key = f"{source}_status"
            reported = asset.get(status_key)
            if asset.get("supported") is False:
                status = "UNSUPPORTED"
            elif reported in {"FRESH", "STALE", "DISCONNECTED", "NOT_RECEIVED", "UNSUPPORTED", "OUTLIER"}:
                status = "STALE" if reported == "FRESH" and (reference_age_ms > 10_000 or source_age + file_age > 10_000) else reported
            else:
                status = "FRESH" if source_age >= 0 and reference_age_ms <= 10_000 and source_age + file_age <= 10_000 else "STALE"
            asset[status_key] = status
            stale = status == "STALE"
            asset[f"{source}_stale"] = stale
            if status != "FRESH":
                asset[source] = None
        asset["stale"] = asset["binance_stale"] and asset["chainlink_stale"]
        if asset.get("binance") is None or asset.get("chainlink") is None:
            asset["divergence_bps"] = None
        for source in asset.get("sources", {}).values():
            reported = source.get("status", "NOT_RECEIVED")
            age = source.get("message_age_ms")
            if age is not None:
                source["message_age_ms"] = max(0, float(age) + file_age)
            if reported == "FRESH" and (age is None or float(age) + file_age > 10_000):
                source["status"] = "STALE"
            if source.get("status") != "FRESH":
                source["price"] = None
    if reference_prices["stale"]:
        for key in ("binance_btcusdt", "chainlink_btcusd", "divergence_usd", "divergence_bps"):
            reference_prices[key] = None
    shadow_health = _json(data_dir / "shadow-health.json", {})
    shadow_health_age = time.time() - shadow_health.get("updated_at", 0)
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
    market_matrix = {asset: {interval: {"count": 0, "markets": []} for interval in INTERVALS} for asset in ASSETS}
    for market in sorted(markets, key=lambda item: item.get("close_ts", 0)):
        asset, interval = market.get("asset"), market.get("interval")
        if asset in market_matrix and interval in market_matrix[asset]:
            cell = market_matrix[asset][interval]
            entry = dict(market)
            entry["slot"] = "current" if cell["count"] == 0 else "next"
            cell["markets"].append(entry)
            cell["count"] += 1
    latest_event = next(iter(latest_shadow.values()), None)
    strategy_score = _strategy_score(latest_event)
    strategy_counts = _strategy_counts((selected_log, strategy_log))
    strategy_evaluations = sum(row["evaluations"] for row in strategy_counts.values())
    strategy_accepts = sum(row["accepts"] for row in strategy_counts.values())
    unique_opportunities = sum(row["unique_opportunities"] for row in strategy_counts.values())
    active_opportunities = sum(row["active_opportunities"] for row in strategy_counts.values())
    strategy_latest = {}
    for item in shadow_events:
        strategy = item.get("strategy", "paired_lock")
        strategy_latest.setdefault(strategy, item)
    strategy_recent = {}
    for strategy in ("late_window_directional_ev", "low_price_lottery_ev", "paired_lock"):
        recent = [item for item in shadow_events if item.get("strategy", "paired_lock") == strategy]
        strategy_recent[strategy] = {
            "by_asset": dict(Counter(item.get("asset", "UNKNOWN") for item in recent)),
            "rejection_reasons": dict(Counter(
                item.get("reason", "unknown") for item in recent if item.get("decision") == "REJECT"
            )),
        }
    ready_markets = int(shadow_health.get("ready_markets", 0))
    clob_readiness = {
        "discovered_markets": len(markets), "paired_markets_ready": ready_markets,
        "not_ready": max(0, len(markets) - ready_markets),
        "waiting_up_snapshot": int(shadow_health.get("waiting_up_snapshot", 0)),
        "waiting_down_snapshot": int(shadow_health.get("waiting_down_snapshot", 0)),
    }
    pair_fields = ("market_id", "up_vwap", "down_vwap", "gross_cost", "up_fee", "down_fee",
                   "buffer", "net_cost", "guaranteed_payout", "locked_profit",
                   "expected_execution_value", "decision", "reason")
    current_pair = {key: latest_event.get(key) for key in pair_fields} if latest_event else {}
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
            "fok_passed": report["fok_passed"],
            "shadow_accepts": max(strategy_accepts, report["accepts"]),
            "unique_opportunities": unique_opportunities,
            "active_opportunities": active_opportunities,
            "simulated_complete": report["performance"]["completed"],
        },
        "shadow_markets": list(latest_shadow.values()),
        "strategy_counts": strategy_counts,
        "strategy_latest": strategy_latest,
        "strategy_recent": strategy_recent,
        "reference_prices": reference_prices,
        "shadow_health": shadow_health,
        "shadow_execution": shadow_execution,
        "shadow_lifecycle": {
            "open_positions": len(lifecycle_state.get("positions", {})),
            "completed_ids": len(lifecycle_state.get("completed", [])),
            "portfolio_rejections": dict(Counter(lifecycle_state.get("portfolio_rejections", {}).values())),
            "portfolio_limits": lifecycle_state.get("portfolio_limits", {}),
            "config_version": lifecycle_state.get("config_version"),
            "config_hash": lifecycle_state.get("config_hash"),
            "real_order_submissions": 0,
            "real_orders": 0,
        },
        "market_matrix": market_matrix,
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
            except (BrokenPipeError, ConnectionResetError):
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
