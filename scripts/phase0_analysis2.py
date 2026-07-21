"""Phase 0 research pass 2: leg2 chase dynamics, orphan-gap proxy, sell-side flow.

Inputs:
  data/phase0-book-snapshots.jsonl  (REST rounds, ~5s apart)
  data/phase0-trade-flow.jsonl      (WS trades)

Output: data/phase0-stats2.json
"""

import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pct(values, p):
    if not values:
        return None
    xs = sorted(values)
    k = (len(xs) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    if f == c:
        return xs[f]
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def dist(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    import statistics
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 6),
        "p5": round(pct(values, 5), 6),
        "p25": round(pct(values, 25), 6),
        "p50": round(pct(values, 50), 6),
        "p75": round(pct(values, 75), 6),
        "p95": round(pct(values, 95), 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }


def bid_drift():
    """Best-bid movement between consecutive REST rounds (~5s) per token."""
    path = ROOT / "data" / "phase0-book-snapshots.jsonl"
    per_token = defaultdict(list)  # token_key -> [(ts, best_bid, market_key)]
    with path.open() as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            up, down = e.get("up"), e.get("down")
            if not (up and down):
                continue
            mk = f"{e.get('asset')}/{e.get('timeframe')}"
            mid = e.get("market_id")
            for side_name, side in (("up", up), ("down", down)):
                bb = side.get("best_bid")
                if bb is not None:
                    per_token[f"{mid}:{side_name}"].append((e["ts"], bb, mk))
    deltas = []
    moved = 0
    total = 0
    by_group = defaultdict(list)
    for key, rows in per_token.items():
        rows.sort()
        for (t0, b0, mk), (t1, b1, _) in zip(rows, rows[1:]):
            if t1 - t0 > 20:  # skip gaps (market rotation)
                continue
            d = b1 - b0
            deltas.append(d)
            by_group[mk].append(d)
            total += 1
            if abs(d) >= 0.0095:
                moved += 1
    ticks = [abs(d) / 0.01 for d in deltas if abs(d) >= 0.0095]
    return {
        "n_round_pairs": total,
        "bid_moved_frac": round(moved / total, 4) if total else None,
        "bid_delta_per_5s": dist(deltas),
        "move_size_ticks_when_moved": dist(ticks),
        "move_frac_ge_1tick_by_group": {
            mk: round(sum(1 for d in v if abs(d) >= 0.0095) / len(v), 4)
            for mk, v in sorted(by_group.items()) if v
        },
    }


def sell_flow_and_orphan_gap():
    """Sell-side (bid-hitting) trades per market/min; leg1->leg2 sell gap proxy."""
    path = ROOT / "data" / "phase0-trade-flow.jsonl"
    trades = []
    with path.open() as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("kind") == "trade":
                trades.append(e)
    ts = [t["ts"] for t in trades]
    span_min = (max(ts) - min(ts)) / 60.0 if ts else 0
    sells_by_market = Counter()
    # per market: sells on up token vs down token
    market_sells = defaultdict(list)  # market_id -> [(ts, outcome)]
    meta = {}
    for tr in trades:
        side = (tr.get("side") or "").upper()
        mid = tr.get("market_id")
        meta[mid] = (tr.get("asset"), tr.get("timeframe"))
        if side == "SELL":
            key = (tr.get("asset"), tr.get("timeframe"))
            sells_by_market[key] += 1
            market_sells[mid].append((tr["ts"], tr.get("outcome")))
    # orphan gap: after a sell on one outcome, time until next sell on the other
    gaps = []
    gap_by_group = defaultdict(list)
    exhausted = Counter()
    for mid, rows in market_sells.items():
        rows.sort()
        key = meta.get(mid, (None, None))
        for i, (t0, o0) in enumerate(rows):
            for t1, o1 in rows[i + 1:]:
                if o1 != o0:
                    gaps.append(t1 - t0)
                    gap_by_group[f"{key[0]}/{key[1]}"].append(t1 - t0)
                    break
            else:
                exhausted[f"{key[0]}/{key[1]}"] += 1
    return {
        "window_minutes": round(span_min, 2),
        "n_sell_trades": sum(sells_by_market.values()),
        "sells_per_min_by_group": {
            f"{a}/{t}": round(c / span_min, 2) for (a, t), c in sells_by_market.most_common()
        },
        "leg1_to_leg2_sell_gap_seconds": dist(gaps),
        "gap_exceeded_window_count_by_group": dict(exhausted),
        "leg1_to_leg2_gap_by_group": {
            mk: dist(v) for mk, v in sorted(gap_by_group.items())
        },
    }


def main():
    stats = {
        "E_bid_drift_between_rounds": bid_drift(),
        "F_sell_flow_orphan_gap": sell_flow_and_orphan_gap(),
    }
    out = ROOT / "data" / "phase0-stats2.json"
    out.write_text(json.dumps(stats, indent=1))
    print(json.dumps({"wrote": str(out)}))


if __name__ == "__main__":
    main()
