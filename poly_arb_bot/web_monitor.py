import json
import mimetypes
import time
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .shadow_report import build_report
from .strategy_config import StrategyConfig


_REPORT_CACHE = {}
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
    if cached and cached[0] == key:
        return cached[1]
    report = build_report(path, execution_path)
    _REPORT_CACHE[str(path)] = (key, report)
    return report


def build_report_empty():
    return {
        "markets_seen": 0, "evaluations": 0, "fok_passed": 0, "accepts": 0,
        "invalid_json": 0, "rejection_reasons": {},
        "opportunity_duration_ms": {"p50": None, "p95": None, "max": None},
        "source_age_ms": {"p50": None, "p95": None, "max": None},
        "performance": {"completed": 0, "wins": 0, "losses": 0, "simulated_pnl": 0.0,
                        "win_rate": None, "sharpe": None, "sharpe_samples": 0},
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
        with path.open(encoding="utf-8") as handle:
            lines = deque(handle, maxlen=limit)
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return list(reversed(rows))


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
    if not event or event.get("reason") in blockers or event.get("event_type") != "shadow_opportunity":
        return {"total": 0, "blocked": True, "components": {}}
    size = max(float(event.get("size", 0)), 1e-9)
    eev = max(0.0, min(1.0, float(event.get("expected_execution_value", 0)) / 0.01))
    depth = max(0.0, min(1.0, min(float(event.get("up_fill", 0)), float(event.get("down_fill", 0))) / size))
    freshness = max(0.0, min(1.0, 1 - float(event.get("source_age_ms", 1000)) / 1000))
    skew = max(0.0, min(1.0, 1 - float(event.get("book_skew_ms", 500)) / 500))
    leg_risk = max(0.0, min(1.0, float(event.get("leg_1_fill_probability", 0)) *
                            float(event.get("leg_2_fill_probability", 0)) *
                            (1 - min(1.0, float(event.get("orphan_leg_loss", 1))))))
    components = {"eev": eev, "depth": depth, "freshness": freshness, "book_skew": skew, "leg_risk": leg_risk}
    total = 100 * (0.35 * eev + 0.20 * depth + 0.15 * freshness + 0.15 * skew + 0.15 * leg_risk)
    return {"total": round(total, 2), "blocked": False, "components": components}


def build_status(data_dir, log_file, state_file):
    snapshot = _json(data_dir / "live_snapshot.json", {"signals": [], "positions": []})
    markets = _json(data_dir / "live_markets.json", {"markets": []}).get("markets", [])
    market_ids = {item.get("market_id") for item in markets}
    signals = [item for item in snapshot.get("signals", []) if item.get("market_id") in market_ids]
    shadow_log = data_dir.parent / "logs" / "shadow-audit.jsonl"
    selected_log = shadow_log if shadow_log.exists() else log_file
    events = _jsonl(selected_log, limit=1000)
    execution_log = data_dir.parent / "logs" / "shadow-execution.jsonl"
    report = _cached_report(selected_log, execution_log)
    shadow_events = [item for item in events if item.get("event_type") in {"shadow_eval", "shadow_opportunity"}]
    latest_shadow = {}
    for item in shadow_events:
        if item.get("market_id") in market_ids:
            latest_shadow.setdefault(item.get("market_id"), item)
    state = _json(state_file, {"client_order_ids": {}})
    shadow_execution = _json(
        data_dir.parent / "state" / "shadow-execution.json",
        {"state": "IDLE", "processed": [], "audit_offset": 0},
    )
    shadow_execution["real_order_submissions"] = 0
    reference_prices = _json(data_dir / "venue-status.json", {})
    reference_age_ms = time.time() * 1000 - reference_prices.get("updated_at_ms", 0)
    reference_prices["stale"] = reference_age_ms > 10_000
    for asset in reference_prices.get("assets", {}).values():
        file_age = max(reference_age_ms, 0)
        for source in ("binance", "chainlink"):
            source_age = asset.get(f"{source}_source_age_ms", -1)
            stale = reference_age_ms > 10_000 or source_age < 0 or source_age + file_age > 10_000
            asset[f"{source}_stale"] = stale
            if stale:
                asset[source] = None
        asset["stale"] = asset["binance_stale"] and asset["chainlink_stale"]
        if asset.get("binance") is None or asset.get("chainlink") is None:
            asset["divergence_bps"] = None
    if reference_prices["stale"]:
        for key in ("binance_btcusdt", "chainlink_btcusd", "divergence_usd", "divergence_bps"):
            reference_prices[key] = None
    shadow_health = _json(data_dir / "shadow-health.json", {})
    shadow_health_age = time.time() - shadow_health.get("updated_at", 0)
    shadow_health["stale"] = shadow_health_age > 5
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
    engine_latency = reference_prices.get("engine_latency_us")
    latency_rankings = {
        "polymarket": {"latest": None, "p50": None, "p95": None, "p99": None, "samples": 0, "unit": "ms"},
        "binance": {"latest": None, "p50": None, "p95": None, "p99": None, "samples": 0, "unit": "ms"},
        "chainlink": {"latest": None, "p50": None, "p95": None, "p99": None, "samples": 0, "unit": "ms"},
        "engine": {"latest": engine_latency, "p50": None, "p95": None, "p99": None,
                   "samples": int(engine_latency is not None), "unit": "us"},
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
            "shadow_evaluations": report["evaluations"],
            "fok_passed": report["fok_passed"],
            "shadow_opportunities": report["accepts"],
            "simulated_complete": report["performance"]["completed"],
        },
        "shadow_markets": list(latest_shadow.values()),
        "reference_prices": reference_prices,
        "shadow_health": shadow_health,
        "shadow_execution": shadow_execution,
        "market_matrix": market_matrix,
        "system_status": system_status,
        "rejection_reasons": report["rejection_reasons"] or dict(rejection_reasons),
        "shadow_report": report,
        "performance": report["performance"],
        "equity_curve": report["equity_curve"],
        "trade_ledger": report["trade_ledger"],
        "pnl_meter": {"simulated_pnl": report["performance"]["simulated_pnl"], "realized_pnl": 0.0},
        "strategy_score": strategy_score,
        "pipeline_steps": {
            "ingest": "PASS" if markets else "BLOCKED",
            "clob_snap": "PASS" if shadow_health.get("ready_markets", 0) else "BLOCKED",
            "replay": "PASS" if report["evaluations"] else "N/A",
            "backtest": "N/A",
            "validate": "PASS" if latest_event else "N/A",
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
            self.wfile.write(body)

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
