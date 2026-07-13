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


def _cached_report(path):
    if not path.exists():
        return build_report_empty()
    stat = path.stat()
    key = (stat.st_size, stat.st_mtime_ns)
    cached = _REPORT_CACHE.get(str(path))
    if cached and cached[0] == key:
        return cached[1]
    report = build_report(path)
    _REPORT_CACHE[str(path)] = (key, report)
    return report


def build_report_empty():
    return {
        "markets_seen": 0, "evaluations": 0, "fok_passed": 0, "accepts": 0,
        "invalid_json": 0, "rejection_reasons": {},
        "opportunity_duration_ms": {"p50": None, "p95": None, "max": None},
        "source_age_ms": {"p50": None, "p95": None, "max": None},
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


def build_status(data_dir, log_file, state_file):
    snapshot = _json(data_dir / "live_snapshot.json", {"signals": [], "positions": []})
    markets = _json(data_dir / "live_markets.json", {"markets": []}).get("markets", [])
    market_ids = {item.get("market_id") for item in markets}
    signals = [item for item in snapshot.get("signals", []) if item.get("market_id") in market_ids]
    shadow_log = data_dir.parent / "logs" / "shadow-audit.jsonl"
    selected_log = shadow_log if shadow_log.exists() else log_file
    events = _jsonl(selected_log, limit=1000)
    report = _cached_report(selected_log)
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
        asset_stale = reference_age_ms > 10_000 or any(
            asset.get(key, 10_001) + max(reference_age_ms, 0) > 10_000
            for key in ("binance_source_age_ms", "chainlink_source_age_ms")
            if asset.get(key, -1) >= 0
        )
        asset["stale"] = asset_stale
        if asset_stale:
            for key in ("binance", "chainlink", "divergence_bps"):
                asset[key] = None
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
        },
        "shadow_markets": list(latest_shadow.values()),
        "reference_prices": reference_prices,
        "shadow_health": shadow_health,
        "shadow_execution": shadow_execution,
        "market_matrix": market_matrix,
        "system_status": system_status,
        "rejection_reasons": report["rejection_reasons"] or dict(rejection_reasons),
        "shadow_report": report,
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
