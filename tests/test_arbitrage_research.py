import json

from poly_arb_bot.arbitrage_research import IncrementalArbitrageResearch


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
        "shadow_attempts": None,
        "both_legs_filled": None,
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
    assert pattern["classification"] == "RESEARCH_CANDIDATE"
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
