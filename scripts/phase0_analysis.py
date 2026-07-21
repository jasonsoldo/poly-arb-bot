"""Phase 0 maker parameter research: aggregate stats from audit logs + live probes.

Inputs (all real data):
  logs/shadow-audit.jsonl          paired_lock taker evaluations (best asks, vwap, fees)
  logs/strategy-audit.jsonl        maker_complete_set_arb quotes + trade-through events
  data/phase0-book-snapshots.jsonl live REST book snapshots (best_bid/best_ask/depth)
  data/phase0-trade-flow.jsonl     live WS trades + book tops

Output: data/phase0-stats.json
Read-only analysis; no orders.
"""

import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pct(values, p):
    if not values:
        return None
    xs = sorted(values)
    k = (len(xs) - 1) * p / 100.0
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return xs[int(k)]
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def dist(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
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


def frac(values, pred):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return round(sum(1 for v in values if pred(v)) / len(values), 4)


# ---------------------------------------------------------------- A. taker audit
def analyze_taker_audit():
    path = ROOT / "logs" / "shadow-audit.jsonl"
    per_pair_net = []
    ask_sum = []
    derived_bid_sum = []  # (1-up_best_ask)+(1-down_best_ask); approximation
    depth_up, depth_down = [], []
    imb = []
    book_age = []
    stc = []
    by_group = defaultdict(lambda: {"net": [], "ask_sum": [], "derived_bid_sum": []})
    ts_min, ts_max = None, None
    n = 0
    reasons = Counter()
    assets = Counter()
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("event_type") != "shadow_eval" or e.get("strategy") != "paired_lock":
                continue
            n += 1
            ts = e.get("ts") or e.get("timestamp")
            if ts:
                ts_min = ts if ts_min is None else min(ts_min, ts)
                ts_max = ts if ts_max is None else max(ts_max, ts)
            reasons[e.get("reason")] += 1
            ua, da = e.get("up_best_ask"), e.get("down_best_ask")
            if ua is not None and da is not None:
                s = ua + da
                ask_sum.append(s)
                derived_bid_sum.append(2.0 - s)
            fill = e.get("up_fill") or 0
            if fill and e.get("net_cost") is not None:
                pp = e["net_cost"] / fill  # per paired share
                per_pair_net.append(pp)
            if e.get("up_available_depth") is not None:
                depth_up.append(e["up_available_depth"])
            if e.get("down_available_depth") is not None:
                depth_down.append(e["down_available_depth"])
            if e.get("up_book_imbalance") is not None:
                imb.append(e["up_book_imbalance"])
            if e.get("book_age_ms") is not None:
                book_age.append(e["book_age_ms"])
            if e.get("seconds_to_close") is not None:
                stc.append(e["seconds_to_close"])
            key = (e.get("asset"), e.get("timeframe"))
            assets[key] += 1
            g = by_group[key]
            if ua is not None and da is not None:
                g["ask_sum"].append(ua + da)
                g["derived_bid_sum"].append(2.0 - ua - da)
            if fill and e.get("net_cost") is not None:
                g["net"].append(e["net_cost"] / fill)
    out = {
        "n_evaluations": n,
        "window_utc": [ts_min, ts_max],
        "window_minutes": round((ts_max - ts_min) / 60.0, 2) if ts_min and ts_max else None,
        "top_reject_reasons": reasons.most_common(12),
        "per_pair_net_cost": dist(per_pair_net),
        "best_ask_sum": dist(ask_sum),
        "derived_best_bid_sum_APPROX": dist(derived_bid_sum),
        "derived_bid_sum_frac_lt_1": frac(derived_bid_sum, lambda v: v < 1.0),
        "ask_sum_frac_lt_1": frac(ask_sum, lambda v: v < 1.0),
        "available_depth_up": dist(depth_up),
        "available_depth_down": dist(depth_down),
        "book_imbalance": dist(imb),
        "book_age_ms": dist(book_age),
        "seconds_to_close": dist(stc),
        "by_asset_timeframe": {
            f"{a}/{t}": {
                "n": sum(len(g[k]) for g in [by_group[k]] for k in ["net"]) or assets[k],
                "ask_sum": dist(by_group[k]["ask_sum"]),
                "derived_bid_sum_frac_lt_1": frac(by_group[k]["derived_bid_sum"], lambda v: v < 1.0),
                "per_pair_net": dist(by_group[k]["net"]),
            }
            for k in sorted(by_group) for a, t in [k]
        },
    }
    return out


# ---------------------------------------------------------------- B. maker observer
def analyze_maker_observer():
    path = ROOT / "logs" / "strategy-audit.jsonl"
    pair_cost, locked_edge, ev = [], [], []
    qualified = Counter()
    decisions = Counter()
    tt_single, tt_both = [], []
    tt_single_by_market = Counter()
    tt_both_by_market = Counter()
    quote_ages = []
    trade_sizes = []
    by_group = defaultdict(list)
    ts_min, ts_max = None, None
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("strategy") != "maker_complete_set_arb":
                continue
            ts = e.get("ts") or e.get("timestamp")
            if ts:
                ts_min = ts if ts_min is None else min(ts_min, ts)
                ts_max = ts if ts_max is None else max(ts_max, ts)
            et = e.get("event_type")
            if et == "shadow_maker_quote_eval":
                decisions[e.get("decision")] += 1
                qualified[bool(e.get("quote_geometry_qualified"))] += 1
                if e.get("pair_quote_cost") is not None:
                    pair_cost.append(e["pair_quote_cost"])
                    by_group[(e.get("asset"), e.get("timeframe"))].append(e["pair_quote_cost"])
                if e.get("locked_edge_if_both_fill") is not None:
                    locked_edge.append(e["locked_edge_if_both_fill"])
                if e.get("expected_value") is not None:
                    ev.append(e["expected_value"])
            elif et == "shadow_maker_single_leg_trade_through":
                tt_single.append(e)
                tt_single_by_market[(e.get("asset"), e.get("timeframe"))] += 1
                if e.get("quote_age_ms") is not None:
                    quote_ages.append(e["quote_age_ms"])
                if e.get("trade_size") is not None:
                    trade_sizes.append(e["trade_size"])
            elif et == "shadow_maker_both_legs_trade_through":
                tt_both.append(e)
                tt_both_by_market[(e.get("asset"), e.get("timeframe"))] += 1
    return {
        "window_utc": [ts_min, ts_max],
        "window_minutes": round((ts_max - ts_min) / 60.0, 2) if ts_min and ts_max else None,
        "quote_evals": sum(decisions.values()),
        "decisions": decisions.most_common(),
        "quote_geometry_qualified": dict(qualified),
        "pair_quote_cost_MODEL_QUOTE": dist(pair_cost),
        "pair_quote_cost_frac_lt_1": frac(pair_cost, lambda v: v < 1.0),
        "locked_edge_if_both_fill": dist(locked_edge),
        "locked_edge_frac_positive": frac(locked_edge, lambda v: v > 0),
        "expected_value": dist(ev),
        "trade_through_single_leg_count": len(tt_single),
        "trade_through_both_legs_count": len(tt_both),
        "trade_through_single_by_market": {f"{a}/{t}": c for (a, t), c in tt_single_by_market.most_common()},
        "trade_through_both_by_market": {f"{a}/{t}": c for (a, t), c in tt_both_by_market.most_common()},
        "quote_age_ms_at_trade_through": dist(quote_ages),
        "trade_size_at_trade_through": dist(trade_sizes),
        "pair_quote_cost_by_group": {
            f"{a}/{t}": dist(v) for (a, t), v in sorted(by_group.items())
        },
    }


# ---------------------------------------------------------------- C. live snapshots
def analyze_live_snapshots():
    path = ROOT / "data" / "phase0-book-snapshots.jsonl"
    if not path.exists():
        return {"error": "missing"}
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("up") and e.get("down"):
                rows.append(e)
    ts = [r["ts"] for r in rows]
    bid_sum, ask_sum, spread_up, spread_down = [], [], [], []
    compl_gap = []  # up_best_bid + down_best_ask - 1 (should be ~0 if complementary)
    bid_depth_best, ask_depth_best = [], []
    bid_depth_top3, ask_depth_top3 = [], []
    bid_depth_total, ask_depth_total = [], []
    improve_room = []  # spread > 1 tick (0.01)
    by_group = defaultdict(lambda: {"bid_sum": [], "spread": [], "depth_best_bid": [], "stc": []})
    stc_all = []
    for r in rows:
        u, d = r["up"], r["down"]
        if not (u and d):
            continue
        ub, db = u.get("best_bid"), d.get("best_bid")
        ua, da = u.get("best_ask"), d.get("best_ask")
        key = (r.get("asset"), r.get("timeframe"))
        if r.get("seconds_to_close") is not None:
            stc_all.append(r["seconds_to_close"])
        if ub is not None and db is not None:
            bid_sum.append(ub + db)
            by_group[key]["bid_sum"].append(ub + db)
        if ua is not None and da is not None:
            ask_sum.append(ua + da)
        if ub is not None and da is not None:
            compl_gap.append(ub + da - 1.0)
        for side, acc in ((u, spread_up), (d, spread_down)):
            if side.get("spread") is not None:
                acc.append(side["spread"])
                by_group[key]["spread"].append(side["spread"])
                improve_room.append(side["spread"] > 0.0105)
        for side in (u, d):
            if side.get("bid_size_at_best") is not None:
                bid_depth_best.append(side["bid_size_at_best"])
                by_group[key]["depth_best_bid"].append(side["bid_size_at_best"])
            if side.get("ask_size_at_best") is not None:
                ask_depth_best.append(side["ask_size_at_best"])
            if side.get("bid_depth_top3") is not None:
                bid_depth_top3.append(side["bid_depth_top3"])
            if side.get("ask_depth_top3") is not None:
                ask_depth_top3.append(side["ask_depth_top3"])
            if side.get("bid_depth_total") is not None:
                bid_depth_total.append(side["bid_depth_total"])
            if side.get("ask_depth_total") is not None:
                ask_depth_total.append(side["ask_depth_total"])
    buffers = [0.002, 0.005, 0.01]
    lockable = {
        f"buffer_{b}": frac(bid_sum, lambda v, b=b: v + b < 1.0) for b in buffers
    }
    margins = [1.0 - v for v in bid_sum]
    by_group_out = {}
    for (a, t), g in sorted(by_group.items()):
        gm = [1.0 - v for v in g["bid_sum"]]
        by_group_out[f"{a}/{t}"] = {
            "n_snapshots": len(g["bid_sum"]),
            "bid_sum": dist(g["bid_sum"]),
            "margin_over_1": dist(gm),
            "lockable_frac": {f"buffer_{b}": frac(g["bid_sum"], lambda v, b=b: v + b < 1.0) for b in buffers},
            "spread": dist(g["spread"]),
            "bid_size_at_best": dist(g["depth_best_bid"]),
        }
    return {
        "n_market_snapshots": len(rows),
        "window_utc": [min(ts), max(ts)] if ts else None,
        "window_minutes": round((max(ts) - min(ts)) / 60.0, 2) if ts else None,
        "seconds_to_close": dist(stc_all),
        "up_best_bid_plus_down_best_bid": dist(bid_sum),
        "maker_margin_over_1": dist(margins),
        "lockable_frac": lockable,
        "best_ask_sum": dist(ask_sum),
        "complementarity_gap_upbid_plus_downask_minus_1": dist(compl_gap),
        "spread_up": dist(spread_up),
        "spread_down": dist(spread_down),
        "spread_frac_gt_1tick": frac(improve_room, lambda v: v),
        "bid_size_at_best": dist(bid_depth_best),
        "ask_size_at_best": dist(ask_depth_best),
        "bid_depth_top3": dist(bid_depth_top3),
        "ask_depth_top3": dist(ask_depth_top3),
        "bid_depth_total": dist(bid_depth_total),
        "ask_depth_total": dist(ask_depth_total),
        "by_asset_timeframe": by_group_out,
    }


# ---------------------------------------------------------------- D. live trades
def analyze_live_trades():
    path = ROOT / "data" / "phase0-trade-flow.jsonl"
    if not path.exists():
        return {"error": "missing"}
    trades = []
    book_tops = defaultdict(list)  # token -> [(ts, best_bid, best_ask)]
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("kind") == "trade":
                trades.append(e)
            elif e.get("kind") == "book_top" and e.get("token_id"):
                book_tops[e["token_id"]].append(
                    (e["ts"], e.get("best_bid"), e.get("best_ask")))
    for v in book_tops.values():
        v.sort()
    ts = [t["ts"] for t in trades]
    span_min = (max(ts) - min(ts)) / 60.0 if ts else 0

    def last_top(token, t):
        arr = book_tops.get(token)
        if not arr:
            return None, None
        best = None
        for row in arr:
            if row[0] <= t:
                best = row
            else:
                break
        return (best[1], best[2]) if best else (None, None)

    # trade-through proxy: sell-side trade at price <= assumed join quote (best bid)
    through, touch, sizes = 0, 0, []
    price_vs_bid = []
    per_token = Counter()
    per_market = Counter()
    inter_trade = defaultdict(list)
    last_trade_ts = {}
    trade_price_series = defaultdict(list)
    for tr in trades:
        token = tr.get("token_id")
        px = tr.get("price")
        try:
            px = float(px)
        except (TypeError, ValueError):
            continue
        size = tr.get("size")
        try:
            sizes.append(float(size))
        except (TypeError, ValueError):
            pass
        side = (tr.get("side") or "").upper()
        bb, ba = last_top(token, tr["ts"])
        if side == "SELL" and bb is not None:
            price_vs_bid.append(px - bb)
            if px < bb - 1e-9:
                through += 1
            elif abs(px - bb) <= 1e-9:
                touch += 1
        per_token[token] += 1
        per_market[(tr.get("asset"), tr.get("timeframe"))] += 1
        if token in last_trade_ts:
            inter_trade[token].append(tr["ts"] - last_trade_ts[token])
        last_trade_ts[token] = tr["ts"]
        trade_price_series[token].append((tr["ts"], px))
    sells = sum(1 for tr in trades if (tr.get("side") or "").upper() == "SELL")
    gaps = [g for v in inter_trade.values() for g in v]
    per_market_out = {}
    for (a, t), c in per_market.most_common():
        per_market_out[f"{a}/{t}"] = {
            "trades": c,
            "trades_per_min": round(c / span_min, 2) if span_min else None,
        }
    return {
        "n_trades": len(trades),
        "n_sell_trades": sells,
        "window_utc": [min(ts), max(ts)] if ts else None,
        "window_minutes": round(span_min, 2),
        "trades_per_min_all_markets": round(len(trades) / span_min, 2) if span_min else None,
        "trade_size": dist(sizes),
        "sell_trade_price_minus_best_bid": dist(price_vs_bid),
        "trade_through_strict_count": through,
        "trade_touch_count": touch,
        "trade_through_frac_of_sells": round(through / sells, 4) if sells else None,
        "trade_touch_frac_of_sells": round(touch / sells, 4) if sells else None,
        "inter_trade_gap_seconds": dist(gaps),
        "per_asset_timeframe": per_market_out,
        "tokens_with_trades": len(per_token),
    }


def main():
    stats = {
        "generated_at": __import__("time").time(),
        "A_taker_audit": analyze_taker_audit(),
        "B_maker_observer": analyze_maker_observer(),
        "C_live_book_snapshots": analyze_live_snapshots(),
        "D_live_trade_flow": analyze_live_trades(),
    }
    out = ROOT / "data" / "phase0-stats.json"
    out.write_text(json.dumps(stats, indent=1))
    print(json.dumps({"wrote": str(out)}))


if __name__ == "__main__":
    main()
