import json
import mimetypes
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .strategy_config import StrategyConfig


def _json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (OSError, ValueError):
        return default


def _jsonl(path, limit=100):
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
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
    market_ids = {item.get("market_id") for item in _json(data_dir / "live_markets.json", {"markets": []}).get("markets", [])}
    signals = [item for item in snapshot.get("signals", []) if item.get("market_id") in market_ids]
    state = _json(state_file, {"client_order_ids": {}})
    decisions = list(state.get("client_order_ids", {}).values())
    decision_records = [item for item in decisions if isinstance(item, dict)]
    config = StrategyConfig()
    model_edges = [item for item in signals if item.get("model_probability", 0) - item.get("expected_fill_price", 1) > config.min_edge]
    blocked = Counter(reason for item in signals if (reason := _signal_block_reason(item, config)))
    risk_passed = [item for item in signals if _signal_block_reason(item, config) is None]
    return {
        "ts": int(time.time()),
        "mode": "DRY RUN",
        "snapshot": snapshot,
        "signals": signals,
        "events": _jsonl(log_file),
        "counts": {
            "raw_signals": len(signals),
            "model_edges": len(model_edges),
            "risk_passed": len(risk_passed),
            "executed_orders": sum(item.get("status") in {"filled", "partially_filled", "submitted"} for item in decision_records),
            "risk_decisions": len(decisions),
            "shadow_attempts": sum(item.get("status") == "dry_run" for item in decision_records),
        },
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
