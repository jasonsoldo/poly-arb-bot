import math

import pytest

from poly_arb_bot.maker_accumulate import (
    CLOSED_WITH_LOSS,
    COMPLETE,
    DEFAULT_FORCE_FLATTEN_SECONDS,
    DEFAULT_LEG2_TIMEOUT_SECONDS,
    DEFAULT_MAX_ORPHAN_SECONDS,
    EMERGENCY_FLATTEN,
    HEDGING_DIRECTIONAL_EXIT,
    LEG1_CANCELLED,
    LEG1_WORKING,
    LEG2_WORKING,
    MakerAccumulateConfig,
    MakerAccumulateInput,
    MakerAccumulateStateMachine,
    MakerBookSide,
    PortfolioView,
    evaluate_maker_accumulate,
    leg2_max_price,
    select_leg1_side,
    taker_fee_total,
)

T0 = 1_700_100_000.0


def side(bid, ask, bid_size=100.0, ask_size=100.0, age=50.0, received=True,
         improve_depth=50.0):
    return MakerBookSide(
        best_bid=bid, best_ask=ask, best_bid_size=bid_size, best_ask_size=ask_size,
        bid_depth_total=bid_size * 3, ask_depth_total=ask_size * 3,
        bid_depth_at_improve_level=improve_depth, age_ms=age,
        snapshot_received=received,
    )


def make_input(ts=T0, up=None, down=None, **overrides):
    data = dict(
        market_id="m1", condition_id="c1", asset="BTC", timeframe="5m",
        window="current", generation=1, session="s1", evaluation_sequence=1,
        timestamp=ts,
        up=up if up is not None else side(0.40, 0.42),
        down=down if down is not None else side(0.56, 0.57),
        book_skew_ms=20.0, seconds_to_close=280.0,
        market_active=True, market_tradable=True, fee_schedule_available=True,
        taker_fee_rate=0.07, clock_skew_ms=5.0,
    )
    data.update(overrides)
    return MakerAccumulateInput(**data)


def machine(**config_overrides):
    config = MakerAccumulateConfig(**config_overrides) if config_overrides \
        else MakerAccumulateConfig()
    return MakerAccumulateStateMachine(config)


def events_of(result, event_type):
    return [event for event in result.events if event["event_type"] == event_type]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_config_defaults_match_phase0_research():
    config = MakerAccumulateConfig()
    assert config.buffer_per_share == 0.005
    assert config.min_expected_locked_margin == 0.005
    assert config.min_hedge_exit_margin == -0.01
    assert config.max_orphan_seconds == {"5m": 90.0, "15m": 240.0, "1h": 360.0, "4h": 600.0}
    assert config.max_order_size == 25.0
    assert config.min_book_depth == 10.0
    assert config.max_spread_to_join == 0.01
    assert config.leg2_improve_interval_ms["5m"] == 1500.0
    assert config.leg2_improve_interval_ms["1h"] == 4000.0
    assert config.leg2_max_improves == 5
    assert config.shadow_fill_mode == "strict"


def test_config_from_env_overrides(monkeypatch):
    monkeypatch.setenv("MAKER_ACCUMULATE_BUFFER_PER_SHARE", "0.007")
    monkeypatch.setenv("MAKER_ACCUMULATE_MAX_ORPHAN_5M", "120")
    monkeypatch.setenv("MAKER_ACCUMULATE_FORCE_FLATTEN_5M", "400")
    monkeypatch.setenv("MAKER_ACCUMULATE_MAX_ORDER_SIZE", "50")
    monkeypatch.setenv("MAKER_ACCUMULATE_SHADOW_FILL_MODE", "queue")
    config = MakerAccumulateConfig.from_env()
    assert config.buffer_per_share == 0.007
    assert config.max_orphan_seconds["5m"] == 120.0
    assert config.max_orphan_seconds["15m"] == 240.0
    assert config.force_flatten_seconds["5m"] == 400.0
    assert config.max_order_size == 50.0
    assert config.shadow_fill_mode == "queue"
    assert config.config_hash() != MakerAccumulateConfig().config_hash()


def test_config_validates_force_flatten_window():
    with pytest.raises(ValueError):
        MakerAccumulateConfig(
            force_flatten_seconds={**DEFAULT_FORCE_FLATTEN_SECONDS, "5m": 100.0})
    with pytest.raises(ValueError):
        MakerAccumulateConfig(shadow_fill_mode="optimistic")


def test_taker_fee_matches_cpp_rounding():
    assert taker_fee_total(0.57, 25.0, 0.07) == math.floor(25 * 0.07 * 0.57 * 0.43 * 1e5 + 0.5) / 1e5
    assert taker_fee_total(0.5, 0.0, 0.07) == 0.0


# ---------------------------------------------------------------------------
# Leg1 open evaluation (pure function)
# ---------------------------------------------------------------------------

def test_open_accept_with_margins():
    result = evaluate_maker_accumulate(make_input())
    assert result.decision == "ACCEPT"
    assert result.leg1_outcome == "Up"
    assert result.leg1_quote_mode == "improve"
    assert result.leg1_quote_price == 0.41
    assert result.expected_margin == pytest.approx(1 - (0.41 + 0.56 + 0.005))
    assert result.real_order_submissions == 0


def test_reject_reasons_are_explicit():
    base = make_input()
    cases = [
        (dict(up=side(0.4, 0.42, received=False), down=side(0.56, 0.57, received=False)), "books_not_ready"),
        (dict(up=side(0.4, 0.42, received=False)), "waiting_up_snapshot"),
        (dict(down=side(0.56, 0.57, received=False)), "waiting_down_snapshot"),
        (dict(up=side(0.4, 0.42, age=800)), "up_book_stale"),
        (dict(down=side(0.56, 0.57, age=800)), "down_book_stale"),
        (dict(book_skew_ms=300.0), "books_not_synced"),
        (dict(fee_schedule_available=False), "fee_schedule_unavailable"),
        (dict(market_tradable=False), "market_not_tradable"),
        (dict(seconds_to_close=0.0), "market_expired"),
        (dict(seconds_to_close=200.0), "outside_time_window"),
        (dict(clock_skew_ms=None), "clock_skew_unavailable"),
        (dict(clock_skew_ms=500.0), "clock_skew_exceeded"),
        (dict(up=side(0.01, 0.02), down=side(0.97, 0.98)), "leg1_extremity_exceeded"),
        (dict(down=side(0.59, 0.60)), "expected_margin_below_threshold"),
        (dict(down=side(0.50, 0.60)), "hedge_exit_margin_below_threshold"),
        (dict(up=side(0.40, 0.42, improve_depth=5.0)), "book_depth_insufficient"),
    ]
    for overrides, reason in cases:
        result = evaluate_maker_accumulate(make_input(**overrides))
        assert result.decision == "REJECT", reason
        assert result.reason == reason, (reason, result.blocking_reasons)
        assert reason in result.blocking_reasons
    assert base.condition_id == "c1"


def test_join_queue_too_deep_rejects():
    deep_bid = MakerBookSide(
        best_bid=0.40, best_ask=0.41, best_bid_size=10000.0, best_ask_size=100.0,
        bid_depth_total=300.0, ask_depth_total=300.0,
        bid_depth_at_improve_level=50.0, age_ms=50.0,
    )
    row = make_input(up=deep_bid, down=side(0.57, 0.58))
    result = evaluate_maker_accumulate(row)
    assert result.reason == "leg1_queue_too_deep"
    assert result.leg1_quote_mode == "join"


def test_orphan_estimate_and_portfolio_caps():
    row = make_input()
    tight_orphan = MakerAccumulateConfig(max_orphan_loss_usd=0.5)
    assert evaluate_maker_accumulate(row, tight_orphan).reason == "orphan_loss_estimate_exceeded"
    tight_exposure = MakerAccumulateConfig(max_total_exposure=5.0)
    assert evaluate_maker_accumulate(row, tight_exposure).reason == "portfolio_exposure_exceeded"
    view = PortfolioView(daily_loss=10.0)
    assert evaluate_maker_accumulate(row, portfolio=view).reason == "daily_loss_limit_reached"
    view = PortfolioView(circuit_open=True)
    assert evaluate_maker_accumulate(row, portfolio=view).reason == "orphan_circuit_breaker_open"
    view = PortfolioView(episodes_in_market=3)
    assert evaluate_maker_accumulate(row, portfolio=view).reason == "max_episodes_per_market_reached"


def test_side_selection_prefers_extremity_and_tiebreaks_on_depth():
    config = MakerAccumulateConfig()
    row = make_input(up=side(0.48, 0.50), down=side(0.40, 0.44))
    outcome, scores, gap = select_leg1_side(row, config)
    assert outcome == "Down"
    # near-symmetric books: score gap below threshold -> deeper bid wins
    up_side = MakerBookSide(0.40, 0.42, 200.0, 100.0, 300.0, 300.0, 50.0, 50.0)
    down_side = MakerBookSide(0.58, 0.60, 100.0, 100.0, 300.0, 300.0, 50.0, 50.0)
    row = make_input(up=up_side, down=down_side)
    outcome, scores, gap = select_leg1_side(row, config)
    assert gap < config.leg1_side_min_score_gap
    assert outcome == "Up"


def test_join_vs_improve_quote_modes():
    joined = evaluate_maker_accumulate(make_input(up=side(0.40, 0.41), down=side(0.57, 0.58)))
    assert joined.decision == "ACCEPT"
    assert joined.leg1_quote_mode == "join"
    assert joined.leg1_quote_price == 0.40
    improved = evaluate_maker_accumulate(make_input(up=side(0.40, 0.43), down=side(0.56, 0.57)))
    assert improved.leg1_quote_mode == "improve"
    assert improved.leg1_quote_price == 0.41
    # never crosses the spread even with wide books
    wide = evaluate_maker_accumulate(make_input(up=side(0.30, 0.50), down=side(0.48, 0.52)))
    assert wide.leg1_quote_price <= 0.50 - 0.01


# ---------------------------------------------------------------------------
# State machine: open and episode identity
# ---------------------------------------------------------------------------

def test_state_machine_opens_episode_and_blocks_second_one():
    sm = machine()
    result = sm.evaluate(make_input())
    assert result.decision.decision == "ACCEPT"
    assert result.decision.state == LEG1_WORKING
    opened = events_of(result, "maker_episode_opened")
    assert len(opened) == 1
    event = opened[0]
    assert event["strategy"] == "maker_paired_accumulate"
    assert event["leg1_outcome"] == "Up"
    assert event["leg1_quote_price"] == 0.41
    assert event["expected_margin"] is not None
    assert event["reference_usage"].startswith("REFERENCE ONLY")
    assert event["real_order_submissions"] == 0 and event["real_orders"] == 0
    # second evaluation on the same condition does not open a new episode
    again = sm.evaluate(make_input(ts=T0 + 1))
    assert again.decision.decision == "WORKING"
    assert not events_of(again, "maker_episode_opened")
    stats = sm.statistics()
    assert stats["episodes_opened"] == 1
    assert stats["active_episodes"] == 1
    assert stats["real_order_submissions"] == 0


def test_reject_events_are_deduplicated_by_reason():
    sm = machine()
    row = make_input(fee_schedule_available=False)
    first = sm.evaluate(row)
    assert first.decision.reason == "fee_schedule_unavailable"
    assert len(events_of(first, "maker_episode_rejected")) == 1
    second = sm.evaluate(make_input(ts=T0 + 1, fee_schedule_available=False))
    assert not events_of(second, "maker_episode_rejected")


def test_event_identity_is_stable_within_episode():
    sm = machine()
    opened = sm.evaluate(make_input())
    episode_id = opened.decision.episode_id
    filled = sm.evaluate(make_input(ts=T0 + 1, up=side(0.39, 0.40)))
    ids = {event["episode_id"] for event in filled.events}
    assert ids == {episode_id}
    event_ids = [event["event_id"] for event in (*opened.events, *filled.events)]
    assert len(event_ids) == len(set(event_ids))
    for event in filled.events:
        assert event["generation"] == 1 and event["session"] == "s1"
        assert event["config_hash"] == sm.config_hash


# ---------------------------------------------------------------------------
# Fill simulation: strict vs queue
# ---------------------------------------------------------------------------

def open_and_work(sm, ts=T0):
    result = sm.evaluate(make_input(ts=ts))
    assert result.decision.decision == "ACCEPT"
    return result


def test_strict_mode_touch_does_not_fill():
    sm = machine()
    open_and_work(sm)
    touched = sm.evaluate(make_input(ts=T0 + 1, up=side(0.39, 0.41)))
    assert not events_of(touched, "maker_leg_filled")
    assert sm.episodes["c1"].state == LEG1_WORKING


def test_queue_mode_touch_fills_and_labels_configured_model():
    sm = machine(shadow_fill_mode="queue")
    open_and_work(sm)
    touched = sm.evaluate(make_input(ts=T0 + 1, up=side(0.39, 0.41)))
    fills = events_of(touched, "maker_leg_filled")
    assert len(fills) == 1
    assert fills[0]["fill_mode"] == "queue"
    assert fills[0]["strict_would_fill"] is False
    assert fills[0]["queue_would_fill"] is True
    assert fills[0]["fill_probability_model"] == "configured_queue_model"


def fill_leg1(sm, ts=T0 + 1, up_book=None, down_book=None, expected_state=LEG2_WORKING):
    up_book = up_book or side(0.39, 0.40)
    result = sm.evaluate(make_input(ts=ts, up=up_book, down=down_book or side(0.56, 0.57)))
    if expected_state is not None:
        assert sm.episodes["c1"].state == expected_state
    return result


def test_strict_crossing_fills_leg1_and_opens_leg2():
    sm = machine()
    open_and_work(sm)
    result = fill_leg1(sm)
    episode = sm.episodes["c1"]
    assert episode.leg1_avg_price == 0.41
    assert episode.leg1_filled_size == 25.0
    assert episode.state == LEG2_WORKING
    assert episode.leg2_max_price == pytest.approx(leg2_max_price(0.41, sm.config))
    assert episode.leg2_max_price == pytest.approx(0.58)
    fills = events_of(result, "maker_leg_filled")
    assert fills[0]["fill_mode"] == "strict"
    transitions = events_of(result, "maker_episode_state_change")
    assert [(t["state_from"], t["state_to"]) for t in transitions] == [
        (LEG1_WORKING, "LEG1_FILLED"), ("LEG1_FILLED", LEG2_WORKING)]


def test_partial_leg1_fill_below_ratio_keeps_working():
    sm = machine()
    open_and_work(sm)
    partial = sm.evaluate(make_input(ts=T0 + 1, up=side(0.39, 0.40, ask_size=10.0)))
    episode = sm.episodes["c1"]
    assert episode.state == LEG1_WORKING
    assert episode.leg1_filled_size == 10.0
    rest = sm.evaluate(make_input(ts=T0 + 2, up=side(0.39, 0.40, ask_size=50.0)))
    assert sm.episodes["c1"].state == LEG2_WORKING
    assert sm.episodes["c1"].leg1_filled_size == 25.0


# ---------------------------------------------------------------------------
# Leg2: max price, improve loop, maker complete
# ---------------------------------------------------------------------------

def test_leg2_maker_complete_cost_chain_and_rebate_excluded():
    sm = machine()
    open_and_work(sm)
    fill_leg1(sm)
    result = sm.evaluate(make_input(ts=T0 + 2, up=side(0.39, 0.40), down=side(0.55, 0.55)))
    assert result.decision.state == COMPLETE
    completed = events_of(result, "maker_episode_completed")
    assert len(completed) == 1
    event = completed[0]
    assert event["gross_cost"] == pytest.approx(0.41 + 0.56)
    assert event["maker_fees"] == 0.0
    assert event["hedge_taker_fee"] == 0.0
    assert event["buffer_per_share"] == 0.005
    assert event["gas_cost_per_share"] == 0.0001
    assert event["net_cost"] == pytest.approx(0.97 + 0.0001 + 0.005)
    assert event["guaranteed_payout"] == 1.0
    assert event["locked_profit"] == pytest.approx(1 - event["net_cost"])
    assert event["locked_roi"] == pytest.approx(event["locked_profit"] / event["net_cost"])
    assert event["locked_size"] == 25.0 and event["at_risk_size"] == 0.0
    # ESTIMATED REBATE is reported but never counted into realized PnL
    assert event["estimated_rebate"] > 0
    assert "ESTIMATED REBATE" in event["estimated_rebate_label"]
    assert event["realized_rebate"] == 0.0
    assert event["episode_realized_pnl"] == pytest.approx(25 * 0.03 - 25 * 2 * 0.0001)
    assert event["exit_path"] == "maker_complete"
    stats = sm.statistics()
    assert stats["episodes_completed"] == 1
    assert stats["realized_shadow_pnl"] == pytest.approx(event["episode_realized_pnl"])


def test_leg2_improve_loop_follows_book_capped_by_max_price():
    sm = machine()
    open_and_work(sm)
    fill_leg1(sm, down_book=side(0.55, 0.58))
    episode = sm.episodes["c1"]
    assert episode.leg2_quote_price == pytest.approx(0.56)  # min(bid+tick, ask-tick, max)
    result = sm.evaluate(make_input(ts=T0 + 3, up=side(0.39, 0.40), down=side(0.57, 0.60)))
    updates = [e for e in events_of(result, "maker_quote_updated")
               if e["quote_reason"] == "improve_loop"]
    assert len(updates) == 1
    # 5m step = 2 ticks, but capped at leg2_max_price = 0.58
    assert updates[0]["new_quote_price"] == pytest.approx(0.58)
    assert updates[0]["improve_attempt"] == 1
    assert updates[0]["max_improves"] == 5


def test_leg2_improves_exhausted_falls_to_directional_exit_when_hedge_fails():
    sm = machine(leg2_max_improves=1)
    open_and_work(sm)
    fill_leg1(sm)
    sm.evaluate(make_input(ts=T0 + 3, up=side(0.38, 0.40), down=side(0.57, 0.63)))
    result = sm.evaluate(make_input(ts=T0 + 6, up=side(0.38, 0.40), down=side(0.57, 0.63)))
    episode = sm.episodes["c1"]
    assert episode.state == HEDGING_DIRECTIONAL_EXIT
    assert episode.terminal_reason == "leg2_improves_exhausted"
    transitions = events_of(result, "maker_episode_state_change")
    assert any(t["state_to"] == HEDGING_DIRECTIONAL_EXIT for t in transitions)


def test_leg2_max_price_below_bid_abandons_maker_path():
    sm = machine()
    open_and_work(sm)
    result = fill_leg1(sm, down_book=side(0.58, 0.62),
                       expected_state=HEDGING_DIRECTIONAL_EXIT)
    episode = sm.episodes["c1"]
    # leg2_max_price 0.58 <= best bid 0.58 -> maker path abandoned at leg2 open
    assert episode.state == HEDGING_DIRECTIONAL_EXIT
    assert episode.terminal_reason == "leg2_max_price_below_bid"


def test_leg2_timeout_taker_hedge_completes_pair():
    sm = machine()
    open_and_work(sm)
    fill_leg1(sm)
    result = sm.evaluate(make_input(ts=T0 + 50, up=side(0.39, 0.40), down=side(0.56, 0.57)))
    assert result.decision.state == COMPLETE
    completed = events_of(result, "maker_episode_completed")[0]
    assert completed["exit_path"] == "taker_hedge"
    assert completed["reason"] == "leg2_timeout"
    assert completed["hedge_taker_fee_rounded"] > 0
    assert completed["leg2_avg_price"] == pytest.approx(0.57)
    expected_pnl = 25 * (1 - 0.98) - completed["hedge_taker_fee_rounded"] - 25 * 2 * 0.0001
    assert completed["episode_realized_pnl"] == pytest.approx(expected_pnl)


def test_orphan_seconds_cap_enters_exit_branches():
    sm = machine(
        leg2_timeout_seconds={**DEFAULT_LEG2_TIMEOUT_SECONDS, "5m": 100.0},
        max_orphan_seconds={**DEFAULT_MAX_ORPHAN_SECONDS, "5m": 90.0},
    )
    open_and_work(sm)
    fill_leg1(sm)
    result = sm.evaluate(make_input(ts=T0 + 96, up=side(0.39, 0.40), down=side(0.56, 0.57)))
    assert result.decision.state == COMPLETE  # hedge branch still viable
    assert events_of(result, "maker_episode_completed")[0]["reason"] == "orphan_seconds_exceeded"


def test_orphan_loss_cap_triggers_emergency_flatten():
    sm = machine()
    open_and_work(sm)
    fill_leg1(sm)
    result = sm.evaluate(make_input(ts=T0 + 5, up=side(0.36, 0.38), down=side(0.56, 0.57)))
    assert result.decision.state == CLOSED_WITH_LOSS
    closed = events_of(result, "maker_episode_closed_with_loss")[0]
    assert closed["reason"] == "orphan_loss_limit_exceeded"
    assert closed["exit_path"] == "emergency_flatten"
    assert closed["exit_vwap"] == 0.36
    assert closed["exit_taker_fee"] > 0
    transitions = events_of(result, "maker_episode_state_change")
    assert any(t["state_to"] == EMERGENCY_FLATTEN for t in transitions)


# ---------------------------------------------------------------------------
# Directional exit and emergency flatten
# ---------------------------------------------------------------------------

def enter_directional_exit(sm):
    open_and_work(sm)
    fill_leg1(sm)
    result = sm.evaluate(make_input(ts=T0 + 50, up=side(0.38, 0.40), down=side(0.57, 0.63)))
    assert sm.episodes["c1"].state == HEDGING_DIRECTIONAL_EXIT
    return result


def test_directional_exit_maker_sell_fill():
    sm = machine()
    enter_directional_exit(sm)
    episode = sm.episodes["c1"]
    assert episode.exit_quote_price == pytest.approx(max(0.41 - 0.03, 0.38 + 0.01))
    result = sm.evaluate(make_input(ts=T0 + 55, up=side(0.41, 0.43), down=side(0.55, 0.63)))
    assert result.decision.state == CLOSED_WITH_LOSS
    closed = events_of(result, "maker_episode_closed_with_loss")[0]
    assert closed["exit_path"] == "directional_exit"
    assert closed["exit_vwap"] == pytest.approx(episode.exit_quote_price)
    assert closed["exit_taker_fee"] == 0.0
    assert closed["episode_realized_pnl"] == pytest.approx(
        25 * (closed["exit_vwap"] - 0.41) - 25 * 0.0001)


def test_directional_exit_timeout_taker_sell():
    sm = machine()
    enter_directional_exit(sm)
    result = sm.evaluate(make_input(ts=T0 + 120, up=side(0.38, 0.40), down=side(0.55, 0.63)))
    assert result.decision.state == CLOSED_WITH_LOSS
    closed = events_of(result, "maker_episode_closed_with_loss")[0]
    assert closed["reason"] == "directional_exit_timeout"
    assert closed["exit_path"] == "directional_exit"
    assert closed["exit_taker_fee"] > 0  # taker sell after directional_exit_timeout
    assert closed["exit_vwap"] == 0.38


def test_force_flatten_window_cancels_working_leg1():
    sm = machine()
    open_and_work(sm)
    result = sm.evaluate(make_input(ts=T0 + 5, seconds_to_close=200.0))
    assert result.decision.state == LEG1_CANCELLED
    cancelled = events_of(result, "maker_leg1_cancelled")[0]
    assert cancelled["reason"] == "emergency_flatten_window"


def test_force_flatten_window_flattens_holding_episode():
    sm = machine()
    open_and_work(sm)
    fill_leg1(sm)
    result = sm.evaluate(make_input(ts=T0 + 10, up=side(0.39, 0.41),
                                    seconds_to_close=200.0))
    assert result.decision.state == CLOSED_WITH_LOSS
    closed = events_of(result, "maker_episode_closed_with_loss")[0]
    assert closed["reason"] == "emergency_flatten_window"
    assert closed["exit_path"] == "emergency_flatten"


def test_market_expired_mid_episode():
    sm = machine()
    open_and_work(sm)
    cancelled = sm.evaluate(make_input(ts=T0 + 5, seconds_to_close=0.0))
    assert cancelled.decision.state == LEG1_CANCELLED
    assert cancelled.decision.reason == "market_expired_mid_episode"

    sm2 = machine()
    open_and_work(sm2)
    fill_leg1(sm2)
    flattened = sm2.evaluate(make_input(ts=T0 + 10, up=side(0.39, 0.41),
                                        seconds_to_close=0.0))
    assert flattened.decision.state == CLOSED_WITH_LOSS
    assert flattened.decision.reason == "market_expired_mid_episode"


def test_leg1_timeout_cancels_without_position():
    sm = machine()
    open_and_work(sm)
    result = sm.evaluate(make_input(ts=T0 + 70))
    assert result.decision.state == LEG1_CANCELLED
    assert result.decision.reason == "leg1_timeout"
    stats = sm.statistics()
    assert stats["episodes_cancelled"] == 1
    assert stats["realized_shadow_pnl"] == 0.0


def test_leg1_margin_deterioration_cancels():
    sm = machine()
    open_and_work(sm)
    result = sm.evaluate(make_input(ts=T0 + 5, down=side(0.59, 0.60)))
    assert result.decision.state == LEG1_CANCELLED
    assert result.decision.reason == "expected_margin_below_threshold"


# ---------------------------------------------------------------------------
# Generation / session binding
# ---------------------------------------------------------------------------

def test_generation_change_cancels_unfilled_leg1():
    sm = machine()
    open_and_work(sm)
    result = sm.evaluate(make_input(ts=T0 + 5, generation=2, session="s2"))
    assert result.decision.state == LEG1_CANCELLED
    assert result.decision.reason == "books_lost_mid_episode"


def test_generation_change_flattens_holding_episode():
    sm = machine()
    open_and_work(sm)
    fill_leg1(sm)
    result = sm.evaluate(make_input(ts=T0 + 5, generation=2, session="s2",
                                    up=side(0.38, 0.40)))
    assert result.decision.state == CLOSED_WITH_LOSS
    assert result.decision.reason == "books_lost_mid_episode"
    closed = events_of(result, "maker_episode_closed_with_loss")[0]
    assert closed["generation"] == 2 and closed["session"] == "s2"


def test_late_message_from_old_session_is_dropped():
    sm = machine()
    open_and_work(sm)
    sm.evaluate(make_input(ts=T0 + 5, generation=2, session="s2"))  # cancels episode
    late = sm.evaluate(make_input(ts=T0 + 6, generation=1, session="s1"))
    assert late.decision.decision == "REJECT"
    assert late.decision.reason == "stale_message_dropped"
    assert sm.statistics()["episodes_opened"] == 1  # no phantom episode from late data


# ---------------------------------------------------------------------------
# Risk: circuit breaker, daily loss, statistics semantics
# ---------------------------------------------------------------------------

def run_losing_episode(sm, ts, condition):
    opened = sm.evaluate(make_input(ts=ts, condition_id=condition))
    assert opened.decision.decision == "ACCEPT"
    sm.evaluate(make_input(ts=ts + 1, condition_id=condition, up=side(0.39, 0.40)))
    closed = sm.evaluate(make_input(ts=ts + 5, condition_id=condition,
                                    up=side(0.36, 0.38)))
    assert closed.decision.state == CLOSED_WITH_LOSS


def test_orphan_circuit_breaker_opens_and_cools_down():
    sm = machine(max_daily_loss=100.0, max_episodes_per_market_window=10)
    run_losing_episode(sm, T0, "c1")
    run_losing_episode(sm, T0 + 10, "c1")
    run_losing_episode(sm, T0 + 20, "c1")
    assert sm.consecutive_orphans == 3
    blocked = sm.evaluate(make_input(ts=T0 + 30, condition_id="c9", market_id="m9"))
    assert blocked.decision.decision == "REJECT"
    assert blocked.decision.reason == "orphan_circuit_breaker_open"
    cooled = sm.evaluate(make_input(ts=T0 + 30 + 3601, condition_id="c9", market_id="m9"))
    assert cooled.decision.decision == "ACCEPT"


def test_daily_loss_limit_blocks_new_episodes():
    sm = machine(max_daily_loss=1.0, max_episodes_per_market_window=10)
    run_losing_episode(sm, T0, "c1")  # loss >> 1.0
    blocked = sm.evaluate(make_input(ts=T0 + 10, condition_id="c9", market_id="m9"))
    assert blocked.decision.reason == "daily_loss_limit_reached"


def test_statistics_na_semantics_and_identity_invariants():
    sm = machine()
    stats = sm.statistics()
    assert stats["leg1_fill_rate"] is None
    assert stats["average_locked_profit"] is None
    assert stats["average_orphan_loss"] is None
    assert stats["realized_shadow_pnl"] == 0.0
    assert stats["real_orders"] == 0 and stats["real_fills"] == 0

    sm = machine(max_daily_loss=100.0, max_episodes_per_market_window=10)
    open_and_work(sm)  # active
    run_losing_episode(sm, T0 + 10, "c2")
    stats = sm.statistics()
    assert stats["episodes_opened"] == (
        stats["episodes_completed"] + stats["episodes_cancelled"]
        + stats["episodes_closed_with_loss"] + stats["active_episodes"])
    assert stats["orphan_rate"] == 1.0
    assert stats["leg2_completion_rate"] == 0.0
    assert stats["average_orphan_loss"] < 0
