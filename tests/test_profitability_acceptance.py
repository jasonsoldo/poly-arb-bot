import json
from pathlib import Path

import pytest

from poly_arb_bot import cli
from poly_arb_bot import profitability_acceptance
from poly_arb_bot.ev_shadow import canonical_strategy_base_config_hash
from poly_arb_bot.probability_calibration_map import (
    PROBABILITY_MODEL_IDS,
    freeze_calibration_snapshot,
)
from poly_arb_bot.profitability_analysis import cohort_key
from poly_arb_bot.profitability_gate import (
    build_profitability_gate,
    publish_profitability_gate,
)


ACTIVATED = 10_000.0
NOW = ACTIVATED + 48 * 3600 + 100
STRATEGY = "late_window_directional_ev"


def _write_jsonl(path, rows):
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _cohort_row(asset="BTC"):
    return {
        "strategy": STRATEGY,
        "asset": asset,
        "timeframe": "5m",
        "outcome": "Up",
        "calibration_input_probability": 0.75,
        "expected_fill_price": 0.4,
        "seconds_to_close": 20.0,
    }


def _cohort(asset="BTC"):
    return {
        "dimensions": {
            "strategy": STRATEGY,
            "asset": asset,
            "timeframe": "5m",
            "outcome": "Up",
            "probability": "0.7-0.8",
            "fill": "0.4-0.5",
            "seconds": "0-30",
        },
        "independent_markets": 60,
        "mean_net_return": 0.05,
        "net_pnl_usd": 12.0,
        "lower_bound_95": 0.01,
        "largest_positive_market_share": 0.10,
    }


def _calibration_map(base_hash, lottery_base_hash):
    return {
        "version": 2,
        "generated_at": ACTIVATED - 100,
        "config": {"min_bucket_samples": 30, "prior_weight": 30.0},
        "excluded_other_cohort": {
            "late_window_directional_ev": 0,
            "low_price_lottery_ev": 0,
        },
        "strategies": {
            STRATEGY: {
                "cohort": {
                    "strategy_config_hash": base_hash,
                    "probability_model_id": PROBABILITY_MODEL_IDS[STRATEGY],
                },
                "timeframes": {
                    "5m": {
                        "0.7-0.8": {
                            "samples": 60,
                            "expected_up_rate": 0.75,
                            "realized_up_rate": 0.8,
                        },
                    },
                },
                "overall": {
                    "0.7-0.8": {
                        "samples": 60,
                        "expected_up_rate": 0.75,
                        "realized_up_rate": 0.8,
                    },
                },
            },
            "low_price_lottery_ev": {
                "cohort": {
                    "strategy_config_hash": lottery_base_hash,
                    "probability_model_id": PROBABILITY_MODEL_IDS[
                        "low_price_lottery_ev"
                    ],
                },
                "timeframes": {
                    "5m": {
                        "0.0-0.1": {
                            "samples": 60,
                            "expected_up_rate": 0.05,
                            "realized_up_rate": 0.05,
                        },
                    },
                },
                "overall": {
                    "0.0-0.1": {
                        "samples": 60,
                        "expected_up_rate": 0.05,
                        "realized_up_rate": 0.05,
                    },
                },
            },
        },
    }


def _artifacts(tmp_path, monkeypatch, assets=("BTC",), capital="1000"):
    monkeypatch.setenv("SHADOW_SIZING_CAPITAL_USD", capital)
    monkeypatch.setenv("SHADOW_CASH_LEDGER_VERSION", "2")
    monkeypatch.setenv("PROFITABILITY_COHORT_VERSION", "1")
    monkeypatch.setenv("PROFITABILITY_GATE_ENABLE", "1")
    base_hash = canonical_strategy_base_config_hash(STRATEGY)
    lottery_base_hash = canonical_strategy_base_config_hash(
        "low_price_lottery_ev"
    )
    research = tmp_path / "probability-calibration-research.json"
    snapshot_path = tmp_path / "probability-calibration-validation.json"
    gate_path = tmp_path / "profitability-gates.json"
    research.write_text(
        json.dumps(_calibration_map(base_hash, lottery_base_hash)),
        encoding="utf-8",
    )
    snapshot = freeze_calibration_snapshot(
        research, snapshot_path, now=ACTIVATED,
    )
    cohorts = {
        cohort_key(_cohort_row(asset)): _cohort(asset)
        for asset in assets
    }
    report = {
        "version": 1,
        "generated_at": ACTIVATED - 1,
        "source": {
            "strategy_audit": {"path": "audit.jsonl", "files": []},
            "execution_log": {"path": "execution.jsonl", "files": []},
        },
        "selected_config_hashes": {STRATEGY: base_hash},
        "completion_lifecycle": "research",
        "blocking_exclusions": {},
        "cohorts": cohorts,
        "real_order_submissions": 0,
        "real_orders": 0,
        "real_fills": 0,
    }
    gate = build_profitability_gate(
        report,
        snapshot,
        {STRATEGY: base_hash},
        now=ACTIVATED,
        cohort_version="1",
    )
    publish_profitability_gate(gate, gate_path)
    monkeypatch.setenv(
        "PROBABILITY_VALIDATION_CALIBRATION_PATH", str(snapshot_path),
    )
    monkeypatch.setenv("PROFITABILITY_GATE_PATH", str(gate_path))
    state_path = tmp_path / "strategy-shadow.json"
    state_path.write_text(json.dumps({
        "calibration_mode": False,
        "portfolio_limits_enforced": True,
        "risk_mode": "PORTFOLIO_LIMITS_ENFORCED",
        "real_order_submissions": 0,
        "real_orders": 0,
        "real_fills": 0,
    }), encoding="utf-8")
    return gate_path, state_path, gate


def _final_config_hash(gate):
    return profitability_acceptance.expected_strategy_config_hash(
        gate, STRATEGY,
    )


def _completion(gate, index, *, asset="BTC", pnl=1.0, market_id=None):
    entry_ts = ACTIVATED + 10 + index * 500
    close_ts = entry_ts + 20
    target_size = 2.0 if pnl >= 0 else 1.0
    entry_cost = 1.0
    payout = entry_cost + pnl
    row = {
        **_cohort_row(asset),
        "event_id": f"complete-{asset}-{index}",
        "entry_event_id": f"entry-{asset}-{index}",
        "event_type": "shadow_complete",
        "strategy_config_hash": _final_config_hash(gate),
        "probability_model_id": PROBABILITY_MODEL_IDS[STRATEGY],
        "market_id": market_id or f"market-{asset}-{index}",
        "condition_id": f"condition-{asset}-{index}",
        "price_to_beat": 100.0,
        "entry_ts": entry_ts,
        "close_ts": close_ts,
        "ts": close_ts + 1,
        "target_size": target_size,
        "dynamic_vwap": 0.4,
        "dynamic_fee": entry_cost - target_size * 0.4,
        "entry_cost": entry_cost,
        "winning_outcome": "Up" if payout else "Down",
        "settlement_price": 101.0 if payout else 99.0,
        "settlement_timestamp_ms": close_ts * 1000,
        "settlement_source": "chainlink",
        "settlement_source_verified": True,
        "payout": payout,
        "realized_simulated_pnl": pnl,
        "net_pnl_usd": pnl,
        "net_return_per_dollar_risked": pnl / entry_cost,
        "profitability_cohort_key": cohort_key(_cohort_row(asset)),
        "profitability_gate_decision": "ALLOW",
        "profitability_gate_reason": "profitability_cohort_eligible",
        "profitability_gate_hash": gate["content_hash"],
        "calibration_snapshot_hash": gate["calibration_snapshot_hash"],
        "deployable_candidate": True,
        "deployable_pnl": True,
        "cash_ledger_version": 2,
        "risk_mode": "PORTFOLIO_LIMITS_ENFORCED",
        "portfolio_limits_enforced": True,
        "real_order_submissions": 0,
        "real_orders": 0,
        "real_fills": 0,
    }
    return row


def _profit_exit(gate, index):
    row = _completion(gate, index)
    row.update({
        "ts": row["entry_ts"] + 10,
        "completion_reason": "profit_target_book_executable",
        "exit_fill_quantity": row["target_size"],
        "exit_vwap": 0.6,
        "exit_total_fee": 0.1,
        "exit_cash_proceeds": 1.1,
        "exit_depth_ok": True,
        "exit_book_fresh": True,
        "exit_observation_semantics": "BOOK_EXECUTABLE_NOT_FILL",
        "payout": 1.1,
        "realized_simulated_pnl": 0.1,
        "net_pnl_usd": 0.1,
        "net_return_per_dollar_risked": 0.1,
    })
    return row


def _build(tmp_path, gate_path, state_path, gate, rows, now=NOW):
    execution = tmp_path / "shadow-execution.jsonl"
    _write_jsonl(execution, rows)
    result = profitability_acceptance.build_profitability_acceptance(
        execution, gate_path, state_path, now=now,
    )
    return execution, result


def test_passes_only_after_full_forward_window_and_300_markets(
    tmp_path, monkeypatch,
):
    gate_path, state_path, gate = _artifacts(tmp_path, monkeypatch)
    rows = [_completion(gate, index) for index in range(300)]

    execution, result = _build(
        tmp_path, gate_path, state_path, gate, rows,
    )

    assert result["status"] == "PASS"
    assert result["reason"] == "profitability_validation_passed"
    assert result["metrics"]["runtime_seconds"] >= 48 * 3600
    assert result["metrics"]["independent_markets"] == 300
    assert result["metrics"]["total_pnl_usd"] == 300
    assert result["metrics"]["lower_bound_95"] > 0
    output = tmp_path / "profitability-acceptance.json"
    assert profitability_acceptance.run(
        execution, gate_path, state_path, output, now=NOW,
    ) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "PASS"
    assert not output.with_suffix(".json.tmp").exists()


@pytest.mark.parametrize(
    "now,count,reason",
    [
        (ACTIVATED + 47 * 3600, 300, "minimum_runtime_not_met"),
        (NOW, 299, "minimum_independent_markets_not_met"),
    ],
)
def test_insufficient_runtime_or_total_sample_is_incomplete(
    tmp_path, monkeypatch, now, count, reason,
):
    gate_path, state_path, gate = _artifacts(tmp_path, monkeypatch)
    rows = [_completion(gate, index) for index in range(count)]

    _, result = _build(
        tmp_path, gate_path, state_path, gate, rows, now=now,
    )

    assert result["status"] == "INCOMPLETE"
    assert result["reason"] == reason
    assert result["exit_code"] == 2


def test_one_enabled_cohort_below_50_is_incomplete(tmp_path, monkeypatch):
    gate_path, state_path, gate = _artifacts(
        tmp_path, monkeypatch, assets=("BTC", "ETH"),
    )
    rows = (
        [_completion(gate, index, asset="BTC") for index in range(251)]
        + [_completion(gate, index, asset="ETH") for index in range(49)]
    )

    _, result = _build(tmp_path, gate_path, state_path, gate, rows)

    assert result["status"] == "INCOMPLETE"
    assert result["reason"] == "minimum_cohort_samples_not_met"
    assert min(result["sample_counts"].values()) == 49


def test_economic_loss_and_excess_drawdown_fail(tmp_path, monkeypatch):
    gate_path, state_path, gate = _artifacts(tmp_path, monkeypatch)
    losing = [_completion(gate, index, pnl=-1.0) for index in range(300)]
    _, loss_result = _build(
        tmp_path, gate_path, state_path, gate, losing,
    )
    assert loss_result["status"] == "FAIL"
    assert loss_result["reason"] == "total_pnl_not_positive"

    gate_path, state_path, gate = _artifacts(
        tmp_path, monkeypatch, capital="5",
    )
    drawdown = [_completion(gate, 0, pnl=-1.0)] + [
        _completion(gate, index, pnl=1.0)
        for index in range(1, 300)
    ]
    _, drawdown_result = _build(
        tmp_path, gate_path, state_path, gate, drawdown,
    )
    assert drawdown_result["status"] == "FAIL"
    assert drawdown_result["reason"] == "maximum_drawdown_exceeded"


def test_positive_pnl_with_nonpositive_lower_bound_is_incomplete(
    tmp_path, monkeypatch,
):
    gate_path, state_path, gate = _artifacts(tmp_path, monkeypatch)
    rows = [_completion(gate, index) for index in range(300)]
    monkeypatch.setattr(
        profitability_acceptance, "block_bootstrap_lower_bound",
        lambda rows, seed_material: 0.0,
    )

    _, result = _build(tmp_path, gate_path, state_path, gate, rows)

    assert result["status"] == "INCOMPLETE"
    assert result["reason"] == "lower_bound_95_not_positive"


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda row: row.__setitem__("net_pnl_usd", 99), "ledger_corrupt"),
        (lambda row: row.__setitem__("real_orders", 1), "real_order_invariant"),
    ],
)
def test_corrupt_ledger_or_nonzero_real_order_fails(
    tmp_path, monkeypatch, mutation, reason,
):
    gate_path, state_path, gate = _artifacts(tmp_path, monkeypatch)
    rows = [_completion(gate, index) for index in range(300)]
    mutation(rows[0])

    _, result = _build(tmp_path, gate_path, state_path, gate, rows)

    assert result["status"] == "FAIL"
    assert result["reason"] == reason
    assert result["exit_code"] == 1


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("strategy_config_hash", "f" * 64, "strategy_config_hash_mismatch"),
        ("profitability_gate_hash", "e" * 64, "profitability_gate_hash_mismatch"),
        ("calibration_snapshot_hash", "d" * 64, "calibration_snapshot_hash_mismatch"),
    ],
)
def test_current_forward_identity_mismatch_is_configuration_error(
    tmp_path, monkeypatch, field, value, reason,
):
    gate_path, state_path, gate = _artifacts(tmp_path, monkeypatch)
    rows = [_completion(gate, index) for index in range(300)]
    rows[0][field] = value

    _, result = _build(tmp_path, gate_path, state_path, gate, rows)

    assert result["status"] == "INCOMPLETE"
    assert result["classification"] == "CONFIGURATION_ERROR"
    assert result["reason"] == reason
    assert result["exit_code"] == 3


def test_duplicate_market_does_not_increase_independent_sample_count(
    tmp_path, monkeypatch,
):
    gate_path, state_path, gate = _artifacts(tmp_path, monkeypatch)
    rows = [_completion(gate, index) for index in range(300)]
    duplicate = _completion(gate, 299, market_id="market-BTC-0")
    duplicate["event_id"] = "duplicate-market-complete"
    duplicate["entry_event_id"] = "duplicate-market-entry"
    rows.append(duplicate)

    _, result = _build(tmp_path, gate_path, state_path, gate, rows)

    assert result["status"] == "PASS"
    assert result["metrics"]["independent_markets"] == 300
    assert result["excluded"]["duplicate_market"] == 1


def test_profit_exit_before_close_reconciles_but_false_settlement_winner_fails(
    tmp_path, monkeypatch,
):
    gate_path, state_path, gate = _artifacts(tmp_path, monkeypatch)
    rows = [_profit_exit(gate, index) for index in range(300)]
    _, result = _build(tmp_path, gate_path, state_path, gate, rows)
    assert result["status"] == "PASS"

    settlement_rows = [_completion(gate, index) for index in range(300)]
    settlement_rows[0]["settlement_price"] = 99.0
    _, corrupt = _build(
        tmp_path, gate_path, state_path, gate, settlement_rows,
    )
    assert corrupt["status"] == "FAIL"
    assert corrupt["reason"] == "ledger_corrupt"


def test_acceptance_loader_rejects_shallow_or_tampered_pass(tmp_path, monkeypatch):
    gate_path, state_path, gate = _artifacts(tmp_path, monkeypatch)
    execution, result = _build(
        tmp_path,
        gate_path,
        state_path,
        gate,
        [_completion(gate, index) for index in range(300)],
    )
    output = tmp_path / "profitability-acceptance.json"
    assert profitability_acceptance.run(
        execution, gate_path, state_path, output, now=NOW,
    ) == 0
    loaded, reason = profitability_acceptance.load_profitability_acceptance(
        output,
        NOW,
        expected_gate_hash=gate["content_hash"],
        expected_snapshot_hash=gate["calibration_snapshot_hash"],
    )
    assert reason is None
    assert loaded == result

    output.write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
    assert profitability_acceptance.load_profitability_acceptance(
        output, NOW,
    ) == (None, "profitability_acceptance_invalid")

    tampered = dict(result)
    tampered["metrics"] = dict(tampered["metrics"])
    tampered["metrics"]["independent_markets"] = 1
    output.write_text(json.dumps(tampered), encoding="utf-8")
    assert profitability_acceptance.load_profitability_acceptance(
        output, NOW,
    ) == (None, "profitability_acceptance_hash_mismatch")


def test_cli_wires_profitability_acceptance_paths(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(
        profitability_acceptance,
        "run",
        lambda *args: captured.append(args) or 2,
    )
    execution = tmp_path / "execution.jsonl"
    gate = tmp_path / "gate.json"
    state = tmp_path / "state.json"
    output = tmp_path / "acceptance.json"
    monkeypatch.setattr("sys.argv", [
        "poly-arb-bot",
        "profitability-acceptance",
        "--execution-log", str(execution),
        "--gate-file", str(gate),
        "--strategy-state", str(state),
        "--acceptance-output", str(output),
    ])

    assert cli.main() == 2
    assert captured == [(execution, gate, state, output)]


def test_run_refuses_to_overwrite_any_acceptance_input(tmp_path, monkeypatch):
    gate_path, state_path, gate = _artifacts(tmp_path, monkeypatch)
    execution = tmp_path / "shadow-execution.jsonl"
    _write_jsonl(
        execution, [_completion(gate, index) for index in range(300)],
    )
    original_gate = gate_path.read_bytes()

    assert profitability_acceptance.run(
        execution, gate_path, state_path, gate_path, now=NOW,
    ) == 3
    assert gate_path.read_bytes() == original_gate
