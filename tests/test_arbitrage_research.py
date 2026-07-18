import json

from poly_arb_bot.arbitrage_research import (
    IncrementalArbitrageResearch,
    _mean_confidence_interval,
    _wilson_interval,
)


def _row(event_id, decision, ts, session=1, reason="opportunity"):
    return {
        "ts": ts,
        "event_id": event_id,
        "event_type": "shadow_eval",
        "strategy": "paired_lock",
        "market_id": "m1",
        "asset": "BTC",
        "timeframe": "5m",
        "generation": 2,
        "session": session,
        "decision": decision,
        "reason": reason,
        "fok": True,
        "up_fee": 0.01,
        "down_fee": 0.01,
        "locked_profit": 0.05 if decision == "ACCEPT" else -0.01,
        "expected_execution_value": 0.03 if decision == "ACCEPT" else -0.02,
        "duration_ms": 20,
        "size": 10,
        "time_between_legs_us": 50_000,
    }


def test_repeated_accepts_are_one_episode_until_requalification(tmp_path):
    audit = tmp_path / "audit.jsonl"
    rows = [
        _row("a1", "ACCEPT", 1),
        _row("a2", "ACCEPT", 2),
        _row("r1", "REJECT", 3, reason="net_cost_above_threshold"),
        _row("a3", "ACCEPT", 4),
        _row("a4", "ACCEPT", 5, session=2),
    ]
    audit.write_text("".join(json.dumps(row) + "\n" for row in rows))

    result = IncrementalArbitrageResearch(audit).refresh()

    funnel = result["funnels"]["paired_lock"]
    assert funnel == {
        "evaluations": 5,
        "depth_passed": 5,
        "fee_passed": 4,
        "latency_survived": 4,
        "independent_episodes": 3,
        "shadow_attempts": 0,
        "leg_1_book_executable": 0,
        "both_legs_book_executable": 0,
        "orphaned": 0,
        "invalidated": 0,
        "completed": 0,
        "positive_completed": 0,
    }


def test_repeatable_candidate_requires_three_distinct_markets(tmp_path):
    audit = tmp_path / "audit.jsonl"
    rows = []
    for index, market_id in enumerate(("m1", "m2", "m3"), 1):
        row = _row(f"a{index}", "ACCEPT", index)
        row["market_id"] = market_id
        row["duration_ms"] = 10 * index
        rows.append(row)
    audit.write_text("".join(json.dumps(row) + "\n" for row in rows))

    result = IncrementalArbitrageResearch(audit).refresh()

    pattern = result["repeatable_patterns"][0]
    assert pattern["strategy"] == "paired_lock"
    assert pattern["independent_episodes"] == 3
    assert pattern["distinct_close_windows"] == 3
    assert pattern["classification"] == "OBSERVED"
    assert pattern["duration_ms"]["p50"] == 20


def test_completed_lifecycle_is_separate_from_evaluations(tmp_path):
    audit = tmp_path / "audit.jsonl"
    execution = tmp_path / "execution.jsonl"
    audit.write_text(json.dumps(_row("a1", "ACCEPT", 1)) + "\n")
    execution.write_text(json.dumps({
        "ts": 2,
        "event_id": "complete-1",
        "event_type": "shadow_complete",
        "strategy": "paired_lock",
        "market_id": "m1",
        "asset": "BTC",
        "timeframe": "5m",
        "realized_simulated_pnl": 0.04,
    }) + "\n")

    result = IncrementalArbitrageResearch(audit, execution).refresh()

    funnel = result["funnels"]["paired_lock"]
    assert funnel["evaluations"] == 1
    assert funnel["completed"] == 1
    assert funnel["positive_completed"] == 1
    assert result["repeatable_patterns"][0]["completed"] == 1
    assert result["repeatable_patterns"][0]["simulated_pnl"] == 0.04


def test_completion_joins_legacy_episode_by_market_id_and_enriches_metadata(tmp_path):
    audit = tmp_path / "audit.jsonl"
    execution = tmp_path / "execution.jsonl"
    legacy = _row("a1", "ACCEPT", 1)
    legacy.pop("asset")
    legacy.pop("timeframe")
    audit.write_text(json.dumps(legacy) + "\n")
    execution.write_text(json.dumps({
        "ts": 2,
        "event_id": "complete-1",
        "event_type": "shadow_complete",
        "strategy": "paired_lock",
        "market_id": "m1",
        "asset": "BTC",
        "timeframe": "5m",
        "realized_simulated_pnl": 0.13875,
    }) + "\n")

    result = IncrementalArbitrageResearch(audit, execution).refresh()

    assert len(result["repeatable_patterns"]) == 1
    pattern = result["repeatable_patterns"][0]
    assert pattern["asset"] == "BTC"
    assert pattern["timeframe"] == "5m"
    assert pattern["independent_episodes"] == 1
    assert pattern["completed"] == 1
    assert pattern["positive_completed"] == 1
    assert pattern["simulated_pnl"] == 0.13875


def test_persisted_split_pattern_is_migrated_without_future_duplication(tmp_path):
    audit = tmp_path / "audit.jsonl"
    state = tmp_path / "state.json"
    audit.write_text(json.dumps(_row("a2", "ACCEPT", 2)) + "\n")
    legacy = {
        "strategy": "paired_lock", "asset": None, "timeframe": None,
        "target_size": 10, "delay_ms": 50.0, "independent_episodes": 1,
        "close_windows": ["m1"], "durations": [], "profits": [0.13875],
        "latency_survived": 1, "completed": 0, "positive_completed": 0,
        "simulated_pnl": 0.0,
    }
    completed = {
        "strategy": "paired_lock", "asset": "BTC", "timeframe": "5m",
        "target_size": 10, "delay_ms": None, "independent_episodes": 0,
        "close_windows": [], "durations": [], "profits": [],
        "latency_survived": 0, "completed": 1, "positive_completed": 1,
        "simulated_pnl": 0.13875,
    }
    persisted = {
        "version": 1,
        "audit": {"identity": None, "offset": 0},
        "execution": {"identity": None, "offset": 0},
        "seen": [], "funnels": {}, "active": {},
        "patterns": {"legacy": legacy, "completed": completed},
    }
    state.write_text(json.dumps(persisted), encoding="utf-8")

    result = IncrementalArbitrageResearch(audit, state_path=state).refresh()

    assert len(result["repeatable_patterns"]) == 1
    pattern = result["repeatable_patterns"][0]
    assert pattern["asset"] == "BTC"
    assert pattern["timeframe"] == "5m"
    assert pattern["independent_episodes"] == 2
    assert pattern["completed"] == 1
    assert pattern["simulated_pnl"] == 0.13875


def test_counterfactual_patterns_are_independent_and_never_count_as_trades(tmp_path):
    audit = tmp_path / "audit.jsonl"
    observation = {
        "ts": 1,
        "event_id": "cf-1",
        "event_type": "shadow_arb_counterfactual",
        "strategy": "arbitrage_pattern_research",
        "market_id": "m1",
        "asset": "BTC",
        "timeframe": "5m",
        "generation": 1,
        "session": 1,
        "observations": [{
            "method": "paired_lock",
            "target_size": 2,
            "depth_ok": True,
            "post_cost_profit": 0.04,
            "latency_stress": [
                {"delay_ms": 0, "expected_execution_value": 0.04},
                {"delay_ms": 250, "expected_execution_value": -0.01},
            ],
        }],
    }
    audit.write_text(json.dumps(observation) + "\n")

    result = IncrementalArbitrageResearch(audit).refresh()

    counterfactual = result["counterfactual_patterns"]
    assert len(counterfactual) == 2
    assert counterfactual[0]["target_size"] == 2
    assert {row["delay_ms"] for row in counterfactual} == {0, 250}
    assert sum(row["independent_episodes"] for row in counterfactual) == 1
    assert result["funnels"]["paired_lock"]["completed"] == 0


def test_existing_version_one_state_is_migrated_for_counterfactual_research(tmp_path):
    audit = tmp_path / "audit.jsonl"
    state = tmp_path / "state.json"
    audit.write_text("", encoding="utf-8")
    state.write_text(json.dumps({
        "version": 1,
        "audit": {"identity": None, "offset": 0},
        "execution": {"identity": None, "offset": 0},
        "seen": [],
        "funnels": {},
        "active": {},
        "patterns": {},
    }), encoding="utf-8")

    result = IncrementalArbitrageResearch(audit, state_path=state).refresh()

    assert result["counterfactual_patterns"] == []
    assert set(result["funnels"]) == {
        "paired_lock", "split_sell_lock", "maker_complete_set_arb",
    }


def _canonical(event_type, event_id, attempt_id, close_ts=100, **updates):
    row = {
        "ts": close_ts - 10,
        "event_id": event_id,
        "event_type": event_type,
        "attempt_id": attempt_id,
        "strategy": "paired_lock",
        "market_id": f"m-{close_ts}",
        "asset": "BTC",
        "timeframe": "5m",
        "target_size": 10,
        "delay_ms": 50,
        "leg_order": "UP_THEN_DOWN",
        "config_hash": "cfg-1",
        "generation": 2,
        "session": 3,
        "close_ts": close_ts,
        "initial_locked_profit": .05,
        "delayed_locked_profit": .04,
        "orphan_pnl": 0,
        "first_leg_book_executable": True,
        "both_legs_book_executable": event_type == "arb_shadow_book_executable",
    }
    row.update(updates)
    return row


def test_canonical_attempt_outcomes_are_counted_without_fake_fills(tmp_path):
    audit = tmp_path / "audit.jsonl"
    rows = [
        _canonical("arb_episode_started", "ep-1", "a1"),
        _canonical("arb_shadow_attempt", "a-1", "a1"),
        _canonical("arb_shadow_leg_result", "l1-1", "a1", leg_index=1),
        _canonical("arb_shadow_book_executable", "b-1", "a1"),
    ]
    audit.write_text("".join(json.dumps(row) + "\n" for row in rows))

    result = IncrementalArbitrageResearch(audit).refresh()

    funnel = result["funnels"]["paired_lock"]
    assert funnel["independent_episodes"] == 1
    assert funnel["shadow_attempts"] == 1
    assert funnel["leg_1_book_executable"] == 1
    assert funnel["both_legs_book_executable"] == 1
    assert funnel["orphaned"] == 0
    assert funnel["invalidated"] == 0
    assert "both_legs_filled" not in funnel
    pattern = result["repeatable_patterns"][0]
    assert pattern["leg_order"] == "UP_THEN_DOWN"
    assert pattern["attempts"] == 1
    assert pattern["book_executable_rate"] == 1
    assert pattern["classification"] == "OBSERVED"


def test_pattern_rates_use_the_same_bounded_attempt_and_outcome_window(tmp_path):
    audit = tmp_path / "audit.jsonl"
    rows = []
    for index in range(4100):
        attempt_id = f"a-{index}"
        rows.extend((
            _canonical("arb_shadow_attempt", f"attempt-{index}", attempt_id),
            _canonical(
                "arb_shadow_book_executable", f"outcome-{index}", attempt_id,
            ),
        ))
    audit.write_text("".join(json.dumps(row) + "\n" for row in rows))

    pattern = IncrementalArbitrageResearch(audit).refresh()[
        "repeatable_patterns"
    ][0]

    assert pattern["lifetime_attempts"] == 4100
    assert pattern["attempts"] == 4096
    assert pattern["book_executable"] == 4096
    assert pattern["book_executable_rate"] == 1


def test_leg_orders_and_config_hashes_are_isolated_patterns(tmp_path):
    audit = tmp_path / "audit.jsonl"
    rows = []
    for index, (order, config_hash) in enumerate((
        ("UP_THEN_DOWN", "cfg-1"),
        ("DOWN_THEN_UP", "cfg-1"),
        ("UP_THEN_DOWN", "cfg-2"),
    )):
        rows.extend((
            _canonical("arb_episode_started", f"ep-{index}", f"a{index}",
                       close_ts=100 + index, leg_order=order,
                       config_hash=config_hash),
            _canonical("arb_shadow_attempt", f"a-{index}", f"a{index}",
                       close_ts=100 + index, leg_order=order,
                       config_hash=config_hash),
        ))
    audit.write_text("".join(json.dumps(row) + "\n" for row in rows))

    result = IncrementalArbitrageResearch(audit).refresh()

    assert len(result["repeatable_patterns"]) == 3
    assert {(row["leg_order"], row["config_hash"])
            for row in result["repeatable_patterns"]} == {
        ("UP_THEN_DOWN", "cfg-1"),
        ("DOWN_THEN_UP", "cfg-1"),
        ("UP_THEN_DOWN", "cfg-2"),
    }


def test_research_candidate_requires_statistical_lower_bounds(tmp_path):
    audit = tmp_path / "audit.jsonl"
    rows = []
    for index in range(20):
        close_ts = 1000 + index
        attempt_id = f"a{index}"
        rows.extend((
            _canonical("arb_episode_started", f"ep-{index}", attempt_id,
                       close_ts=close_ts),
            _canonical("arb_shadow_attempt", f"a-{index}", attempt_id,
                       close_ts=close_ts),
            _canonical("arb_shadow_book_executable", f"b-{index}", attempt_id,
                       close_ts=close_ts, delayed_locked_profit=.04),
        ))
    audit.write_text("".join(json.dumps(row) + "\n" for row in rows))

    pattern = IncrementalArbitrageResearch(audit).refresh()[
        "repeatable_patterns"
    ][0]

    assert pattern["classification"] == "RESEARCH_CANDIDATE"
    assert pattern["distinct_close_windows"] == 20
    assert pattern["book_executable_wilson_95"]["lower"] > .8
    assert pattern["conservative_pnl_95"]["lower"] > 0


def test_orphans_prevent_candidate_and_use_conservative_pnl(tmp_path):
    audit = tmp_path / "audit.jsonl"
    rows = []
    for index in range(20):
        close_ts = 2000 + index
        attempt_id = f"a{index}"
        outcome = "arb_shadow_orphaned" if index in (0, 1) else "arb_shadow_book_executable"
        rows.extend((
            _canonical("arb_episode_started", f"ep-{index}", attempt_id,
                       close_ts=close_ts),
            _canonical("arb_shadow_attempt", f"a-{index}", attempt_id,
                       close_ts=close_ts),
            _canonical(outcome, f"o-{index}", attempt_id,
                       close_ts=close_ts,
                       delayed_locked_profit=.04,
                       orphan_pnl=-.5 if outcome == "arb_shadow_orphaned" else 0),
        ))
    audit.write_text("".join(json.dumps(row) + "\n" for row in rows))

    result = IncrementalArbitrageResearch(audit).refresh()
    pattern = result["repeatable_patterns"][0]

    assert pattern["orphan_rate"] == .1
    assert pattern["classification"] == "OBSERVED"
    assert result["no_repeatable_arbitrage"] is True


def test_confidence_helpers_are_deterministic():
    wilson = _wilson_interval(20, 20)
    assert round(wilson[0], 6) == .83887
    assert wilson[1] == 1
    mean = _mean_confidence_interval([.02, .04, .06, .08])
    assert mean[0] == .05
    assert mean[1] < .05 < mean[2]


def test_empty_research_reports_no_repeatable_arbitrage(tmp_path):
    audit = tmp_path / "audit.jsonl"
    audit.write_text("")

    result = IncrementalArbitrageResearch(audit).refresh()

    assert result["no_repeatable_arbitrage"] is True
    assert result["conclusion"] == "NO REPEATABLE ARBITRAGE FOUND"


def test_persistent_state_is_batched_without_delaying_live_report(tmp_path):
    audit = tmp_path / "audit.jsonl"
    state = tmp_path / "state.json"
    now = [100.0]
    audit.write_text(json.dumps(_row("a1", "ACCEPT", 1)) + "\n")
    tracker = IncrementalArbitrageResearch(
        audit,
        state_path=state,
        save_interval_seconds=30,
        clock=lambda: now[0],
    )

    assert tracker.refresh()["funnels"]["paired_lock"]["evaluations"] == 1
    first_persisted = json.loads(state.read_text(encoding="utf-8"))
    assert first_persisted["funnels"]["paired_lock"]["evaluations"] == 1

    with audit.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_row("a2", "REJECT", 2)) + "\n")
    now[0] += 1
    assert tracker.refresh()["funnels"]["paired_lock"]["evaluations"] == 2
    still_persisted = json.loads(state.read_text(encoding="utf-8"))
    assert still_persisted["funnels"]["paired_lock"]["evaluations"] == 1

    now[0] += 30
    tracker.refresh()
    latest_persisted = json.loads(state.read_text(encoding="utf-8"))
    assert latest_persisted["funnels"]["paired_lock"]["evaluations"] == 2


def test_out_of_sample_validation_uses_later_independent_windows(tmp_path):
    audit = tmp_path / "audit.jsonl"
    rows = []
    for index in range(50):
        close_ts = 3000 + index
        attempt_id = f"a{index}"
        rows.extend((
            _canonical("arb_episode_started", f"ep-{index}", attempt_id,
                       close_ts=close_ts),
            _canonical("arb_shadow_attempt", f"a-{index}", attempt_id,
                       close_ts=close_ts),
            _canonical("arb_shadow_book_executable", f"b-{index}", attempt_id,
                       close_ts=close_ts, delayed_locked_profit=.04),
        ))
    audit.write_text("".join(json.dumps(row) + "\n" for row in rows))

    pattern = IncrementalArbitrageResearch(audit).refresh()[
        "repeatable_patterns"
    ][0]

    assert pattern["classification"] == "OUT_OF_SAMPLE_VALIDATED"
    assert pattern["cohorts"]["discovery"]["distinct_close_windows"] == 30
    assert pattern["cohorts"]["validation"]["distinct_close_windows"] == 20
    assert pattern["cohorts"]["validation"]["max_market_pnl_contribution"] == .05
    assert pattern["profitable_capacity"] == 10


def test_validation_concentration_aggregates_repeated_market_profit(tmp_path):
    audit = tmp_path / "audit.jsonl"
    rows = []
    for index in range(50):
        close_ts = 4000 + index
        attempt_id = f"a{index}"
        market_id = "concentrated" if index >= 30 and index < 35 else f"m{index}"
        rows.extend((
            _canonical("arb_episode_started", f"ep-{index}", attempt_id,
                       close_ts=close_ts, market_id=market_id),
            _canonical("arb_shadow_attempt", f"a-{index}", attempt_id,
                       close_ts=close_ts, market_id=market_id),
            _canonical("arb_shadow_book_executable", f"b-{index}", attempt_id,
                       close_ts=close_ts, market_id=market_id,
                       delayed_locked_profit=.04),
        ))
    audit.write_text("".join(json.dumps(row) + "\n" for row in rows))

    pattern = IncrementalArbitrageResearch(audit).refresh()[
        "repeatable_patterns"
    ][0]

    assert pattern["cohorts"]["validation"][
        "max_market_pnl_contribution"
    ] == .25
    assert pattern["classification"] == "RESEARCH_CANDIDATE"
