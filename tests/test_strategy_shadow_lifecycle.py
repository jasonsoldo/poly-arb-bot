import json
from dataclasses import replace

import pytest

from poly_arb_bot.ev_shadow import canonical_strategy_config_hash
from poly_arb_bot.profitability_analysis import (
    build_profitability_report, cohort_key,
)
from poly_arb_bot.strategy_shadow_lifecycle import PortfolioLimits, StrategyShadowLifecycle, process_audit_once


@pytest.fixture(autouse=True)
def _deterministic_calibration_mode(monkeypatch):
    # Results must not depend on the operator's .env leaking
    # SHADOW_CALIBRATION_MODE=1 into the pytest process (observed on the VPS
    # after `set -a; . ./.env; set +a`): calibration mode deliberately bypasses
    # the portfolio limits most tests here assert on.
    monkeypatch.delenv("SHADOW_CALIBRATION_MODE", raising=False)
    # Same leak concern for the profit-exit knobs: default "ev" mode behavior
    # must not depend on the operator's .env.
    monkeypatch.delenv("SHADOW_PROFIT_EXIT_MODE", raising=False)
    monkeypatch.delenv("SHADOW_PROFIT_EXIT_EV_MARGIN", raising=False)
    monkeypatch.delenv("DIRECTIONAL_PROFIT_EXIT_MODE", raising=False)
    monkeypatch.delenv("LOTTERY_PROFIT_EXIT_MODE", raising=False)


def test_lifecycle_persists_real_order_invariants_on_initialization(tmp_path):
    state = tmp_path / "state.json"
    StrategyShadowLifecycle(state, tmp_path / "audit.jsonl")
    stored = json.loads(state.read_text(encoding="utf-8"))
    assert stored["real_order_submissions"] == 0
    assert stored["real_orders"] == 0
    assert stored["real_fills"] == 0


def test_legacy_probability_state_migrates_without_touching_paired_lock(
        tmp_path):
    state = tmp_path / "state.json"
    research_hash = "c" * 64
    deployable_hash = "d" * 64
    state.write_text(json.dumps({
        "positions": {
            "late_window_directional_ev:m1:Up": {
                    "strategy": "late_window_directional_ev",
                    "market_id": "m1",
                    "deployable_pnl": False,
                    "config_hash": research_hash,
            },
            "low_price_lottery_ev:m2:Down": {
                    "strategy": "low_price_lottery_ev",
                    "market_id": "m2",
                    "deployable_pnl": True,
                    "config_hash": deployable_hash,
            },
            "paired_lock:m3:Both": {
                "strategy": "paired_lock",
                "market_id": "m3",
            },
        },
        "completed": [],
        "audit_offset": 0,
        "paired_audit_offset": 0,
        "real_order_submissions": 0,
        "real_orders": 0,
        "real_fills": 0,
    }), encoding="utf-8")

    lifecycle = StrategyShadowLifecycle(state, tmp_path / "events.jsonl")

    assert set(lifecycle.data["positions"]) == {
        "low_price_lottery_ev:m2:Down", "paired_lock:m3:Both",
    }
    assert set(lifecycle.data["research_positions"]) == {
        "late_window_directional_ev:m1:Up",
    }
    assert lifecycle.data["research_positions"][
        "late_window_directional_ev:m1:Up"
    ]["risk_mode"] == "CALIBRATION_RESEARCH"
    assert {
        claim.rsplit("|", 1)[-1]
        for claim in lifecycle.data["research_claimed_markets"]
    } == {"m1", "m2"}
    assert lifecycle.data["research_claimed_markets"] == [
        f"{research_hash}|m1", f"{deployable_hash}|m2",
    ]
    assert {
        claim.rsplit("|", 1)[-1]
        for claim in lifecycle.data["deployable_claimed_markets"]
    } == {"m2"}
    assert all("m3" not in claim for claim in (
        lifecycle.data["research_claimed_markets"]
        + lifecycle.data["deployable_claimed_markets"]
    ))


def test_legacy_completion_log_is_claimed_before_new_entries_are_consumed(
        tmp_path):
    log = tmp_path / "events.jsonl"
    old_hash = "c" * 64
    log.write_text(json.dumps({
        "event_id": "legacy:complete",
        "event_type": "shadow_complete",
        "strategy": "late_window_directional_ev",
        "market_id": "m1",
        "ts": 1101,
        "realized_simulated_pnl": 1.0,
        "strategy_config_hash": canonical_strategy_config_hash(),
        "config_hash": old_hash,
    }) + "\n", encoding="utf-8")
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    row = accepted("new-after-upgrade")

    assert lifecycle.capture_research_candidate(row, {"m1": market()}) is False
    assert lifecycle.consume(row, {"m1": market()}) is False
    assert lifecycle.data["research_positions"] == {}
    assert lifecycle.data["positions"] == {}
    assert len(lifecycle.data["research_claimed_markets"]) == 1
    assert len(lifecycle.data["deployable_claimed_markets"]) == 1


def test_legacy_research_completion_rebuilds_missing_claim_on_restart(tmp_path):
    state = tmp_path / "state.json"
    old_hash = "c" * 64
    state.write_text(json.dumps({
        "positions": {},
        "research_positions": {},
        "research_completed": [{
            "event_id": "research:m1:complete",
            "strategy": "late_window_directional_ev",
            "market_id": "m1",
            "research_lifecycle_config_hash": old_hash,
            "research_claim_key": f"{old_hash}|m1",
        }],
        "research_claimed_markets": [],
        "deployable_claimed_markets": [],
        "completed": [],
        "audit_offset": 0,
        "paired_audit_offset": 0,
        "real_order_submissions": 0,
        "real_orders": 0,
        "real_fills": 0,
    }), encoding="utf-8")
    lifecycle = StrategyShadowLifecycle(state, tmp_path / "events.jsonl")

    assert lifecycle.capture_research_candidate(
        accepted("new-research"), {"m1": market()},
    ) is False
    assert lifecycle.data["research_positions"] == {}
    assert {
        claim.rsplit("|", 1)[-1]
        for claim in lifecycle.data["research_claimed_markets"]
    } == {"m1"}
    assert lifecycle.data["research_claimed_markets"] == [f"{old_hash}|m1"]
    assert lifecycle.data["deployable_claimed_markets"] == []


def test_research_completion_log_backfill_rebuilds_missing_claim(tmp_path):
    log = tmp_path / "events.jsonl"
    old_hash = "c" * 64
    log.write_text(json.dumps({
        "event_id": "research:m1:complete",
        "event_type": "shadow_research_complete",
        "strategy": "late_window_directional_ev",
        "market_id": "m1",
        "config_hash": old_hash,
        "research_lifecycle_config_hash": old_hash,
        "research_claim_key": f"{old_hash}|m1",
    }) + "\n", encoding="utf-8")
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)

    assert lifecycle.capture_research_candidate(
        accepted("new-after-log-backfill"), {"m1": market()},
    ) is False
    assert lifecycle.data["research_positions"] == {}
    assert lifecycle.data["research_completed"] == [{
        "event_id": "research:m1:complete",
        "strategy": "late_window_directional_ev",
        "market_id": "m1",
        "research_lifecycle_config_hash": old_hash,
        "research_claim_key": f"{old_hash}|m1",
    }]
    assert len(lifecycle.data["research_claimed_markets"]) == 1
    assert lifecycle.data["deployable_claimed_markets"] == []


def _research_completion(
        event_id="research:m1:complete", market_id="m1",
        lifecycle_config_hash=None, strategy="late_window_directional_ev"):
    lifecycle_config_hash = lifecycle_config_hash or ("c" * 64)
    return {
        "event_id": event_id,
        "event_type": "shadow_research_complete",
        "strategy": strategy,
        "market_id": market_id,
        "config_hash": lifecycle_config_hash,
        "research_lifecycle_config_hash": lifecycle_config_hash,
        "research_claim_key": f"{lifecycle_config_hash}|{market_id}",
    }


def test_research_claim_identity_survives_config_change_restart(tmp_path):
    state = tmp_path / "state.json"
    log = tmp_path / "events.jsonl"
    first = StrategyShadowLifecycle(state, log, profit_exit_min_pnl=.10)
    row = accepted()
    assert first.capture_research_candidate(row, {"m1": market()}) is True
    original_claim = first.data["research_claimed_markets"][0]
    position = next(iter(first.data["research_positions"].values()))
    assert position["research_claim_key"] == original_claim
    assert position["research_lifecycle_config_hash"] == first.config_hash

    stored = json.loads(state.read_text(encoding="utf-8"))
    stored["research_claimed_markets"] = []
    state.write_text(json.dumps(stored), encoding="utf-8")
    restarted = StrategyShadowLifecycle(
        state, log, profit_exit_min_pnl=.25,
    )

    assert restarted.config_hash != first.config_hash
    assert restarted.data["research_claimed_markets"] == [original_claim]
    assert (
        next(iter(restarted.data["research_positions"].values()))[
            "research_claim_key"
        ] == original_claim
    )
    assert restarted._claim_key("m1") != original_claim


def test_research_completion_preserves_original_claim_across_config_change(
        tmp_path):
    state = tmp_path / "state.json"
    log = tmp_path / "events.jsonl"
    lifecycle = StrategyShadowLifecycle(state, log, profit_exit_min_pnl=.10)
    row = accepted()
    assert lifecycle.capture_research_candidate(row, {"m1": market()}) is True
    original_claim = lifecycle.data["research_claimed_markets"][0]
    lifecycle.settle(
        {"m1": market()},
        {"assets": {"BTC": {"chainlink_settlement_samples": [{
            "source_timestamp_ms": 1_100_000, "price": 101,
        }]}}},
        now=1101,
    )
    completion = lifecycle.data["research_completed"][0]
    assert completion["research_claim_key"] == original_claim
    assert completion["research_lifecycle_config_hash"] == lifecycle.config_hash

    stored = json.loads(state.read_text(encoding="utf-8"))
    stored["research_claimed_markets"] = []
    state.write_text(json.dumps(stored), encoding="utf-8")
    restarted = StrategyShadowLifecycle(
        state, log, profit_exit_min_pnl=.25,
    )
    assert restarted.data["research_claimed_markets"] == [original_claim]
    assert restarted._claim_key("m1") != original_claim


def test_legacy_research_string_resolves_from_defensive_rotated_history(
        tmp_path):
    state = tmp_path / "state.json"
    log = tmp_path / "events.jsonl"
    event_id = "legacy-research:complete"
    old_hash = "d" * 64
    state.write_text(json.dumps({
        "positions": {},
        "research_positions": {},
        "research_completed": [event_id],
        "research_claimed_markets": [],
        "deployable_claimed_markets": [],
        "completed": [],
        "real_order_submissions": 0,
        "real_orders": 0,
        "real_fills": 0,
    }), encoding="utf-8")
    log.write_text("", encoding="utf-8")
    incomplete = {
        "event_id": event_id,
        "event_type": "shadow_research_complete",
        "strategy": "late_window_directional_ev",
        "market_id": "m1",
    }
    wrong_strategy = _research_completion(
        "wrong-strategy:complete", "paired-market", old_hash, "paired_lock",
    )
    valid = _research_completion(event_id, "m1", old_hash)
    (tmp_path / "events.jsonl.1").write_text(
        "\n".join((
            json.dumps(7),
            json.dumps(incomplete),
            json.dumps(wrong_strategy),
            json.dumps(valid),
        )) + "\n",
        encoding="utf-8",
    )

    lifecycle = StrategyShadowLifecycle(state, log)

    assert lifecycle.data["research_completed"] == [{
        "event_id": event_id,
        "strategy": "late_window_directional_ev",
        "market_id": "m1",
        "research_lifecycle_config_hash": old_hash,
        "research_claim_key": f"{old_hash}|m1",
    }]
    assert lifecycle.data["research_claimed_markets"] == [f"{old_hash}|m1"]
    assert lifecycle.data["research_claim_migration_incomplete"] == []
    assert all(
        "paired-market" not in claim
        for claim in lifecycle.data["research_claimed_markets"]
    )


def test_unresolved_legacy_research_string_persists_fail_closed_guard(
        tmp_path):
    state = tmp_path / "state.json"
    log = tmp_path / "events.jsonl"
    event_id = "rotated-away:complete"
    state.write_text(json.dumps({
        "positions": {},
        "research_positions": {},
        "research_completed": [event_id],
        "research_claimed_markets": [],
        "deployable_claimed_markets": [],
        "completed": [],
        "real_order_submissions": 0,
        "real_orders": 0,
        "real_fills": 0,
    }), encoding="utf-8")
    log.write_text("", encoding="utf-8")

    lifecycle = StrategyShadowLifecycle(state, log)

    assert lifecycle.data["research_claim_migration_incomplete"] == [event_id]
    assert lifecycle.capture_research_candidate(
        accepted("blocked-by-migration"), {"m1": market()},
    ) is False
    persisted = json.loads(state.read_text(encoding="utf-8"))
    assert persisted["research_claim_migration_incomplete"] == [event_id]

    restarted = StrategyShadowLifecycle(state, log)
    assert restarted.data["research_claim_migration_incomplete"] == [event_id]
    assert restarted.capture_research_candidate(
        accepted("still-blocked"), {"m1": market()},
    ) is False

    old_hash = "e" * 64
    log.write_text(
        json.dumps(_research_completion(event_id, "m1", old_hash)) + "\n",
        encoding="utf-8",
    )
    recovered = StrategyShadowLifecycle(state, log)
    assert recovered.data["research_claim_migration_incomplete"] == []
    assert recovered.data["research_completed"][0]["research_claim_key"] == (
        f"{old_hash}|m1"
    )
    unrelated = accepted("unblocked-after-recovery")
    unrelated["market_id"] = "m2"
    assert recovered.capture_research_candidate(
        unrelated, {"m2": dict(market(), market_id="m2")},
    ) is True


def test_conflicting_valid_history_rows_keep_legacy_research_unresolved(
        tmp_path):
    state = tmp_path / "state.json"
    log = tmp_path / "events.jsonl"
    event_id = "conflicted-research:complete"
    state.write_text(json.dumps({
        "positions": {},
        "research_positions": {},
        "research_completed": [event_id],
        "research_claimed_markets": [],
        "deployable_claimed_markets": [],
        "completed": [],
        "real_order_submissions": 0,
        "real_orders": 0,
        "real_fills": 0,
    }), encoding="utf-8")
    genuine = _research_completion(event_id, "m1", "a" * 64)
    conflicting = _research_completion(event_id, "wrong-market", "b" * 64)
    log.write_text(json.dumps(genuine) + "\n", encoding="utf-8")
    (tmp_path / "events.jsonl.1").write_text(
        json.dumps(conflicting) + "\n", encoding="utf-8",
    )

    lifecycle = StrategyShadowLifecycle(state, log)

    assert lifecycle.data["research_completed"] == [event_id]
    assert event_id in lifecycle.data["research_claim_migration_incomplete"]
    assert any(
        marker.startswith("conflict:")
        for marker in lifecycle.data["research_claim_migration_incomplete"]
    )
    assert lifecycle.data["research_claimed_markets"] == []
    assert lifecycle.capture_research_candidate(
        accepted("blocked-by-conflict"), {"m1": market()},
    ) is False

    (tmp_path / "events.jsonl.1").unlink()
    restarted = StrategyShadowLifecycle(state, log)
    assert restarted.data["research_completed"] == [event_id]
    assert event_id in restarted.data["research_claim_migration_incomplete"]
    assert restarted.data["research_claimed_markets"] == []


def test_identical_valid_history_duplicates_resolve_legacy_research(
        tmp_path):
    state = tmp_path / "state.json"
    log = tmp_path / "events.jsonl"
    event_id = "duplicate-research:complete"
    old_hash = "a" * 64
    state.write_text(json.dumps({
        "positions": {},
        "research_positions": {},
        "research_completed": [event_id],
        "research_claimed_markets": [],
        "deployable_claimed_markets": [],
        "completed": [],
        "real_order_submissions": 0,
        "real_orders": 0,
        "real_fills": 0,
    }), encoding="utf-8")
    completion = _research_completion(event_id, "m1", old_hash)
    serialized = json.dumps(completion) + "\n"
    log.write_text(serialized, encoding="utf-8")
    (tmp_path / "events.jsonl.1").write_text(
        serialized, encoding="utf-8",
    )

    lifecycle = StrategyShadowLifecycle(state, log)

    assert lifecycle.data["research_completed"] == [{
        "event_id": event_id,
        "strategy": "late_window_directional_ev",
        "market_id": "m1",
        "research_lifecycle_config_hash": old_hash,
        "research_claim_key": f"{old_hash}|m1",
    }]
    assert lifecycle.data["research_claim_migration_incomplete"] == []
    assert lifecycle.data["research_claimed_markets"] == [f"{old_hash}|m1"]


def test_malformed_research_completion_without_event_id_is_retained_and_guarded(
        tmp_path):
    state = tmp_path / "state.json"
    log = tmp_path / "events.jsonl"
    malformed = {
        "strategy": "late_window_directional_ev",
        "market_id": "m1",
        "unexpected": ["raw", "evidence"],
    }
    state.write_text(json.dumps({
        "positions": {},
        "research_positions": {},
        "research_completed": [malformed],
        "research_claimed_markets": [],
        "deployable_claimed_markets": [],
        "completed": [],
        "real_order_submissions": 0,
        "real_orders": 0,
        "real_fills": 0,
    }), encoding="utf-8")

    lifecycle = StrategyShadowLifecycle(state, log)

    assert lifecycle.data["research_completed"] == [malformed]
    assert len(lifecycle.data["research_claim_migration_incomplete"]) == 1
    marker = lifecycle.data["research_claim_migration_incomplete"][0]
    assert marker.startswith("opaque:")
    assert lifecycle.data["research_claimed_markets"] == []
    persisted = json.loads(state.read_text(encoding="utf-8"))
    assert persisted["research_completed"] == [malformed]
    assert persisted["research_claim_migration_incomplete"] == [marker]

    restarted = StrategyShadowLifecycle(state, log)
    assert restarted.data["research_completed"] == [malformed]
    assert restarted.data["research_claim_migration_incomplete"] == [marker]
    assert restarted.capture_research_candidate(
        accepted("blocked-by-opaque-guard"), {"m1": market()},
    ) is False


def test_opaque_research_guard_survives_completion_history_truncation(
        tmp_path):
    state = tmp_path / "state.json"
    log = tmp_path / "events.jsonl"
    malformed = {"strategy": "late_window_directional_ev"}
    state.write_text(json.dumps({
        "positions": {},
        "research_positions": {},
        "research_completed": [malformed],
        "research_claimed_markets": [],
        "deployable_claimed_markets": [],
        "completed": [],
        "real_order_submissions": 0,
        "real_orders": 0,
        "real_fills": 0,
    }), encoding="utf-8")
    lifecycle = StrategyShadowLifecycle(state, log)
    marker = lifecycle.data["research_claim_migration_incomplete"][0]
    valid = _research_completion("retained-valid:complete", "m1", "f" * 64)
    lifecycle.data["research_completed"] = [malformed] + [valid] * 20000
    lifecycle._append_research_completion(
        _research_completion("new-valid:complete", "m2", "e" * 64)
    )
    assert malformed in lifecycle.data["research_completed"]
    assert len(lifecycle.data["research_completed"]) == 20001
    assert marker in lifecycle.data["research_claim_migration_incomplete"]

    lifecycle.data["research_completed"] = (
        lifecycle.data["research_completed"][-20000:]
    )
    lifecycle._mark_dirty()
    lifecycle.flush()

    restarted = StrategyShadowLifecycle(state, log)

    assert malformed not in restarted.data["research_completed"]
    assert restarted.data["research_claim_migration_incomplete"] == [marker]
    assert restarted.capture_research_candidate(
        accepted("blocked-after-truncation"), {"m2": dict(market(), market_id="m2")},
    ) is False


def accepted(event_id="a1", strategy="late_window_directional_ev", outcome="Up"):
    probability_model_id = (
        "directional_logistic_projected_v2"
        if strategy == "late_window_directional_ev"
        else "lottery_logistic_projected_blend_v2"
    )
    row = {
        "event_id": event_id, "event_type": "shadow_eval", "strategy": strategy,
        "market_id": "m1", "condition_id": "c1", "asset": "BTC",
        "timeframe": "5m", "outcome": outcome,
        "decision": "ACCEPT", "expected_fill_price": 0.4, "fees": 0.01,
        "target_size": 10, "ts": 1000,
        "config_hash": canonical_strategy_config_hash(strategy),
        "estimated_probability": 0.7,
        "calibration_input_probability": 0.72,
        "probability_model_id": probability_model_id,
        "profitability_gate_decision": "ALLOW",
        "profitability_gate_reason": "profitability_cohort_eligible",
        "profitability_gate_hash": "a" * 64,
        "calibration_snapshot_hash": "b" * 64,
        "deployable_candidate": True,
        "settlement_source": "chainlink",
        "settlement_source_verified": True,
        "price_to_beat": 100,
        "seconds_to_close": 100,
        "target_depth_ok": True,
        "executable_depth_size": 12,
        "fee_rate": 0.07,
        "config_version": "shadow-buy-rules-v10",
        "sizing_mode": "real_market_dynamic_v1",
        "dynamic_target_size": 10,
        "market_minimum_size": 1,
        "dynamic_buy_notional": 4.0,
        "dynamic_vwap": 0.4,
        "dynamic_fee": 0.1,
        "dynamic_buffer": 0.2,
        "dynamic_cash_cost": 4.1,
        "dynamic_risk_adjusted_cost": 4.3,
        "dynamic_all_in_cost": 4.3,
        "dynamic_maximum_loss": 4.1,
        "capital_budget_usd": 20,
        "size_binding_constraint": "capital_budget",
        "real_order_submissions": 0,
        "real_orders": 0,
        "real_fills": 0,
    }
    row["profitability_cohort_key"] = cohort_key(row)
    return row


def paired(event_id="pair1"):
    return {
        "event_id": event_id, "event_type": "shadow_opportunity", "strategy": "paired_lock",
        "market_id": "m1", "decision": "ACCEPT", "target_size": 10,
        "condition_id": "c1", "asset": "BTC", "timeframe": "5m", "window": "current",
        "generation": 3, "session": 7, "evaluation_sequence": 11,
        "net_cost": 9.7, "locked_profit": 0.3, "ts": 1000,
        "config_version": "paired-lock-shadow-v3", "config_hash": "paired-hash",
        "sizing_mode": "real_market_dynamic_v1",
        "dynamic_target_size": 10,
        "market_minimum_size": 1,
        "dynamic_all_in_cost": 9.7,
        "dynamic_maximum_loss": 9.7,
        "capital_budget_usd": 20,
        "size_binding_constraint": "executable_depth",
    }


def hedged(event_id="hedge1", main_outcome="Up"):
    return {
        "event_id": event_id, "event_type": "shadow_hedged_opportunity",
        "strategy": "late_window_directional_ev", "hedge_strategy": "low_price_lottery_ev",
        "market_id": "m1", "decision": "ACCEPT", "asset": "BTC", "timeframe": "5m",
        "main_outcome": main_outcome, "hedge_outcome": "Down" if main_outcome == "Up" else "Up",
        "main_size": 10, "hedge_size": 8, "main_expected_fill_price": .8,
        "hedge_expected_fill_price": .04, "main_cost": 8.05, "hedge_cost": .35,
        "total_cost": 8.4, "main_win_pnl": 1.6, "reversal_pnl": -.4,
        "expected_portfolio_pnl": 1.4, "worst_case_pnl": -.4,
        "estimated_probability": .9, "seconds_to_close": 8, "target_size": 10,
        "ts": 1000, "config_version": "terminal-hedge-v1", "config_hash": "hedge-hash",
    }


def market(source="chainlink", timeframe="5m"):
    return {"market_id": "m1", "asset": "BTC", "interval": timeframe,
            "settlement_source": source, "close_ts": 1100, "open_price": 100}


def prediction(event_id="prediction-1", strategy="late_window_directional_ev",
               decision="REJECT", seconds_to_close=90):
    return {
        "event_id": event_id, "event_type": "shadow_eval", "strategy": strategy,
        "market_id": "m1", "condition_id": "c1", "asset": "BTC",
        "timeframe": "5m", "window": "current", "outcome": "Up",
        "decision": decision, "reason": "net_ev_below_threshold",
        "estimated_probability": 0.7, "raw_estimated_probability": 0.75,
        "probability_model_id": "directional_logistic_projected_v2",
        "reference_quorum_met": True, "reference_state": "REFERENCE_READY",
        "settlement_source_verified": True, "settlement_reference": 100.5,
        "price_to_beat": 100, "seconds_to_close": seconds_to_close,
        "ts": 1010, "config_version": "strategy-v1", "config_hash": "model-hash",
    }


def probability_observation(event_id="observation-1",
                            strategy="late_window_directional_ev"):
    row = prediction(event_id, strategy)
    row["event_type"] = "shadow_prediction_observation"
    row["opens_position"] = False
    row["observation_semantics"] = "PROBABILITY_CALIBRATION_NOT_ORDER"
    return row


def test_repeated_accepts_open_one_position(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "complete.jsonl")
    assert lifecycle.consume(accepted(), {"m1": market()}) is True
    assert lifecycle.consume(accepted("a2"), {"m1": market()}) is False
    assert len(lifecycle.data["positions"]) == 1
    position = next(iter(lifecycle.data["positions"].values()))
    assert position["entry_cost"] == 4.1
    assert position["risk_adjusted_entry_cost"] == 4.3
    assert position["cash_ledger_version"] == 2
    assert position["deployable_pnl"] is True
    assert position["real_order_submissions"] == 0
    assert position["config_version"] == "shadow-portfolio-v8"
    assert len(position["config_hash"]) == 64


def test_raw_directional_accept_is_calibration_only_and_does_not_open_position(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "events.jsonl")
    row = accepted()
    row["config_version"] = "shadow-buy-rules-v7"
    assert lifecycle.consume(row, {"m1": market()}) is False
    assert lifecycle.data["positions"] == {}


def test_v8_directional_accept_does_not_open_dynamic_shadow_position(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "events.jsonl")
    row = accepted()
    row["config_version"] = "shadow-buy-rules-v8"

    assert lifecycle.consume(row, {"m1": market()}) is False
    assert lifecycle.data["positions"] == {}


def test_v9_directional_accept_does_not_open_dynamic_shadow_position(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "events.jsonl")
    row = accepted()
    row["config_version"] = "shadow-buy-rules-v9"

    assert lifecycle.consume(row, {"m1": market()}) is False
    assert lifecycle.data["positions"] == {}


def test_v10_directional_accept_requires_dynamic_sizing_evidence(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "events.jsonl")
    row = accepted()
    row.pop("dynamic_cash_cost")

    assert lifecycle.consume(row, {"m1": market()}) is False
    assert lifecycle.data["positions"] == {}


def test_v10_directional_accept_separates_cash_and_risk_adjusted_cost(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "events.jsonl")
    row = accepted()
    row.update({
        "target_size": 7.25,
        "dynamic_target_size": 7.25,
        "dynamic_buy_notional": 4.0,
        "dynamic_fee": 0.1,
        "dynamic_buffer": 0.2,
        "dynamic_cash_cost": 4.1,
        "dynamic_risk_adjusted_cost": 4.3,
        "dynamic_all_in_cost": 4.3,
        "dynamic_maximum_loss": 4.1,
        "capital_budget_usd": 4.3,
        "size_binding_constraint": "capital_budget",
    })

    assert lifecycle.consume(row, {"m1": market()}) is True
    position = next(iter(lifecycle.data["positions"].values()))
    assert position["target_size"] == 7.25
    assert position["entry_cost"] == 4.1
    assert position["risk_adjusted_entry_cost"] == 4.3
    assert position["dynamic_maximum_loss"] == 4.1
    assert position["sizing_mode"] == "real_market_dynamic_v1"
    assert position["size_binding_constraint"] == "capital_budget"


@pytest.mark.parametrize("risk_adjusted_cost", (-1.0, 4.0, 4.2))
def test_v10_probability_rejects_invalid_risk_adjusted_cost(
        tmp_path, risk_adjusted_cost):
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", tmp_path / "events.jsonl",
    )
    row = accepted()
    row["dynamic_risk_adjusted_cost"] = risk_adjusted_cost

    assert lifecycle.consume(row, {"m1": market()}) is False
    assert lifecycle.data["positions"] == {}


def test_probability_portfolio_limit_uses_risk_adjusted_entry_cost(tmp_path):
    limits = replace(PortfolioLimits(), directional_max_open_notional=4.2)
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", tmp_path / "events.jsonl", limits=limits,
    )

    assert lifecycle.consume(accepted(), {"m1": market()}) is False
    assert lifecycle.data["positions"] == {}
    assert lifecycle.data["portfolio_rejections"] == {
        "late_window_directional_ev:m1:Up": "directional_open_notional_limit",
    }


def test_paired_v3_requires_dynamic_sizing_evidence(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "events.jsonl")
    row = paired()
    row.pop("sizing_mode")

    assert lifecycle.consume(row, {"m1": market()}) is False
    assert lifecycle.data["positions"] == {}


def test_probability_position_exits_on_future_full_bid_depth_after_all_costs(tmp_path):
    log = tmp_path / "events.jsonl"
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", log, profit_exit_min_pnl=.10,
        profit_exit_mode="flat",
    )
    entry = accepted()
    entry["config_version"] = "shadow-buy-rules-v10"
    entry["generation"] = 2
    entry["session"] = 3
    assert lifecycle.consume(entry, {"m1": market()}) is True

    quote = {
        **entry,
        "event_id": "future-book-1",
        "event_type": "shadow_probability_profit_exit_book_executable",
        "decision": "REJECT",
        "reason": "net_ev_below_threshold",
        "ts": 1010,
        "generation": 4,
        "session": 9,
        "exit_fill_quantity": 10,
        "exit_vwap": .45,
        "exit_total_fee": .02,
        "exit_execution_buffer": .01,
        "exit_depth_ok": True,
        "exit_book_fresh": True,
        "exit_observation_semantics": "BOOK_EXECUTABLE_NOT_FILL",
    }

    assert lifecycle.consume(quote, {"m1": market()}) is True
    assert lifecycle.data["positions"] == {}
    complete = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert complete["event_type"] == "shadow_complete"
    assert complete["completion_reason"] == "profit_target_book_executable"
    assert complete["exit_vwap"] == .45
    assert complete["exit_cash_proceeds"] == 4.48
    assert complete["exit_risk_adjusted_proceeds"] == 4.47
    assert complete["realized_simulated_pnl"] == .38
    assert complete["cash_ledger_version"] == 2
    assert complete["deployable_pnl"] is True
    assert complete["real_orders"] == 0
    assert complete["real_fills"] == 0


def test_probability_position_does_not_fake_exit_without_full_bid_depth(tmp_path):
    log = tmp_path / "events.jsonl"
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", log, profit_exit_min_pnl=.25,
    )
    entry = accepted()
    entry["config_version"] = "shadow-buy-rules-v10"
    assert lifecycle.consume(entry, {"m1": market()}) is True

    quote = {
        **entry,
        "event_id": "partial-book",
        "decision": "REJECT",
        "ts": 1010,
        "exit_fill_quantity": 9.9,
        "exit_vwap": .90,
        "exit_total_fee": 0,
        "exit_execution_buffer": 0,
        "exit_depth_ok": False,
        "exit_book_fresh": True,
        "exit_observation_semantics": "BOOK_EXECUTABLE_NOT_FILL",
    }

    assert lifecycle.consume(quote, {"m1": market()}) is False
    assert len(lifecycle.data["positions"]) == 1
    assert not log.exists() or "shadow_complete" not in log.read_text(encoding="utf-8")


def test_probability_position_keeps_holding_when_net_profit_is_below_target(tmp_path):
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", tmp_path / "events.jsonl", profit_exit_min_pnl=.25,
        profit_exit_mode="flat",
    )
    entry = accepted()
    entry["config_version"] = "shadow-buy-rules-v10"
    assert lifecycle.consume(entry, {"m1": market()}) is True
    quote = {
        **entry,
        "event_id": "small-profit",
        "decision": "REJECT",
        "ts": 1010,
        "exit_fill_quantity": 10,
        "exit_vwap": .45,
        "exit_total_fee": .02,
        "exit_execution_buffer": .01,
        "exit_depth_ok": True,
        "exit_book_fresh": True,
        "exit_observation_semantics": "BOOK_EXECUTABLE_NOT_FILL",
    }

    assert lifecycle.consume(quote, {"m1": market()}) is False
    assert len(lifecycle.data["positions"]) == 1


def _exit_quote(entry, event_id="exit-1", ts=1010, vwap=.45):
    return {
        **entry,
        "event_id": event_id,
        "event_type": "shadow_probability_profit_exit_book_executable",
        "decision": "REJECT",
        "ts": ts,
        "exit_fill_quantity": 10,
        "exit_vwap": vwap,
        "exit_total_fee": .02,
        "exit_execution_buffer": .01,
        "exit_depth_ok": True,
        "exit_book_fresh": True,
        "exit_observation_semantics": "BOOK_EXECUTABLE_NOT_FILL",
    }


def test_ev_mode_holds_when_exit_proceeds_below_expected_settlement(tmp_path):
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", tmp_path / "events.jsonl",
        profit_exit_min_pnl=.10,
    )
    entry = accepted()  # estimated_probability 0.7, size 10 -> settlement EV 7.0
    entry["config_version"] = "shadow-buy-rules-v10"
    assert lifecycle.consume(entry, {"m1": market()}) is True

    # Risk-adjusted proceeds 4.47 clear the minimum while holding to
    # settlement is worth 7.0 in expectation: EV mode must keep holding.
    assert lifecycle.consume(_exit_quote(entry), {"m1": market()}) is False
    assert len(lifecycle.data["positions"]) == 1


def test_ev_mode_exits_when_proceeds_beat_expected_settlement(tmp_path):
    log = tmp_path / "events.jsonl"
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", log, profit_exit_min_pnl=.10,
    )
    entry = accepted()
    entry["config_version"] = "shadow-buy-rules-v10"
    entry["estimated_probability"] = 0.3  # settlement EV 3.0 < proceeds 4.47
    assert lifecycle.consume(entry, {"m1": market()}) is True

    assert lifecycle.consume(_exit_quote(entry), {"m1": market()}) is True
    assert lifecycle.data["positions"] == {}
    complete = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert complete["completion_reason"] == "profit_target_book_executable"
    assert complete["profit_exit_mode"] == "ev"
    assert complete["expected_settlement_value"] == 3.0
    assert complete["exit_ev_margin"] == 0.0
    assert complete["realized_simulated_pnl"] == .38


def test_ev_mode_respects_configured_margin(tmp_path):
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", tmp_path / "events.jsonl",
        profit_exit_min_pnl=.10, profit_exit_ev_margin=2.0,
    )
    entry = accepted()
    entry["config_version"] = "shadow-buy-rules-v10"
    entry["estimated_probability"] = 0.3  # settlement EV 3.0 + margin 2.0 > 4.47
    assert lifecycle.consume(entry, {"m1": market()}) is True

    assert lifecycle.consume(_exit_quote(entry), {"m1": market()}) is False
    assert len(lifecycle.data["positions"]) == 1


def test_probability_position_fails_closed_without_probability_evidence(tmp_path):
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", tmp_path / "events.jsonl",
        profit_exit_min_pnl=.10,
    )
    entry = accepted()
    entry["config_version"] = "shadow-buy-rules-v10"
    entry["estimated_probability"] = None
    assert lifecycle.consume(entry, {"m1": market()}) is False

    assert lifecycle.consume(_exit_quote(entry), {"m1": market()}) is False
    assert lifecycle.data["positions"] == {}


def test_per_strategy_env_override_restores_flat_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("LOTTERY_PROFIT_EXIT_MODE", "flat")
    log = tmp_path / "events.jsonl"
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", log, profit_exit_min_pnl=.10,
    )
    entry = accepted(strategy="low_price_lottery_ev")
    entry["config_version"] = "shadow-buy-rules-v10"
    assert lifecycle.consume(entry, {"m1": market()}) is True

    # Global default is EV mode (which would hold at proceeds 4.47 < 7.0),
    # but the lottery override restores the legacy flat rule and exits.
    assert lifecycle.consume(_exit_quote(entry), {"m1": market()}) is True
    complete = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert complete["profit_exit_mode"] == "flat"
    assert complete["expected_settlement_value"] is None


def test_stale_dedicated_profit_exit_cannot_close_a_newer_position(tmp_path):
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", tmp_path / "events.jsonl",
        profit_exit_min_pnl=.10,
    )
    entry = accepted("new-entry")
    entry["config_version"] = "shadow-buy-rules-v10"
    assert lifecycle.consume(entry, {"m1": market()}) is True

    stale_exit = {
        **entry,
        "event_id": "old-exit",
        "entry_event_id": "old-entry",
        "event_type": "shadow_probability_profit_exit_book_executable",
        "ts": 999,
        "exit_fill_quantity": 10,
        "exit_vwap": .90,
        "exit_total_fee": 0,
        "exit_execution_buffer": 0,
        "exit_depth_ok": True,
        "exit_book_fresh": True,
        "exit_observation_semantics": "BOOK_EXECUTABLE_NOT_FILL",
    }

    assert lifecycle.consume(stale_exit, {"m1": market()}) is False
    assert len(lifecycle.data["positions"]) == 1


def test_current_generation_profit_exit_reprices_position_after_entry_id_rebind(tmp_path):
    log = tmp_path / "events.jsonl"
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", log, profit_exit_min_pnl=.10,
        profit_exit_mode="flat",
    )
    entry = accepted("current-entry", outcome="Down")
    entry.update({
        "config_version": "shadow-buy-rules-v10",
        "generation": 4,
        "session": 7,
    })
    assert lifecycle.consume(entry, {"m1": market()}) is True

    exit_quote = {
        **entry,
        "event_id": "current-exit",
        "entry_event_id": "stale-cpp-entry",
        "event_type": "shadow_probability_profit_exit_book_executable",
        "ts": 1010,
        "exit_fill_quantity": 10,
        "exit_vwap": .45,
        "exit_total_fee": .02,
        "exit_execution_buffer": .01,
        "exit_depth_ok": True,
        "exit_book_fresh": True,
        "exit_observation_semantics": "BOOK_EXECUTABLE_NOT_FILL",
    }

    assert lifecycle.consume(exit_quote, {"m1": market()}) is True
    assert lifecycle.data["positions"] == {}
    complete = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert complete["entry_event_id"] == "current-entry"
    assert complete["exit_event_id"] == "current-exit"
    assert complete["realized_simulated_pnl"] == .38


def test_new_session_profit_exit_reprices_position_kept_across_restart(tmp_path):
    log = tmp_path / "events.jsonl"
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", log, profit_exit_min_pnl=.10,
        profit_exit_mode="flat",
    )
    entry = accepted("old-session-entry", outcome="Down")
    entry.update({
        "config_version": "shadow-buy-rules-v10",
        "generation": 4,
        "session": 7,
    })
    assert lifecycle.consume(entry, {"m1": market()}) is True

    exit_quote = {
        **entry,
        "event_id": "new-session-exit",
        "entry_event_id": "new-session-cpp-entry",
        "event_type": "shadow_probability_profit_exit_book_executable",
        "generation": 1,
        "session": 8,
        "ts": 1010,
        "exit_fill_quantity": 10,
        "exit_vwap": .45,
        "exit_total_fee": .02,
        "exit_execution_buffer": .01,
        "exit_depth_ok": True,
        "exit_book_fresh": True,
        "exit_observation_semantics": "BOOK_EXECUTABLE_NOT_FILL",
    }

    assert lifecycle.consume(exit_quote, {"m1": market()}) is True
    complete = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert complete["entry_event_id"] == "old-session-entry"
    assert complete["exit_source_entry_event_id"] == "new-session-cpp-entry"
    assert complete["realized_simulated_pnl"] == .38


def test_terminal_hedge_opens_one_combined_position(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "events.jsonl")
    assert lifecycle.consume(hedged(), {"m1": market()}) is True
    position = next(iter(lifecycle.data["positions"].values()))
    assert position["outcome"] == "Up"
    assert position["hedge_outcome"] == "Down"
    assert position["entry_cost"] == 8.4
    assert position["main_size"] == 10
    assert position["hedge_size"] == 8
    assert position["expected_portfolio_pnl"] == 1.4


def test_inventory_lock_is_completed_immediately_without_fake_market_settlement(tmp_path):
    log = tmp_path / "events.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    row = {
        "event_id": "inventory-lock-1",
        "event_type": "shadow_inventory_action",
        "strategy": "inventory_rebalancing_arb",
        "market_id": "m1",
        "asset": "BTC",
        "timeframe": "5m",
        "decision": "ACCEPT",
        "reason": "inventory_lock",
        "action": "BUY_DOWN_AND_LOCK",
        "projected_locked_quantity": 10,
        "projected_locked_profit": .4,
        "realized_locked_profit": .4,
        "residual_up_quantity": 0,
        "residual_down_quantity": 0,
        "residual_up_cost": 0,
        "residual_down_cost": 0,
        "ts": 1000,
        "config_version": "inventory-rebalancing-v1",
        "config_hash": "inventory-hash",
    }

    assert lifecycle.consume(row, {"m1": market()}) is True

    complete = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert complete["event_type"] == "shadow_complete"
    assert complete["strategy"] == "inventory_rebalancing_arb"
    assert complete["payout"] == 10
    assert complete["entry_cost"] == 9.6
    assert complete["realized_simulated_pnl"] == .4
    assert complete["real_orders"] == 0


def test_legacy_inventory_loss_cap_is_completed_with_bounded_loss(tmp_path):
    log = tmp_path / "events.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    row = {
        "event_id": "legacy-loss-cap",
        "event_type": "shadow_inventory_action",
        "strategy": "inventory_rebalancing_arb",
        "market_id": "m1",
        "asset": "BTC",
        "timeframe": "5m",
        "decision": "ACCEPT",
        "reason": "legacy_inventory_loss_cap",
        "action": "BUY_DOWN_AND_CAP_LOSS",
        "projected_locked_quantity": 10,
        "projected_locked_profit": -.4,
        "realized_locked_profit": -.4,
        "guaranteed_loss": .4,
        "loss_reduction_ratio": .95,
        "residual_up_quantity": 0,
        "residual_down_quantity": 0,
        "residual_up_cost": 0,
        "residual_down_cost": 0,
        "config_version": "inventory-rebalancing-v1",
        "config_hash": "new-config",
        "inventory_origin_config_hash": "old-config",
        "ts": 1000,
    }

    assert lifecycle.consume(row, {"m1": market()}) is True

    complete = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert complete["event_type"] == "shadow_complete"
    assert complete["realized_simulated_pnl"] == -.4
    assert complete["strategy_config_hash"] == "old-config"
    assert lifecycle.data["complete_set_inventory"] == {}


def test_maker_quote_is_observation_only_and_never_counts_as_fill(tmp_path):
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", tmp_path / "events.jsonl"
    )
    row = {
        "event_id": "maker-1",
        "event_type": "shadow_maker_quote_eval",
        "strategy": "maker_complete_set_arb",
        "market_id": "m1",
        "asset": "BTC",
        "timeframe": "5m",
        "decision": "REJECT",
        "reason": "maker_fill_probability_unavailable",
        "up_bid_quote": .48,
        "down_bid_quote": .48,
        "pair_quote_cost": .96,
        "locked_edge_if_both_fill": .04,
        "expected_value": 0,
        "ts": 1000,
    }

    assert lifecycle.consume(row, {"m1": market()}) is False
    assert lifecycle.data["completed"] == []
    assert lifecycle.data["maker_quotes"]["m1"]["pair_quote_cost"] == .96


def test_maker_trade_through_observation_never_opens_position(tmp_path):
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", tmp_path / "execution.jsonl"
    )
    changed = lifecycle.consume({
        "event_id": "maker-through",
        "event_type": "shadow_maker_both_legs_trade_through",
        "strategy": "maker_complete_set_arb",
        "market_id": "m1",
        "decision": "OBSERVED",
        "simulated_fill": False,
        "up_trade_through": True,
        "down_trade_through": True,
    }, {"m1": {"market_id": "m1"}})

    assert changed is False
    assert lifecycle.data["positions"] == {}
    assert lifecycle.data["completed"] == []


def test_split_sell_lock_completes_immediately_with_locked_profit(tmp_path):
    log_path = tmp_path / "execution.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log_path)
    row = {
        "ts": 1000,
        "event_id": "split-sell-1",
        "event_type": "shadow_split_sell_opportunity",
        "strategy": "split_sell_lock",
        "arb_method": "SPLIT_AND_SELL_BOTH",
        "market_id": "m1",
        "asset": "BTC",
        "timeframe": "5m",
        "split_collateral_cost": 10,
        "net_proceeds": 10.22,
        "locked_profit": .22,
        "config_version": "split-sell-shadow-v1",
        "config_hash": "split-hash",
        "decision": "ACCEPT",
    }

    assert lifecycle.consume(row, {}) is True
    assert lifecycle.consume(row, {}) is False
    assert lifecycle.data["positions"] == {}
    assert len(lifecycle.data["completed"]) == 1
    completed = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("event_type") == "shadow_complete"
    ][0]
    assert completed["strategy"] == "split_sell_lock"
    assert completed["entry_cost"] == 10
    assert completed["payout"] == 10.22
    assert completed["realized_simulated_pnl"] == .22


def test_unmatched_complete_set_inventory_is_settled_and_loss_is_recorded(tmp_path):
    log = tmp_path / "events.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    lifecycle.consume({
        "event_id": "inventory-up-1",
        "event_type": "shadow_inventory_action",
        "strategy": "inventory_rebalancing_arb",
        "market_id": "m1",
        "asset": "BTC",
        "timeframe": "5m",
        "decision": "ACCEPT",
        "reason": "inventory_accumulation",
        "action": "BUY_UP",
        "residual_up_quantity": 10,
        "residual_down_quantity": 0,
        "residual_up_cost": 6,
        "residual_down_cost": 0,
        "close_ts": 1100,
        "settlement_source": "chainlink",
        "price_to_beat": 100,
        "config_version": "inventory-rebalancing-v1",
        "config_hash": "inventory-hash",
        "inventory_origin_config_hash": "old-inventory-hash",
        "ts": 1000,
    }, {"m1": market()})
    venue = {"assets": {"BTC": {"chainlink_settlement_samples": [
        {"source_timestamp_ms": 1_100_000, "price": 99},
    ]}}}

    assert lifecycle.settle({"m1": market()}, venue, now=1101) == 1

    complete = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert complete["strategy"] == "inventory_rebalancing_arb"
    assert complete["winning_outcome"] == "Down"
    assert complete["entry_cost"] == 6
    assert complete["payout"] == 0
    assert complete["realized_simulated_pnl"] == -6
    assert complete["strategy_config_hash"] == "old-inventory-hash"


def test_terminal_hedge_settlement_uses_main_or_hedge_payout(tmp_path):
    log = tmp_path / "events.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    lifecycle.consume(hedged(), {"m1": market()})
    venue = {"assets": {"BTC": {"chainlink_settlement_samples": [
        {"source_timestamp_ms": 1_100_000, "price": 99},
    ]}}}
    assert lifecycle.settle({"m1": market()}, venue, now=1101) == 1
    complete = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert complete["winning_outcome"] == "Down"
    assert complete["payout"] == 8
    assert complete["realized_simulated_pnl"] == -.4


def test_chainlink_settlement_completes_winning_up_position(tmp_path):
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    lifecycle.consume(accepted(), {"m1": market()})
    venue = {"assets": {"BTC": {"chainlink_settlement_samples": [
        {"source_timestamp_ms": 1_100_000, "price": 101},
    ]}}}
    assert lifecycle.settle({"m1": dict(market(), open_price=100)}, venue, now=1101) == 1
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert row["event_type"] == "shadow_complete"
    assert row["winning_outcome"] == "Up"
    assert row["realized_simulated_pnl"] == 5.9
    assert row["cash_ledger_version"] == 2
    assert row["deployable_pnl"] is True
    assert row["real_orders"] == 0


def test_binance_settlement_requires_matching_timeframe(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "complete.jsonl")
    row = accepted()
    row["settlement_source"] = "binance"
    row["timeframe"] = "1h"
    row["profitability_cohort_key"] = cohort_key(row)
    lifecycle.consume(row, {"m1": market("binance", "1h")})
    venue = {"assets": {"BTC": {"binance_settlement_samples": [
        {"source_timestamp_ms": 1_100_000, "price": 99, "timeframe": "4h"},
        {"source_timestamp_ms": 1_100_000, "price": 101, "timeframe": "1h"},
    ]}}}
    assert lifecycle.settle({"m1": dict(market("binance", "1h"), open_price=100)}, venue, now=1101) == 1


def test_missing_official_settlement_keeps_position_open(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "complete.jsonl")
    lifecycle.consume(accepted(), {"m1": market()})
    assert lifecycle.settle({"m1": dict(market(), open_price=100)}, {"assets": {}}, now=1200) == 0
    assert len(lifecycle.data["positions"]) == 1


def test_paired_lock_completes_only_after_official_close_sample(tmp_path):
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    assert lifecycle.consume(paired(), {"m1": market()}) is True
    assert lifecycle.settle({"m1": market()}, {"assets": {}}, now=1200) == 0
    venue = {"assets": {"BTC": {"chainlink_settlement_samples": [
        {"source_timestamp_ms": 1_100_000, "price": 101},
    ]}}}
    assert lifecycle.settle({"m1": market()}, venue, now=1200) == 1
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert row["strategy"] == "paired_lock"
    assert row["payout"] == 10
    assert row["realized_simulated_pnl"] == 0.3
    assert row["condition_id"] == "c1"
    assert row["generation"] == 3
    assert row["session"] == 7
    assert row["evaluation_sequence"] == 11
    assert row["strategy_config_version"] == "paired-lock-shadow-v3"
    assert row["strategy_config_hash"] == "paired-hash"
    assert row["timestamp"] == 1200
    assert row["real_fills"] == 0


def test_paired_lock_does_not_require_directional_opening_anchor(tmp_path):
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    row = market()
    row["open_price"] = None
    lifecycle.consume(paired(), {"m1": row})
    venue = {"assets": {"BTC": {"chainlink_settlement_samples": [
        {"source_timestamp_ms": 1_100_000, "price": 101},
    ]}}}
    assert lifecycle.settle({"m1": row}, venue, now=1200) == 1
    complete = json.loads(log.read_text(encoding="utf-8"))
    assert complete["winning_outcome"] is None


def test_strategy_audit_offset_is_persisted_and_accept_is_not_replayed(tmp_path):
    audit = tmp_path / "strategy.jsonl"
    audit.write_text(json.dumps(accepted()) + "\n", encoding="utf-8")
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "complete.jsonl")
    markets = {"m1": market()}
    assert process_audit_once(audit, lifecycle, markets) == 1
    assert process_audit_once(audit, lifecycle, markets) == 0
    assert lifecycle.data["audit_offset"] == audit.stat().st_size


def test_rejected_fixed_horizon_prediction_is_captured_without_opening_trade(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "events.jsonl")

    assert lifecycle.capture_prediction(prediction(), {"m1": market()}) is True

    assert lifecycle.data["positions"] == {}
    assert len(lifecycle.data["probability_predictions"]) == 1
    stored = next(iter(lifecycle.data["probability_predictions"].values()))
    assert stored["origin_decision"] == "REJECT"
    assert stored["estimated_up_probability"] == 0.7
    assert stored["calibration_horizon_seconds"] == 90


def test_dedicated_probability_observation_is_captured_without_opening_trade(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "events.jsonl")

    assert lifecycle.capture_prediction(
        probability_observation(), {"m1": market()}
    ) is True

    assert lifecycle.data["positions"] == {}
    stored = next(iter(lifecycle.data["probability_predictions"].values()))
    assert stored["source_event_type"] == "shadow_prediction_observation"
    assert stored["opens_position"] is False
    assert stored["observation_semantics"] == "PROBABILITY_CALIBRATION_NOT_ORDER"


def test_probability_observation_cannot_claim_to_open_position(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "events.jsonl")
    row = probability_observation("bad-observation")
    row["opens_position"] = True

    assert lifecycle.capture_prediction(row, {"m1": market()}) is False
    assert lifecycle.data["probability_predictions"] == {}


def test_prediction_capture_is_one_up_sample_per_market_model_and_horizon(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "events.jsonl")
    markets = {"m1": market()}

    assert lifecycle.capture_prediction(prediction(), markets) is True
    assert lifecycle.capture_prediction(prediction("prediction-2"), markets) is False
    down = prediction("prediction-down")
    down["outcome"] = "Down"
    assert lifecycle.capture_prediction(down, markets) is False
    too_early = prediction("prediction-early", seconds_to_close=91)
    too_early["market_id"] = "m2"
    assert lifecycle.capture_prediction(
        too_early, {"m2": dict(market(), market_id="m2")}
    ) is False


def test_prediction_capture_requires_valid_probability_inputs(tmp_path):
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "events.jsonl")
    markets = {"m1": market()}
    for field in (
        "estimated_probability", "probability_model_id", "config_hash",
        "price_to_beat", "settlement_reference",
    ):
        row = prediction(f"missing-{field}")
        row[field] = None
        assert lifecycle.capture_prediction(row, markets) is False
    row = prediction("blocked-reference")
    row["reference_quorum_met"] = False
    assert lifecycle.capture_prediction(row, markets) is False
    row = prediction("unverified-settlement")
    row["settlement_source_verified"] = False
    assert lifecycle.capture_prediction(row, markets) is False


def test_prediction_settlement_writes_independent_calibration_event(tmp_path):
    log = tmp_path / "events.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    lifecycle.capture_prediction(prediction(), {"m1": market()})
    venue = {"assets": {"BTC": {"chainlink_settlement_samples": [
        {"source_timestamp_ms": 1_100_000, "price": 101},
    ]}}}

    assert lifecycle.settle({"m1": market()}, venue, now=1101) == 0

    complete = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert complete["event_type"] == "shadow_prediction_complete"
    assert complete["actual_up"] == 1
    assert complete["winning_outcome"] == "Up"
    assert complete["brier_score"] == 0.09
    assert complete["origin_decision"] == "REJECT"
    assert complete["trade_accepted"] is False
    assert lifecycle.data["probability_predictions"] == {}
    assert lifecycle.data["probability_calibration"]["late_window_directional_ev"]["samples"] == 1


def test_cross_strategy_same_market_outcome_is_not_double_opened(tmp_path):
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    markets = {"m1": market()}
    assert lifecycle.consume(accepted(), markets) is True
    assert lifecycle.consume(accepted("l1", "low_price_lottery_ev"), markets) is False
    reject = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert reject["event_type"] == "shadow_position_reject"
    assert reject["reason"] == "correlated_market_outcome_exposure"


def test_directional_and_lottery_share_close_window_risk_limit(tmp_path):
    limits = replace(PortfolioLimits(), combined_max_per_close_window=1)
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log, limits)
    markets = {"m1": market(), "m2": dict(market(), market_id="m2", asset="ETH")}
    assert lifecycle.consume(accepted(), markets) is True
    second = accepted("l1", "low_price_lottery_ev")
    second.update(market_id="m2", asset="ETH")
    second["profitability_cohort_key"] = cohort_key(second)
    assert lifecycle.consume(second, markets) is False
    assert json.loads(log.read_text().splitlines()[-1])["reason"] == "combined_close_window_limit"


def test_default_portfolio_limit_allows_one_directional_risk_per_close_window(tmp_path):
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    markets = {"m1": market(), "m2": dict(market(), market_id="m2", asset="ETH")}
    assert lifecycle.consume(accepted(), markets) is True
    second = accepted("l1", "low_price_lottery_ev")
    second.update(market_id="m2", asset="ETH")
    second["profitability_cohort_key"] = cohort_key(second)
    assert lifecycle.consume(second, markets) is False
    assert json.loads(log.read_text().splitlines()[-1])["reason"] == "combined_close_window_limit"


def test_lottery_close_window_and_total_notional_limits_are_enforced(tmp_path):
    limits = replace(PortfolioLimits(), combined_max_per_close_window=2,
                     lottery_max_per_close_window=1,
                     lottery_max_open_notional=10.0)
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log, limits)
    markets = {"m1": market(), "m2": dict(market(), market_id="m2")}
    assert lifecycle.consume(accepted("l1", "low_price_lottery_ev"), markets) is True
    second = accepted("l2", "low_price_lottery_ev")
    second["market_id"] = "m2"
    second["asset"] = "ETH"
    second["profitability_cohort_key"] = cohort_key(second)
    assert lifecycle.consume(second, markets) is False
    assert json.loads(log.read_text().splitlines()[-1])["reason"] == "lottery_close_window_limit"


def test_lottery_total_open_notional_limit_is_enforced(tmp_path):
    limits = replace(PortfolioLimits(), lottery_max_open_notional=5.0)
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log, limits)
    markets = {"m1": market(), "m2": dict(market(), market_id="m2", close_ts=1300)}
    assert lifecycle.consume(accepted("l1", "low_price_lottery_ev"), markets) is True
    second = accepted("l2", "low_price_lottery_ev")
    second["market_id"] = "m2"
    second["asset"] = "ETH"
    second["profitability_cohort_key"] = cohort_key(second)
    assert lifecycle.consume(second, markets) is False
    assert json.loads(log.read_text().splitlines()[-1])["reason"] == "lottery_open_notional_limit"


def test_strategy_order_size_limits_are_enforced(tmp_path):
    limits = replace(PortfolioLimits(), directional_max_order_size=5,
                     lottery_max_order_size=5)
    log = tmp_path / "complete.jsonl"
    markets = {"m1": market()}
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log, limits)
    assert lifecycle.consume(accepted(), markets) is False
    reject = json.loads(log.read_text().splitlines()[-1])
    assert reject["reason"] == "directional_order_size_limit"
    assert reject["real_fills"] == 0


def test_calibration_mode_never_opens_deployable_position(tmp_path):
    limits = replace(PortfolioLimits(), directional_max_order_size=5)
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", tmp_path / "events.jsonl", limits,
        calibration_mode=True,
    )

    row = accepted()
    assert lifecycle.capture_research_candidate(row, {"m1": market()}) is True
    assert lifecycle.consume(row, {"m1": market()}) is False

    assert lifecycle.data["positions"] == {}
    position = next(iter(lifecycle.data["research_positions"].values()))
    assert position["risk_mode"] == "CALIBRATION_RESEARCH"
    assert position["portfolio_limits_enforced"] is False
    assert position["deployable_pnl"] is False
    assert lifecycle.data["current_risk_halts"] == {}


def test_calibration_mode_can_be_enabled_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SHADOW_CALIBRATION_MODE", "true")
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", tmp_path / "events.jsonl")
    assert lifecycle.data["calibration_mode"] is True
    assert lifecycle.data["portfolio_limits_enforced"] is False


def test_lottery_daily_loss_blocks_new_positions_after_settlement(tmp_path, monkeypatch):
    monkeypatch.setattr("poly_arb_bot.strategy_shadow_lifecycle.time.time", lambda: 1200)
    limits = replace(PortfolioLimits(), lottery_max_daily_loss=0.5)
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log, limits)
    markets = {"m1": market(), "m2": dict(market(), market_id="m2", close_ts=1300)}
    lifecycle.consume(accepted("l1", "low_price_lottery_ev", "Down"), markets)
    venue = {"assets": {"BTC": {"chainlink_settlement_samples": [
        {"source_timestamp_ms": 1_100_000, "price": 101},
    ]}}}
    assert lifecycle.settle(markets, venue, now=1200) == 1
    second = accepted("l2", "low_price_lottery_ev")
    second["market_id"] = "m2"
    second["profitability_cohort_key"] = cohort_key(second)
    assert lifecycle.consume(second, markets) is False
    assert json.loads(log.read_text().splitlines()[-1])["reason"] == "lottery_daily_loss_limit"


def test_existing_completion_log_is_backfilled_for_loss_limits(tmp_path, monkeypatch):
    monkeypatch.setattr("poly_arb_bot.strategy_shadow_lifecycle.time.time", lambda: 1200)
    log = tmp_path / "complete.jsonl"
    log.write_text(json.dumps({
        "ts": 1101, "event_id": "old:complete", "event_type": "shadow_complete",
        "strategy": "late_window_directional_ev", "market_id": "old",
        "strategy_config_hash": canonical_strategy_config_hash(),
        "realized_simulated_pnl": -6.0,
    }) + "\n", encoding="utf-8")
    limits = replace(PortfolioLimits(), directional_max_daily_loss=5.0)
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log, limits)
    assert len(lifecycle.data["completed_trades"]) == 1
    assert lifecycle.consume(accepted("d2"), {"m1": market()}) is False
    assert json.loads(log.read_text().splitlines()[-1])["reason"] == "directional_daily_loss_limit"


def test_old_config_loss_does_not_block_current_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr("poly_arb_bot.strategy_shadow_lifecycle.time.time", lambda: 1200)
    log = tmp_path / "complete.jsonl"
    log.write_text(json.dumps({
        "ts": 1101, "event_id": "old:complete", "event_type": "shadow_complete",
        "strategy": "late_window_directional_ev", "market_id": "old",
        "strategy_config_hash": "old-hash", "realized_simulated_pnl": -100.0,
    }) + "\n", encoding="utf-8")
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    assert lifecycle.consume(accepted("current"), {"m1": market()}) is True


def test_existing_state_trade_hash_is_migrated_from_canonical_log(tmp_path):
    current_hash = canonical_strategy_config_hash()
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "positions": {}, "completed": ["done"], "completed_trades": [{
            "event_id": "done", "strategy": "late_window_directional_ev",
            "market_id": "m1", "ts": 1000, "pnl": -1,
        }],
    }), encoding="utf-8")
    log = tmp_path / "complete.jsonl"
    log.write_text(json.dumps({
        "event_id": "done", "event_type": "shadow_complete",
        "strategy": "late_window_directional_ev", "market_id": "m1",
        "strategy_config_hash": current_hash, "realized_simulated_pnl": -1,
    }), encoding="utf-8")
    lifecycle = StrategyShadowLifecycle(state, log)
    assert lifecycle.data["completed_trades"][0]["strategy_config_hash"] == current_hash


def test_existing_state_trade_hash_is_migrated_from_rotated_log(tmp_path):
    current_hash = canonical_strategy_config_hash()
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "positions": {}, "completed": ["done"], "completed_trades": [{
            "event_id": "done", "strategy": "late_window_directional_ev",
            "market_id": "m1", "ts": 1000, "pnl": -1,
        }],
    }), encoding="utf-8")
    log = tmp_path / "complete.jsonl"
    log.write_text("", encoding="utf-8")
    (tmp_path / "complete.jsonl.1").write_text(json.dumps({
        "event_id": "done", "event_type": "shadow_complete",
        "strategy_config_hash": current_hash,
    }), encoding="utf-8")
    lifecycle = StrategyShadowLifecycle(state, log)
    assert lifecycle.data["completed_trades"][0]["strategy_config_hash"] == current_hash


def test_completed_event_preserves_entry_model_evidence(tmp_path):
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    row = accepted()
    row.update(estimated_probability=.7, net_ev=.2, gross_edge=.3,
               consensus_price=101, settlement_reference=100.8,
               probability_reference_source="settlement_reference",
               probability_reference_price=100.8, reference_state="REFERENCE_READY",
               volatility_per_sqrt_second=.001, up_final_model_z=.5,
               paired_book_imbalance=.2,
               model_sample_span_seconds=120,
               minimum_model_sample_span_seconds=60,
               confidence_type="input_quality_not_historical_accuracy")
    lifecycle.consume(row, {"m1": market()})
    venue = {"assets": {"BTC": {"chainlink_settlement_samples": [
        {"source_timestamp_ms": 1_100_000, "price": 101},
    ]}}}
    lifecycle.settle({"m1": market()}, venue, now=1200)
    complete = json.loads(log.read_text().splitlines()[-1])
    assert complete["estimated_probability"] == .7
    assert complete["net_ev"] == .2
    assert complete["consensus_price"] == 101
    assert complete["settlement_reference"] == 100.8
    assert complete["probability_reference_source"] == "settlement_reference"
    assert complete["probability_reference_price"] == 100.8
    assert complete["volatility_per_sqrt_second"] == .001
    assert complete["up_final_model_z"] == .5
    assert complete["paired_book_imbalance"] == .2
    assert complete["model_sample_span_seconds"] == 120
    assert complete["minimum_model_sample_span_seconds"] == 60

def test_opened_position_has_explicit_active_lifecycle_state(tmp_path):
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json",
        tmp_path / "complete.jsonl",
    )

    assert lifecycle.consume(accepted(), {"m1": market()}) is True

    position = next(iter(lifecycle.data["positions"].values()))
    assert position["lifecycle_state"] == "ACTIVE"


def test_missing_settlement_marks_position_pending_before_orphan_timeout(tmp_path):
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json",
        tmp_path / "complete.jsonl",
        orphan_after_seconds=900,
    )
    lifecycle.consume(accepted(), {"m1": market()})

    assert lifecycle.settle(
        {"m1": market()},
        {"assets": {}},
        now=1200,
    ) == 0

    position = next(iter(lifecycle.data["positions"].values()))
    assert position["lifecycle_state"] == "SETTLEMENT_PENDING"
    assert position["settlement_pending_since"] == 1200
    assert lifecycle.data["orphaned_positions"] == []


def test_unsettled_position_is_orphaned_and_releases_portfolio_capacity(tmp_path):
    log = tmp_path / "complete.jsonl"
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json",
        log,
        orphan_after_seconds=900,
    )
    lifecycle.consume(accepted(), {"m1": market()})

    assert lifecycle.settle(
        {"m1": market()},
        {"assets": {}},
        now=2001,
    ) == 0

    assert lifecycle.data["positions"] == {}
    assert len(lifecycle.data["orphaned_positions"]) == 1

    orphan = lifecycle.data["orphaned_positions"][0]
    assert orphan["lifecycle_state"] == "ORPHANED"
    assert orphan["orphan_reason"] == "settlement_sample_unavailable"
    assert orphan["real_orders"] == 0
    assert orphan["real_order_submissions"] == 0
    assert orphan["real_fills"] == 0
    assert orphan["timestamp"] == 2001

    log_row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert log_row["event_type"] == "shadow_orphaned"


def test_lifecycle_checkpoints_are_dirty_and_coalesced(tmp_path):
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", tmp_path / "events.jsonl",
        checkpoint_interval_seconds=5,
    )
    writes = []
    lifecycle._write_state = lambda: writes.append(dict(lifecycle.data))
    lifecycle._mark_dirty()
    lifecycle._save()
    lifecycle._mark_dirty()
    lifecycle._save()
    assert writes == []
    lifecycle.flush()
    assert len(writes) == 1
    lifecycle.flush()
    assert len(writes) == 1


def _profit_exit(entry, event_id="exit-1"):
    return {
        **entry,
        "event_id": event_id,
        "event_type": "shadow_probability_profit_exit_book_executable",
        "decision": "REJECT",
        "reason": "net_ev_below_threshold",
        "ts": 1010,
        "exit_fill_quantity": 10,
        "exit_vwap": .45,
        "exit_total_fee": .02,
        "exit_execution_buffer": .01,
        "exit_depth_ok": True,
        "exit_book_fresh": True,
        "exit_observation_semantics": "BOOK_EXECUTABLE_NOT_FILL",
    }


def test_research_and_deployable_admission_are_isolated(tmp_path):
    markets = {"m1": market()}
    blocked = accepted()
    blocked.update(
        profitability_gate_decision="BLOCK",
        profitability_gate_reason="profitability_gate_no_eligible_cohort",
        deployable_candidate=False,
    )
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "blocked-state.json", tmp_path / "blocked-events.jsonl",
    )

    assert lifecycle.capture_research_candidate(blocked, markets) is True
    assert lifecycle.consume(blocked, markets) is False
    assert lifecycle.data["positions"] == {}
    assert len(lifecycle.data["research_positions"]) == 1
    reject = json.loads(
        (tmp_path / "blocked-events.jsonl").read_text().splitlines()[-1]
    )
    assert reject["reason"] == "profitability_gate_no_eligible_cohort"

    allowed_lifecycle = StrategyShadowLifecycle(
        tmp_path / "allowed-state.json", tmp_path / "allowed-events.jsonl",
    )
    allowed = accepted()
    assert allowed_lifecycle.capture_research_candidate(allowed, markets) is True
    assert allowed_lifecycle.consume(allowed, markets) is True
    deployable = next(iter(allowed_lifecycle.data["positions"].values()))
    research = next(iter(allowed_lifecycle.data["research_positions"].values()))
    assert deployable["deployable_pnl"] is True
    assert deployable["portfolio_limits_enforced"] is True
    assert research["deployable_pnl"] is False
    assert research["portfolio_limits_enforced"] is False
    assert deployable["entry_cost"] == research["entry_cost"] == 4.1
    assert deployable["risk_adjusted_entry_cost"] == research[
        "risk_adjusted_entry_cost"] == 4.3


@pytest.mark.parametrize("field,value,reason", [
    ("deployable_candidate", False, "deployable_candidate_not_true"),
    (
        "profitability_gate_reason", "eligible_cohort",
        "profitability_gate_classification_invalid",
    ),
    (
        "profitability_cohort_key", "",
        "profitability_gate_classification_invalid",
    ),
    (
        "profitability_gate_hash", "not-a-hash",
        "profitability_gate_classification_invalid",
    ),
    (
        "calibration_snapshot_hash", None,
        "profitability_gate_classification_invalid",
    ),
    (
        "config_hash", "c" * 64,
        "probability_identity_mismatch",
    ),
    (
        "probability_model_id", "wrong-model",
        "probability_identity_mismatch",
    ),
])
def test_deployable_requires_complete_consistent_task4_classification(
        tmp_path, field, value, reason):
    log = tmp_path / "events.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    row = accepted()
    row[field] = value

    if field in {
        "deployable_candidate", "profitability_gate_reason",
        "profitability_cohort_key", "profitability_gate_hash",
        "calibration_snapshot_hash",
    }:
        assert lifecycle.capture_research_candidate(
            row, {"m1": market()},
        ) is True
    assert lifecycle.consume(row, {"m1": market()}) is False
    assert lifecycle.data["positions"] == {}
    assert lifecycle.data["deployable_claimed_markets"] == []
    reject = json.loads(log.read_text().splitlines()[-1])
    assert reject["reason"] == reason


@pytest.mark.parametrize("mutate,reason", [
    (
        lambda row: row.update(settlement_source_verified=False),
        "settlement_provenance_unverified",
    ),
    (
        lambda row: row.update(settlement_source="binance"),
        "settlement_provenance_mismatch",
    ),
    (
        lambda row: row.update(dynamic_risk_adjusted_cost=4.2),
        "invalid_dynamic_cash_evidence",
    ),
    (
        lambda row: row.update(config_version="shadow-buy-rules-v9"),
        "unsupported_probability_config_version",
    ),
    (
        lambda row: row.update(real_orders=1),
        "real_order_invariant",
    ),
    (
        lambda row: row.update(ts="not-a-timestamp"),
        "invalid_probability_entry_timestamp",
    ),
    (
        lambda row: row.update(condition_id=None),
        "probability_identity_mismatch",
    ),
])
def test_probability_entry_reject_reasons_are_specific(
        tmp_path, mutate, reason):
    log = tmp_path / "events.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    row = accepted()
    mutate(row)

    assert lifecycle.consume(row, {"m1": market()}) is False
    assert lifecycle.data["positions"] == {}
    assert lifecycle.data["deployable_claimed_markets"] == []
    reject = json.loads(log.read_text().splitlines()[-1])
    assert reject["reason"] == reason


def test_calibration_mode_is_research_only_even_when_gate_allows(tmp_path):
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", tmp_path / "events.jsonl",
        calibration_mode=True,
    )
    row = accepted()

    assert lifecycle.capture_research_candidate(row, {"m1": market()}) is True
    assert lifecycle.consume(row, {"m1": market()}) is False
    assert lifecycle.data["positions"] == {}
    research = next(iter(lifecycle.data["research_positions"].values()))
    assert research["risk_mode"] == "CALIBRATION_RESEARCH"
    assert research["deployable_pnl"] is False


@pytest.mark.parametrize("field,value", [
    ("settlement_source", None),
    ("settlement_source", "unverified"),
    ("settlement_source_verified", False),
    ("settlement_source_verified", None),
])
def test_probability_position_capture_requires_verified_entry_settlement_source(
        tmp_path, field, value):
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", tmp_path / "events.jsonl",
    )
    row = accepted()
    row[field] = value

    assert lifecycle.capture_research_candidate(row, {"m1": market()}) is False
    assert lifecycle.consume(row, {"m1": market()}) is False
    assert lifecycle.data["research_positions"] == {}
    assert lifecycle.data["positions"] == {}


def test_same_bid_exit_completes_both_ledgers_and_claims_prevent_reopen(tmp_path):
    log = tmp_path / "events.jsonl"
    state = tmp_path / "state.json"
    markets = {"m1": market()}
    lifecycle = StrategyShadowLifecycle(
        state, log, profit_exit_min_pnl=.10, profit_exit_mode="flat",
    )
    entry = accepted()
    assert lifecycle.capture_research_candidate(entry, markets) is True
    assert lifecycle.consume(entry, markets) is True

    assert lifecycle.consume(_profit_exit(entry), markets) is True
    assert lifecycle.data["positions"] == {}
    assert lifecycle.data["research_positions"] == {}
    events = [json.loads(line) for line in log.read_text().splitlines()]
    completions = [
        row for row in events
        if row["event_type"] in {"shadow_complete", "shadow_research_complete"}
    ]
    assert {row["event_type"] for row in completions} == {
        "shadow_complete", "shadow_research_complete",
    }
    assert next(row for row in completions if row["event_type"] == "shadow_complete")[
        "deployable_pnl"] is True
    assert next(
        row for row in completions
        if row["event_type"] == "shadow_research_complete"
    )["deployable_pnl"] is False
    assert all(row["net_pnl_usd"] == .38 for row in completions)
    assert all(row["net_return_per_dollar_risked"] == pytest.approx(.38 / 4.1)
               for row in completions)

    restarted = StrategyShadowLifecycle(
        state, log, profit_exit_min_pnl=.10, profit_exit_mode="flat",
    )
    reopened = accepted("a2")
    assert restarted.capture_research_candidate(reopened, markets) is False
    assert restarted.consume(reopened, markets) is False
    assert restarted.data["positions"] == {}
    assert restarted.data["research_positions"] == {}
    assert len(restarted.data["research_claimed_markets"]) == 1
    assert len(restarted.data["deployable_claimed_markets"]) == 1


def test_same_settlement_evidence_completes_both_ledgers_with_entry_provenance(
        tmp_path):
    log = tmp_path / "events.jsonl"
    lifecycle = StrategyShadowLifecycle(tmp_path / "state.json", log)
    markets = {"m1": market()}
    entry = accepted()
    assert lifecycle.capture_research_candidate(entry, markets) is True
    assert lifecycle.consume(entry, markets) is True
    venue = {"assets": {"BTC": {"chainlink_settlement_samples": [
        {"source_timestamp_ms": 1_100_000, "price": 101},
    ]}}}

    lifecycle.settle(markets, venue, now=1101)

    completions = [
        json.loads(line) for line in log.read_text().splitlines()
        if json.loads(line)["event_type"] in {
            "shadow_complete", "shadow_research_complete",
        }
    ]
    assert {row["event_type"] for row in completions} == {
        "shadow_complete", "shadow_research_complete",
    }
    assert all(row["settlement_source"] == "chainlink" for row in completions)
    assert all(row["settlement_source_verified"] is True for row in completions)
    assert all(row["real_order_submissions"] == 0 for row in completions)
    assert all(row["real_orders"] == 0 for row in completions)
    assert all(row["real_fills"] == 0 for row in completions)


def test_lifecycle_settlement_is_eligible_for_profitability_reconciliation(
        tmp_path):
    strategy_audit = tmp_path / "strategy-audit.jsonl"
    execution_log = tmp_path / "shadow-execution.jsonl"
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", execution_log,
    )
    markets = {"m1": market()}
    entry = accepted()
    strategy_audit.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    assert lifecycle.capture_research_candidate(entry, markets) is True
    assert lifecycle.consume(entry, markets) is True
    lifecycle.settle(
        markets,
        {"assets": {"BTC": {"chainlink_settlement_samples": [
            {"source_timestamp_ms": 1_100_000, "price": 101},
        ]}}},
        now=1101,
    )

    report = build_profitability_report(
        strategy_audit,
        execution_log,
        {"late_window_directional_ev": entry["config_hash"]},
    )

    assert report["overall"]["independent_markets"] == 1
    assert report["excluded"].get("settlement_provenance_unverified", 0) == 0
    assert report["real_order_submissions"] == 0
    assert report["real_orders"] == 0
    assert report["real_fills"] == 0


def test_process_audit_captures_research_before_gate_reject(tmp_path):
    audit = tmp_path / "strategy.jsonl"
    row = accepted()
    row.update(
        profitability_gate_decision="BLOCK",
        profitability_gate_reason="profitability_gate_unavailable",
        deployable_candidate=False,
    )
    audit.write_text(json.dumps(row) + "\n", encoding="utf-8")
    lifecycle = StrategyShadowLifecycle(
        tmp_path / "state.json", tmp_path / "events.jsonl",
    )

    assert process_audit_once(audit, lifecycle, {"m1": market()}) == 0
    assert len(lifecycle.data["research_positions"]) == 1
    assert lifecycle.data["positions"] == {}

