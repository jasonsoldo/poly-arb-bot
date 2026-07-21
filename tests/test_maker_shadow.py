"""Integration tests for the maker_paired_accumulate runtime bridge
(poly_arb_bot.maker_shadow): synthetic C++ paired_lock/split_sell audit
streams drive the real state machine end to end; the bridge must write
canonical JSONL audit events with producer-stable event_ids, enforce
portfolio limits, and handle disconnect/session invalidation."""
import json

import pytest

from poly_arb_bot import maker_shadow, web_monitor
from poly_arb_bot.maker_accumulate import (
    COMPLETE,
    LEG1_CANCELLED,
    MakerAccumulateConfig,
    MakerAccumulateStateMachine,
    STRATEGY,
)

T0 = 1_784_600_000.0
MARKET_ID = "0xmarket1"
CONDITION_ID = "0xcond1"


def paired_event(seq, ts, up_ask, down_ask, up_fill=50.0, down_fill=50.0,
                 up_depth=500.0, down_depth=500.0, up_imb=-0.3, down_imb=0.3,
                 up_age=100.0, down_age=120.0, session=19, generation=2,
                 seconds_to_close=280.0, fee_rate=0.07, market_id=MARKET_ID):
    return {
        "ts": ts, "timestamp": ts,
        "event_id": f"run1:{generation}:{session}:{market_id}:{seq}",
        "run_id": "run1", "evaluation_sequence": seq,
        "event_type": "shadow_eval", "strategy": "paired_lock",
        "market_id": market_id, "condition_id": CONDITION_ID,
        "asset": "BTC", "timeframe": "5m", "window": "current",
        "close_ts": ts + seconds_to_close,
        "generation": generation, "session": session,
        "subscription_generation": generation, "ws_session_id": session,
        "decision": "REJECT", "reason": "net_cost_above_threshold",
        "seconds_to_close": seconds_to_close,
        "clock_skew_ms": 40.0,
        "book_age_ms": max(up_age, down_age),
        "up_book_age_ms": up_age, "down_book_age_ms": down_age,
        "books_synced": True,
        "up_best_ask": up_ask, "down_best_ask": down_ask,
        "up_vwap": up_ask, "down_vwap": down_ask,
        "up_fill": up_fill, "down_fill": down_fill,
        "up_available_depth": up_depth, "down_available_depth": down_depth,
        "up_book_imbalance": up_imb, "down_book_imbalance": down_imb,
        "up_fee": 0.001, "down_fee": 0.001, "fee_rate": fee_rate,
        "market_minimum_size": 5,
        "real_order_submissions": 0, "real_orders": 0, "real_fills": 0,
    }


def split_sell_event(seq, ts, up_sell, down_sell, session=19, generation=2,
                     market_id=MARKET_ID):
    return {
        "ts": ts, "timestamp": ts,
        "event_id": f"run1:{generation}:{session}:{market_id}:split_sell:{seq}",
        "event_type": "shadow_split_sell_eval", "strategy": "split_sell_lock",
        "market_id": market_id, "condition_id": CONDITION_ID,
        "asset": "BTC", "timeframe": "5m", "window": "current",
        "generation": generation, "session": session,
        "decision": "REJECT", "reason": "split_sell_profit_below_threshold",
        "up_sell_vwap": up_sell, "down_sell_vwap": down_sell,
        "up_book_age_ms": 100.0, "down_book_age_ms": 120.0,
        "real_order_submissions": 0, "real_orders": 0, "real_fills": 0,
    }


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(
        encoding="utf-8").splitlines() if line.strip()]


def run_bridge(tmp_path, rows, machine=None):
    audit = tmp_path / "shadow-audit.jsonl"
    output = tmp_path / "strategy-audit.jsonl"
    state = tmp_path / "state" / "maker-shadow.json"
    markets = tmp_path / "live_markets.json"
    write_jsonl(audit, rows)
    markets.write_text(json.dumps({"markets": [{
        "market_id": MARKET_ID, "condition_id": CONDITION_ID, "asset": "BTC",
        "interval": "5m", "window": "current", "active": True,
        "accepting_orders": True, "close_ts": T0 + 280.0,
        "min_order_size": 5,
    }]}), encoding="utf-8")
    emitted = maker_shadow.process_once(audit, output, state, markets, machine)
    return emitted, read_jsonl(output), machine


# ---------------------------------------------------------------------------
# Row construction (REAL vs DERIVED field mapping)
# ---------------------------------------------------------------------------

def test_row_from_event_maps_real_fields_and_records_basis():
    event = paired_event(1, T0, 0.09, 0.93)
    row = maker_shadow.row_from_event(
        event, bid_view={"up_sell_vwap": 0.06, "down_sell_vwap": 0.90})
    assert row.market_id == MARKET_ID
    assert row.condition_id == CONDITION_ID
    assert row.asset == "BTC" and row.timeframe == "5m" and row.window == "current"
    assert row.generation == 2 and row.session == "19"
    assert row.evaluation_sequence == 1 and row.timestamp == T0
    # REAL fields
    assert row.up.best_ask == 0.09 and row.down.best_ask == 0.93
    assert row.up.ask_depth_total == 500.0
    assert row.up.age_ms == 100.0 and row.down.age_ms == 120.0
    assert row.book_skew_ms == 20.0
    assert row.taker_fee_rate == 0.07 and row.fee_schedule_available
    assert row.clock_skew_ms == 40.0 and row.seconds_to_close == 280.0
    # DERIVED fields: conservative sell-VWAP bid proxy + imbalance identity
    assert row.up.best_bid == 0.06 and row.down.best_bid == 0.90
    assert row.up.bid_depth_total == pytest.approx(
        500.0 * 0.7 / 1.3, rel=1e-9)
    assert row.book_view_basis == maker_shadow.BOOK_VIEW_BASIS
    # complement fallback when no fresh bid view exists
    fallback = maker_shadow.row_from_event(event)
    assert fallback.up.best_bid == pytest.approx(1.0 - 0.93)
    assert fallback.down.best_bid == pytest.approx(1.0 - 0.09)


def test_row_from_event_fail_closed_on_empty_ask_side():
    event = paired_event(1, T0, 0.0, 0.93)
    row = maker_shadow.row_from_event(event)
    assert not row.up.snapshot_received
    machine = MakerAccumulateStateMachine()
    evaluation = machine.evaluate(row)
    assert evaluation.decision.decision == "REJECT"
    assert evaluation.decision.reason in {
        "waiting_up_snapshot", "books_not_ready",
    }


def test_row_from_event_prefers_venue_clock_skew():
    event = paired_event(1, T0, 0.09, 0.93)
    assert event["clock_skew_ms"] == 40.0  # CLOB diagnostic
    row = maker_shadow.row_from_event(event, venue_asset={"clock_skew_ms": 12.0})
    assert row.clock_skew_ms == 12.0
    fallback = maker_shadow.row_from_event(event)
    assert fallback.clock_skew_ms == 40.0


# ---------------------------------------------------------------------------
# Full episode through the bridge + audit file invariants
# ---------------------------------------------------------------------------

def episode_rows(session=19):
    """Open -> leg1 fill -> leg2 fill -> COMPLETE via synthetic book ticks."""
    return [
        split_sell_event(1, T0, 0.06, 0.90, session=session),
        paired_event(2, T0, 0.09, 0.93, session=session),          # open leg1
        split_sell_event(3, T0 + 1, 0.06, 0.90, session=session),
        paired_event(4, T0 + 1, 0.065, 0.93, session=session),     # leg1 fills
        split_sell_event(5, T0 + 2, 0.06, 0.90, session=session),
        paired_event(6, T0 + 2, 0.065, 0.905, session=session),    # leg2 fills
    ]


def test_full_episode_writes_canonical_audit_events(tmp_path):
    machine = MakerAccumulateStateMachine()
    emitted, events, _ = run_bridge(tmp_path, episode_rows(), machine)
    assert emitted == len(events) > 0
    event_types = [row["event_type"] for row in events]
    assert "maker_episode_opened" in event_types
    assert "maker_leg_filled" in event_types
    assert "maker_episode_completed" in event_types
    identity_fields = (
        "event_id", "event_type", "strategy", "market_id", "condition_id",
        "asset", "timeframe", "window", "generation", "session",
        "evaluation_sequence", "timestamp",
    )
    for row in events:
        for field in identity_fields:
            assert field in row, f"{field} missing in {row['event_type']}"
        assert row["strategy"] == STRATEGY
        assert row["real_order_submissions"] == 0
        assert row["real_orders"] == 0
        assert row["real_fills"] == 0
        assert row["book_view_basis"] == maker_shadow.BOOK_VIEW_BASIS
        assert row["reference_usage"].startswith("REFERENCE ONLY")
    completed = next(row for row in events
                     if row["event_type"] == "maker_episode_completed")
    assert completed["decision"] == "COMPLETE"
    assert completed["locked_profit"] is not None
    assert completed["episode_realized_pnl"] is not None
    stats = machine.statistics()
    assert stats["episodes_completed"] == 1
    assert stats["real_orders"] == 0 and stats["real_fills"] == 0


def test_event_ids_are_producer_stable_across_replay(tmp_path):
    rows = episode_rows()
    machine_a = MakerAccumulateStateMachine()
    _, events_a, _ = run_bridge(tmp_path / "a", rows, machine_a)
    machine_b = MakerAccumulateStateMachine()
    _, events_b, _ = run_bridge(tmp_path / "b", rows, machine_b)
    ids_a = [row["event_id"] for row in events_a]
    ids_b = [row["event_id"] for row in events_b]
    assert ids_a == ids_b
    assert len(set(ids_a)) == len(ids_a)


def test_process_once_offsets_and_dedupes(tmp_path):
    rows = episode_rows()
    machine = MakerAccumulateStateMachine()
    audit = tmp_path / "shadow-audit.jsonl"
    output = tmp_path / "strategy-audit.jsonl"
    state = tmp_path / "state" / "maker-shadow.json"
    markets = tmp_path / "live_markets.json"
    write_jsonl(audit, rows)
    markets.write_text(json.dumps({"markets": []}), encoding="utf-8")
    first = maker_shadow.process_once(audit, output, state, markets, machine)
    second = maker_shadow.process_once(audit, output, state, markets, machine)
    assert first > 0
    assert second == 0
    events = read_jsonl(output)
    assert len(events) == first
    assert len({row["event_id"] for row in events}) == len(events)


# ---------------------------------------------------------------------------
# Reject path, limits, circuit breaker, disconnect handling
# ---------------------------------------------------------------------------

def test_reject_events_carry_blocking_reasons_and_dedup(tmp_path):
    machine = MakerAccumulateStateMachine()
    rows = [
        paired_event(1, T0, 0.09, 0.93, seconds_to_close=100.0),  # flatten window
        paired_event(2, T0 + 1, 0.09, 0.93, seconds_to_close=99.0),
    ]
    emitted, events, _ = run_bridge(tmp_path, rows, machine)
    assert emitted == 1
    assert events[0]["event_type"] == "maker_episode_rejected"
    assert events[0]["decision"] == "REJECT"
    assert events[0]["reason"] == "outside_time_window"
    assert "outside_time_window" in events[0]["blocking_reasons"]


def test_portfolio_exposure_limit_blocks_open(tmp_path):
    config = MakerAccumulateConfig(max_total_exposure=0.01)
    machine = MakerAccumulateStateMachine(config)
    _, events, _ = run_bridge(tmp_path, episode_rows(), machine)
    assert [row["event_type"] for row in events] == ["maker_episode_rejected"]
    assert events[0]["reason"] == "portfolio_exposure_exceeded"


def test_orphan_circuit_breaker_trips_after_consecutive_losses(tmp_path):
    config = MakerAccumulateConfig(max_consecutive_orphans=1,
                                   circuit_cooldown_seconds=3600.0)
    machine = MakerAccumulateStateMachine(config)
    # Episode opens, leg1 fills, then the book moves against the orphan leg
    # until the orphan-loss cap forces an emergency flatten (CLOSED_WITH_LOSS).
    rows = [
        split_sell_event(1, T0, 0.06, 0.90),
        paired_event(2, T0, 0.09, 0.93),
        split_sell_event(3, T0 + 1, 0.06, 0.90),
        paired_event(4, T0 + 1, 0.065, 0.93),       # leg1 fills @0.07
        # leg2 max price = 1-0.07-0.005-0.005 = 0.92; keep down ask high so
        # leg2 never fills and force the orphan drawdown cap instead:
        # (1.0 - down_bid_proxy) marks leg1 bid at 0.03 -> loss 25*(0.07-0.03)=1.0
        split_sell_event(5, T0 + 2, 0.03, 0.90),
        paired_event(6, T0 + 2, 0.09, 0.93),
    ]
    _, events, _ = run_bridge(tmp_path, rows, machine)
    event_types = [row["event_type"] for row in events]
    assert "maker_episode_closed_with_loss" in event_types
    assert machine.consecutive_orphans == 1
    assert machine.circuit_open_until is not None
    # Next open attempt is blocked by the circuit breaker.
    rows2 = [
        split_sell_event(7, T0 + 3, 0.06, 0.90),
        paired_event(8, T0 + 3, 0.09, 0.93),
    ]
    _, events2, _ = run_bridge(tmp_path / "second", rows2, machine)
    assert [row["event_type"] for row in events2] == ["maker_episode_rejected"]
    assert events2[0]["reason"] == "orphan_circuit_breaker_open"


def test_session_change_cancels_working_leg1(tmp_path):
    machine = MakerAccumulateStateMachine()
    rows = [
        split_sell_event(1, T0, 0.06, 0.90),
        paired_event(2, T0, 0.09, 0.93),  # opens leg1 (working)
    ]
    run_bridge(tmp_path / "first", rows, machine)
    assert len(machine.episodes) == 1
    # Reconnect: new WS session arrives while leg1 is still working.
    rows2 = [
        split_sell_event(3, T0 + 5, 0.06, 0.90, session=20),
        paired_event(4, T0 + 5, 0.09, 0.93, session=20),
    ]
    _, events2, _ = run_bridge(tmp_path / "second", rows2, machine)
    event_types = [row["event_type"] for row in events2]
    assert "maker_leg1_cancelled" in event_types
    cancelled = next(row for row in events2
                     if row["event_type"] == "maker_leg1_cancelled")
    assert cancelled["reason"] == "books_lost_mid_episode"
    assert not machine.episodes
    # Late message from the OLD session must be dropped.
    rows3 = [paired_event(5, T0 + 6, 0.065, 0.93, session=19)]
    emitted3, events3, _ = run_bridge(tmp_path / "third", rows3, machine)
    assert emitted3 == 0 and events3 == []


# ---------------------------------------------------------------------------
# Web aggregation
# ---------------------------------------------------------------------------

def test_web_monitor_counts_maker_decisions(tmp_path):
    machine = MakerAccumulateStateMachine()
    rows = episode_rows() + [
        paired_event(7, T0 + 3, 0.09, 0.93, seconds_to_close=100.0),
    ]
    _, events, _ = run_bridge(tmp_path, rows, machine)
    assert any(row["event_type"] == "maker_episode_opened" for row in events)
    assert any(row["event_type"] == "maker_episode_rejected" for row in events)
    output = tmp_path / "strategy-audit.jsonl"
    web_monitor._STRATEGY_COUNT_CACHE.pop(str(output.resolve()), None)
    try:
        counts = web_monitor._strategy_counts((output,))
    finally:
        web_monitor._STRATEGY_COUNT_CACHE.pop(str(output.resolve()), None)
    maker = counts["maker_paired_accumulate"]
    assert maker["evaluations"] == 2
    assert maker["accepts"] == 1
    assert maker["rejections"] == 1
    # other strategies untouched
    assert counts["paired_lock"]["evaluations"] == 0


def test_strategy_audit_lines_are_complete_json_objects(tmp_path):
    machine = MakerAccumulateStateMachine()
    run_bridge(tmp_path, episode_rows(), machine)
    output = tmp_path / "strategy-audit.jsonl"
    for line in output.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)  # raises on a torn line
        assert row["event_id"].startswith("maker-")
