"""Runtime bridge for the ``maker_paired_accumulate`` shadow strategy.

Tails the C++ ``market_ws_engine`` paired_lock audit stream
(``logs/shadow-audit.jsonl``), builds ``MakerAccumulateInput`` rows from the
real C++ order-book evaluation fields, drives one
``MakerAccumulateStateMachine``, and appends the machine's audit events to
``logs/strategy-audit.jsonl`` (one complete JSON object per line, flushed;
producer-generated ``event_id`` values are preserved verbatim).

Pure Shadow / Dry Run: every event keeps ``real_order_submissions = 0``,
``real_orders = 0``, ``real_fills = 0``.

Book-view provenance (no fabricated book data). The C++ engine is the
order-book authority; the only Python-visible per-market book view is the
paired_lock ``shadow_eval`` event. Field mapping:

REAL (computed by C++ from the live WS books):
  best_ask, ask VWAP, executable ask depth (``up_fill``/``down_fill``),
  total ask depth (``*_available_depth``), full-book imbalance,
  per-side book age, ``books_synced``, fee rate, clock-skew diagnostic,
  generation / session / evaluation_sequence, seconds_to_close,
  bid-side sell VWAP for the paired target size (from the C++
  ``shadow_split_sell_eval`` events, ``up_sell_vwap``/``down_sell_vwap``).
DERIVED (documented approximations, never presented as raw book data):
  ``best_bid`` = latest real sell VWAP for the target size when fresh
  (conservative: VWAP over N shares <= best bid, so quote placement and
  margin checks are biased towards fewer ACCEPTs, never more); fallback
  when no fresh bid view exists: 1 - opposite best_ask (binary complement,
  an optimistic no-arb upper bound — recorded as such in the event basis),
  ``bid_depth_total`` = ask_depth * (1+imb)/(1-imb) (recovers the C++ bids
  total from the real imbalance identity),
  best sizes / improve-level depth use the executable-depth proxies.
Every emitted event carries ``book_view_basis`` describing this mapping.

Known limitations of this bridge:
  - Top-of-book bids and per-level sizes are not exported by the C++ engine;
    leg1 quote placement and queue-depth estimates therefore rely on the
    documented proxies above. A future C++ book-top export should replace
    them (do not relax thresholds to compensate).
  - ``clock_skew_ms`` comes from venue-status.json (system NTP offset, the
    same source the directional strategies use); only when venue data is
    missing does the bridge fall back to the paired event's
    ``clob_source_timestamp_age_diagnostic``.
  - Episodes are in-memory; a bridge restart loses active episode state
    (written audit events remain the canonical record) and the machine
    resumes from IDLE.
"""
import json
import os
import time
from pathlib import Path

from .maker_accumulate import (
    MakerAccumulateInput,
    MakerAccumulateStateMachine,
    MakerBookSide,
    STRATEGY,
)

AUDIT_PATH = "logs/shadow-audit.jsonl"
STRATEGY_AUDIT_PATH = "logs/strategy-audit.jsonl"
STATE_PATH = "state/maker-shadow.json"
MARKET_PATH = "data/live_markets.json"
VENUE_PATH = "data/venue-status.json"

#: Counted by web_monitor as maker evaluation decisions.
DECISION_EVENT_TYPES = frozenset({
    "maker_episode_opened",
    "maker_episode_rejected",
})

BOOK_VIEW_BASIS = (
    "cpp_paired_eval_bridge: REAL=best_ask,ask_vwap,executable_ask_depth,"
    "total_ask_depth,book_imbalance,per_side_age_ms,books_synced,fee_rate,"
    "generation,session,sell_vwap_bid_view(split_sell events); "
    "DERIVED=best_bid(latest real sell_vwap, conservative; fallback "
    "1-opposite_best_ask complement, optimistic no-arb upper bound),"
    "bid_depth_total(from real imbalance identity),"
    "best_sizes/improve_depth(executable-depth proxies); "
    "clock_skew_ms=venue_asset system_ntp_offset (same source as the "
    "directional strategies); fallback=clob_source_timestamp_age_diagnostic"
)


def bid_view_max_age_seconds():
    """Maximum age of the split_sell bid-side VWAP view used as the best_bid
    proxy; older views fall back to the complement approximation."""
    return float(os.getenv("MAKER_SHADOW_BID_VIEW_MAX_AGE_SECONDS", "10"))


def maker_accumulate_enabled():
    """Explicit strategy enable flag (AGENTS.md §26). Default on, matching the
    repository convention that all shadow strategies run; set
    ``MAKER_ACCUMULATE_ENABLE=0`` to disable."""
    return os.getenv("MAKER_ACCUMULATE_ENABLE", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _num(event, key, default=0.0):
    value = event.get(key)
    if value is None:
        return float(default)
    return float(value)


def book_side_from_event(event, prefix, other_prefix, bid_vwap=None):
    """Build a MakerBookSide from a C++ paired_lock shadow_eval event.

    ``prefix``/``other_prefix`` are "up"/"down". ``bid_vwap`` is the latest
    real bid-side sell VWAP for the paired target size (from split_sell
    events) when fresh; it is a conservative best_bid proxy. Without it the
    complement approximation (1 - opposite best_ask) is used. See the module
    docstring for the full REAL vs DERIVED mapping. An empty ask side (no
    real best ask) is treated as not evaluable (fail closed)."""
    best_ask = _num(event, f"{prefix}_best_ask")
    other_best_ask = _num(event, f"{other_prefix}_best_ask")
    ask_depth = _num(event, f"{prefix}_available_depth")
    imbalance = max(-0.999999, min(0.999999, _num(event, f"{prefix}_book_imbalance")))
    bid_depth = ask_depth * (1.0 + imbalance) / (1.0 - imbalance)
    executable = _num(event, f"{prefix}_fill")
    other_executable = _num(event, f"{other_prefix}_fill")
    if bid_vwap is not None and 0.0 < bid_vwap < 1.0:
        best_bid = float(bid_vwap)
    else:
        best_bid = 1.0 - other_best_ask
    evaluable = 0.0 < best_ask < 1.0 and 0.0 < other_best_ask < 1.0
    return MakerBookSide(
        best_bid=max(0.0, min(1.0, round(best_bid, 5))),
        best_ask=best_ask,
        best_bid_size=other_executable,
        best_ask_size=executable,
        bid_depth_total=bid_depth,
        ask_depth_total=ask_depth,
        bid_depth_at_improve_level=other_executable,
        age_ms=_num(event, f"{prefix}_book_age_ms", 1e9),
        snapshot_received=evaluable,
    )


def row_from_event(event, market=None, bid_view=None, venue_asset=None):
    """Build a MakerAccumulateInput from a C++ paired_lock shadow_eval event.

    ``market`` is the optional live_markets.json record (used for
    active/tradable/min-order-size metadata when present). ``bid_view`` is
    the latest split_sell bid-side VWAP snapshot for this market, e.g.
    ``{"up_sell_vwap": ..., "down_sell_vwap": ...}``. ``venue_asset`` is the
    venue-status.json record for the asset; its ``clock_skew_ms`` (system NTP
    offset, the same source the directional strategies use) takes priority
    over the paired event's CLOB source-timestamp-age diagnostic."""
    market = market or {}
    bid_view = bid_view or {}
    venue_asset = venue_asset or {}
    timestamp = _num(event, "ts", event.get("timestamp") or time.time())
    close_ts = _num(event, "close_ts", market.get("close_ts") or 0.0)
    seconds_to_close = _num(
        event, "seconds_to_close", max(0.0, close_ts - timestamp))
    fee_rate = event.get("fee_rate")
    fee_rate = float(fee_rate) if fee_rate is not None else None
    market_id = str(event.get("market_id", ""))
    up_age = _num(event, "up_book_age_ms", 1e9)
    down_age = _num(event, "down_book_age_ms", 1e9)
    min_order_size = (
        event.get("market_minimum_size")
        or market.get("min_order_size")
        or 5.0
    )
    return MakerAccumulateInput(
        market_id=market_id,
        condition_id=str(event.get("condition_id", market_id)),
        asset=str(event.get("asset", market.get("asset", ""))),
        timeframe=str(event.get("timeframe", market.get("interval", ""))),
        window=str(event.get("window", market.get("window", "current"))),
        generation=int(event.get("subscription_generation",
                                 event.get("generation", 0))),
        session=str(event.get("ws_session_id", event.get("session", 0))),
        evaluation_sequence=int(event.get("evaluation_sequence", 0)),
        timestamp=timestamp,
        up=book_side_from_event(event, "up", "down",
                                bid_view.get("up_sell_vwap")),
        down=book_side_from_event(event, "down", "up",
                                  bid_view.get("down_sell_vwap")),
        book_skew_ms=abs(up_age - down_age),
        seconds_to_close=seconds_to_close,
        market_active=bool(market.get("active", True)) and close_ts > timestamp,
        market_tradable=bool(market.get("accepting_orders", True)),
        fee_schedule_available=fee_rate is not None and fee_rate > 0,
        taker_fee_rate=fee_rate if fee_rate and fee_rate > 0 else None,
        clock_skew_ms=(venue_asset.get("clock_skew_ms")
                       if venue_asset.get("clock_skew_ms") is not None
                       else event.get("clock_skew_ms")),
        reference=None,  # not exported by the paired event; REFERENCE ONLY anyway
        min_order_size=float(min_order_size),
        book_view_basis=BOOK_VIEW_BASIS,
    )


def _load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_state(path, state):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary, path)


def process_once(audit_path=AUDIT_PATH, output_path=STRATEGY_AUDIT_PATH,
                 state_path=STATE_PATH, market_path=MARKET_PATH, machine=None,
                 venue_path=VENUE_PATH):
    """Tail the C++ paired_lock audit stream once, evaluate new events through
    the maker state machine, and append emitted events to the strategy audit
    log. Returns the number of maker events written."""
    machine = machine or MakerAccumulateStateMachine()
    state = _load_json(state_path, {"offset": 0, "processed": []})
    processed = set(state.get("processed", []))
    markets = {
        row.get("market_id"): row
        for row in _load_json(market_path, {"markets": []}).get("markets", [])
    }
    venue_assets = _load_json(venue_path, {}).get("assets", {})
    audit_path, output_path, state_path = (
        Path(audit_path), Path(output_path), Path(state_path))
    if not audit_path.exists():
        return 0
    stat = audit_path.stat()
    identity = f"{stat.st_dev}:{stat.st_ino}"
    previous_identity = state.get("file_identity")
    if (previous_identity not in {None, identity}
            or stat.st_size < state.get("offset", 0)):
        state["offset"] = 0
    if previous_identity != identity and (previous_identity is not None
                                          or stat.st_size > 0):
        state["file_identity"] = identity
    emitted = 0
    bid_views = state.get("bid_views", {})
    max_bid_view_age = bid_view_max_age_seconds()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open(encoding="utf-8") as source, \
            output_path.open("a", encoding="utf-8") as target:
        source.seek(state.get("offset", 0))
        while line := source.readline():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if (event.get("event_type") == "shadow_split_sell_eval"
                    and event.get("strategy") == "split_sell_lock"):
                # Real bid-side sell VWAPs (conservative best_bid proxy).
                market_id = event.get("market_id")
                if market_id:
                    bid_views[market_id] = {
                        "up_sell_vwap": event.get("up_sell_vwap"),
                        "down_sell_vwap": event.get("down_sell_vwap"),
                        "ts": event.get("ts"),
                    }
                continue
            if (event.get("strategy") != "paired_lock"
                    or event.get("event_type") != "shadow_eval"):
                continue
            event_id = event.get("event_id")
            if not event_id or event_id in processed:
                continue
            bid_view = bid_views.get(event.get("market_id")) or {}
            view_ts = bid_view.get("ts")
            if view_ts is None or abs(_num(event, "ts") - _num(bid_view, "ts")) \
                    > max_bid_view_age:
                bid_view = {}
            row = row_from_event(
                event, markets.get(event.get("market_id")), bid_view,
                venue_assets.get(event.get("asset"), {}))
            evaluation = machine.evaluate(row)
            for maker_event in evaluation.events:
                # Producer-generated event_id is preserved verbatim (AGENTS.md
                # §14); downstream consumers must not regenerate it.
                target.write(json.dumps(
                    maker_event, separators=(",", ":"), sort_keys=True) + "\n")
                emitted += 1
            processed.add(event_id)
        offset = source.tell()
        if offset != state.get("offset"):
            state["offset"] = offset
        target.flush()
    state["processed"] = list(processed)[-20000:]
    state["bid_views"] = dict(sorted(
        bid_views.items(), key=lambda item: _num(item[1], "ts"))[-500:])
    state["statistics"] = machine.statistics()
    state["updated_at"] = time.time()
    _write_state(state_path, state)
    return emitted


def run(audit_path=AUDIT_PATH, output_path=STRATEGY_AUDIT_PATH,
        state_path=STATE_PATH, market_path=MARKET_PATH, poll_seconds=0.5,
        venue_path=VENUE_PATH):
    if not maker_accumulate_enabled():
        print("MAKER_SHADOW disabled MAKER_ACCUMULATE_ENABLE=0")
        return 0
    machine = MakerAccumulateStateMachine()
    print(f"MAKER_SHADOW start audit={audit_path} output={output_path} "
          f"config_hash={machine.config_hash}")
    while True:
        process_once(audit_path, output_path, state_path, market_path,
                     machine, venue_path)
        time.sleep(poll_seconds)


def main():
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
