"""Phase 0 research: collect paired CLOB order-book snapshots via REST /books batch.

Read-only market data collection. No orders are placed.

Usage:
    python scripts/collect_orderbook_snapshots.py --duration 240 --interval 5 \
        --output data/phase0-book-snapshots.jsonl

Each line is one market snapshot:
    {ts, market_id, condition_id, asset, timeframe(interval), window, close_ts,
     seconds_to_close, tick_size, min_order_size, fee_rate,
     up:  {best_bid, best_ask, spread, bid_depth_top3, ask_depth_top3,
           bid_depth_total, ask_depth_total, imbalance, levels},
     down: {...}, latency_ms}
Append mode lets us run multiple bounded chunks under shell time limits.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from poly_arb_bot.clob_client import PolymarketClobClient


def load_markets(path: str):
    data = json.loads(Path(path).read_text())
    markets = data.get("markets", []) if isinstance(data, dict) else data
    now = time.time()
    active = [m for m in markets if float(m.get("close_ts", 0)) > now + 5]
    return active


def book_side(book):
    if book is None:
        return None
    bids, asks = book.bids, book.asks
    best_bid = bids[0].price if bids else None
    best_ask = asks[0].price if asks else None
    bid_top3 = sum(l.size for l in bids[:3])
    ask_top3 = sum(l.size for l in asks[:3])
    bid_total = sum(l.size for l in bids)
    ask_total = sum(l.size for l in asks)
    denom = bid_total + ask_total
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": (best_ask - best_bid) if best_bid is not None and best_ask is not None else None,
        "bid_size_at_best": bids[0].size if bids else None,
        "ask_size_at_best": asks[0].size if asks else None,
        "bid_depth_top3": bid_top3,
        "ask_depth_top3": ask_top3,
        "bid_depth_total": bid_total,
        "ask_depth_total": ask_total,
        "imbalance": (bid_total - ask_total) / denom if denom else None,
        "n_bid_levels": len(bids),
        "n_ask_levels": len(asks),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", default="data/live_markets.json")
    parser.add_argument("--duration", type=float, default=240.0)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--output", default="data/phase0-book-snapshots.jsonl")
    args = parser.parse_args()

    client = PolymarketClobClient()
    deadline = time.monotonic() + args.duration
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    rounds = 0
    with out.open("a", encoding="utf-8") as fh:
        while time.monotonic() < deadline:
            markets = load_markets(args.markets)
            if not markets:
                time.sleep(min(args.interval, 10))
                continue
            token_ids = []
            for m in markets:
                token_ids.extend([str(m["up_token_id"]), str(m["down_token_id"])])
            ts = time.time()
            try:
                books = client.get_books(token_ids)
            except Exception as exc:  # noqa: BLE001 - record and continue
                fh.write(json.dumps({"ts": ts, "error": str(exc)}) + "\n")
                fh.flush()
                time.sleep(args.interval)
                continue
            latency = max((b.latency_ms for b in books.values()), default=None)
            for m in markets:
                up = books.get(str(m["up_token_id"]))
                down = books.get(str(m["down_token_id"]))
                meta = up or down
                row = {
                    "ts": ts,
                    "market_id": m["market_id"],
                    "condition_id": m.get("condition_id", m["market_id"]),
                    "asset": m.get("asset"),
                    "timeframe": m.get("interval") or m.get("timeframe"),
                    "window": m.get("window"),
                    "close_ts": m.get("close_ts"),
                    "seconds_to_close": float(m.get("close_ts", 0)) - ts,
                    "tick_size": meta.tick_size if meta else m.get("tick_size"),
                    "min_order_size": meta.min_order_size if meta else m.get("min_order_size"),
                    "fee_rate": m.get("fee_rate"),
                    "latency_ms": latency,
                    "up": book_side(up),
                    "down": book_side(down),
                }
                fh.write(json.dumps(row) + "\n")
            fh.flush()
            rounds += 1
            elapsed = time.monotonic()
            next_at = ts + args.interval
            sleep_for = next_at - time.time()
            if sleep_for > 0:
                time.sleep(sleep_for)
    print(json.dumps({"rounds": rounds, "output": str(out)}))


if __name__ == "__main__":
    main()
