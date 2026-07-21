"""Phase 0 research: collect CLOB WS trade flow (last_trade_price) + book tops.

Read-only market data collection over the public market WebSocket.
No orders are placed.

Usage:
    python scripts/collect_trade_flow_ws.py --duration 240 \
        --output data/phase0-trade-flow.jsonl

Emits JSONL lines:
    {"ts", "kind": "trade", market_id, asset, timeframe, window, outcome,
     token_id, price, size, side, seconds_to_close}
    {"ts", "kind": "book_top", market_id, ..., outcome, token_id,
     best_bid, best_ask, bid_size_at_best, ask_size_at_best}
    {"ts", "kind": "heartbeat", n_trades, n_book_updates}
Append mode + bounded --duration lets us run several chunks under shell limits.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def load_markets(path: str):
    data = json.loads(Path(path).read_text())
    markets = data.get("markets", []) if isinstance(data, dict) else data
    now = time.time()
    return [m for m in markets if float(m.get("close_ts", 0)) > now + 5]


async def collect(args):
    import websockets

    markets = load_markets(args.markets)
    token_meta = {}
    for m in markets:
        for outcome in ("up", "down"):
            token_meta[str(m[f"{outcome}_token_id"])] = {
                "market_id": m["market_id"],
                "asset": m.get("asset"),
                "timeframe": m.get("interval") or m.get("timeframe"),
                "window": m.get("window"),
                "outcome": outcome.capitalize(),
                "close_ts": m.get("close_ts"),
            }
    deadline = time.monotonic() + args.duration
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    n_trades = 0
    n_books = 0

    def meta_for(token, ts):
        meta = dict(token_meta.get(token, {}))
        meta["token_id"] = token
        if meta.get("close_ts"):
            meta["seconds_to_close"] = float(meta["close_ts"]) - ts
        return meta

    with out.open("a", encoding="utf-8") as fh:
        while time.monotonic() < deadline:
            markets = load_markets(args.markets)
            token_meta = {}
            for m in markets:
                for outcome in ("up", "down"):
                    token_meta[str(m[f"{outcome}_token_id"])] = {
                        "market_id": m["market_id"],
                        "asset": m.get("asset"),
                        "timeframe": m.get("interval") or m.get("timeframe"),
                        "window": m.get("window"),
                        "outcome": outcome.capitalize(),
                        "close_ts": m.get("close_ts"),
                    }
            try:
                async with websockets.connect(
                    WS_URL, ping_interval=10, ping_timeout=20, open_timeout=20
                ) as socket:
                    await socket.send(json.dumps({
                        "assets_ids": list(token_meta),
                        "type": "market",
                        "custom_feature_enabled": True,
                    }))
                    while time.monotonic() < deadline:
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            fh.write(json.dumps({
                                "ts": time.time(), "kind": "heartbeat",
                                "n_trades": n_trades, "n_book_updates": n_books,
                            }) + "\n")
                            fh.flush()
                            continue
                        if raw == "PONG":
                            continue
                        ts = time.time()
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        events = payload if isinstance(payload, list) else [payload]
                        for event in events:
                            if not isinstance(event, dict):
                                continue
                            etype = event.get("event_type", "")
                            if etype == "last_trade_price":
                                token = str(event.get("asset_id") or "")
                                row = {"ts": ts, "kind": "trade",
                                       "price": event.get("price"),
                                       "size": event.get("size"),
                                       "side": event.get("side"),
                                       "fee_rate_bps": event.get("fee_rate_bps")}
                                row.update(meta_for(token, ts))
                                fh.write(json.dumps(row) + "\n")
                                n_trades += 1
                            elif etype == "book":
                                token = str(event.get("asset_id") or event.get("market") or "")
                                bids = event.get("bids", []) or []
                                asks = event.get("asks", []) or []
                                def lvl(level):
                                    if isinstance(level, dict):
                                        return level.get("price"), level.get("size")
                                    return level[0], level[1]
                                bb = max((float(lvl(l)[0]) for l in bids), default=None)
                                ba = min((float(lvl(l)[0]) for l in asks), default=None)
                                bs = next((float(lvl(l)[1]) for l in bids if bb is not None and float(lvl(l)[0]) == bb), None)
                                asz = next((float(lvl(l)[1]) for l in asks if ba is not None and float(lvl(l)[0]) == ba), None)
                                row = {"ts": ts, "kind": "book_top",
                                       "best_bid": bb, "best_ask": ba,
                                       "bid_size_at_best": bs, "ask_size_at_best": asz}
                                row.update(meta_for(token, ts))
                                fh.write(json.dumps(row) + "\n")
                                n_books += 1
                            elif etype == "price_change":
                                if not args.log_price_changes:
                                    continue
                                for change in event.get("price_changes", []):
                                    token = str(change.get("asset_id") or "")
                                    row = {"ts": ts, "kind": "price_change",
                                           "price": change.get("price"),
                                           "side": change.get("side"),
                                           "best_bid": change.get("best_bid"),
                                           "best_ask": change.get("best_ask")}
                                    row.update(meta_for(token, ts))
                                    fh.write(json.dumps(row) + "\n")
                        fh.flush()
            except Exception as exc:  # noqa: BLE001 - reconnect until deadline
                fh.write(json.dumps({"ts": time.time(), "kind": "ws_error",
                                     "error": str(exc)}) + "\n")
                fh.flush()
                await asyncio.sleep(2)
    return n_trades, n_books


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", default="data/live_markets.json")
    parser.add_argument("--duration", type=float, default=240.0)
    parser.add_argument("--output", default="data/phase0-trade-flow.jsonl")
    parser.add_argument("--log-price-changes", action="store_true",
                        help="also log every price_change event (very verbose)")
    args = parser.parse_args()
    n_trades, n_books = asyncio.run(collect(args))
    print(json.dumps({"trades": n_trades, "book_updates": n_books,
                      "output": args.output}))


if __name__ == "__main__":
    main()
