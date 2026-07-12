import json
import mimetypes
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


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


def build_status(data_dir, log_file, state_file):
    snapshot = _json(data_dir / "live_snapshot.json", {"signals": [], "positions": []})
    state = _json(state_file, {"client_order_ids": {}})
    return {
        "ts": int(time.time()),
        "mode": "DRY RUN",
        "snapshot": snapshot,
        "signals": snapshot.get("signals", []),
        "events": _jsonl(log_file),
        "orders_recorded": len(state.get("client_order_ids", {})),
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
