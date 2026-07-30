import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from poly_arb_bot.ev_shadow import (
    canonical_strategy_base_config_hash,
    canonical_strategy_config_hash,
)
from poly_arb_bot.probability_calibration_map import (
    PROBABILITY_MODEL_IDS,
    freeze_calibration_snapshot,
    load_frozen_calibration_snapshot,
)
from poly_arb_bot.profitability_analysis import cohort_key
from poly_arb_bot.profitability_gate import (
    build_profitability_gate,
    canonical_payload_hash,
    evaluate_profitability_gate,
    load_profitability_gate,
    publish_profitability_gate,
)


NOW = 1000.0
STRATEGY = "late_window_directional_ev"
BASE_HASH = "directional-base-hash"
COHORT_VERSION = "7"


def _calibration_map():
    return {
        "version": 2,
        "generated_at": 900.0,
        "config": {"min_bucket_samples": 30, "prior_weight": 30.0},
        "excluded_other_cohort": {
            "late_window_directional_ev": 0,
            "low_price_lottery_ev": 0,
        },
        "strategies": {
            STRATEGY: {
                "cohort": {
                    "strategy_config_hash": BASE_HASH,
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
                    "strategy_config_hash": "lottery-base-hash",
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


def _row():
    return {
        "strategy": STRATEGY,
        "asset": "BTC",
        "timeframe": "5m",
        "outcome": "Up",
        "calibration_input_probability": 0.75,
        "expected_fill_price": 0.65,
        "seconds_to_close": 20.0,
    }


def _cohort(markets=60, mean=0.05, lower=0.01, share=0.10):
    return {
        "dimensions": {
            "strategy": STRATEGY,
            "asset": "BTC",
            "timeframe": "5m",
            "outcome": "Up",
            "probability": "0.7-0.8",
            "fill": "0.6-0.7",
            "seconds": "0-30",
        },
        "independent_markets": markets,
        "mean_net_return": mean,
        "net_pnl_usd": 12.0,
        "lower_bound_95": lower,
        "largest_positive_market_share": share,
    }


def _report(cohorts=None, blocking=None):
    return {
        "version": 1,
        "generated_at": 900.0,
        "source": {
            "strategy_audit": {"path": "audit.jsonl", "files": []},
            "execution_log": {"path": "execution.jsonl", "files": []},
        },
        "selected_config_hashes": {STRATEGY: BASE_HASH},
        "blocking_exclusions": blocking or {},
        "cohorts": cohorts if cohorts is not None else {
            cohort_key(_row()): _cohort(),
        },
        "real_order_submissions": 0,
        "real_orders": 0,
        "real_fills": 0,
    }


def _snapshot(tmp_path):
    source = tmp_path / "research.json"
    destination = tmp_path / "validation.json"
    source.write_text(json.dumps(_calibration_map()), encoding="utf-8")
    return freeze_calibration_snapshot(source, destination, now=NOW)


def _gate(tmp_path, report=None):
    snapshot = _snapshot(tmp_path)
    payload = build_profitability_gate(
        report or _report(),
        snapshot,
        {STRATEGY: BASE_HASH},
        now=NOW,
        cohort_version=COHORT_VERSION,
    )
    return snapshot, payload


def _expected(snapshot):
    return {
        "strategy_base_config_hash": BASE_HASH,
        "probability_model_id": PROBABILITY_MODEL_IDS[STRATEGY],
        "calibration_snapshot_hash": snapshot["content_hash"],
        "profitability_cohort_version": COHORT_VERSION,
    }


def test_freeze_snapshot_hashes_atomic_copy_without_mutating_source(tmp_path):
    source = tmp_path / "probability-calibration-research.json"
    destination = tmp_path / "probability-calibration-validation.json"
    source_payload = _calibration_map()
    source.write_text(json.dumps(source_payload), encoding="utf-8")

    snapshot = freeze_calibration_snapshot(source, destination, now=NOW)

    assert snapshot["validation_activated_at"] == NOW
    assert snapshot["validation_expires_at"] == NOW + 72 * 3600
    assert snapshot["content_hash"] == canonical_payload_hash(snapshot)
    assert json.loads(source.read_text(encoding="utf-8")) == source_payload
    assert not destination.with_suffix(".json.tmp").exists()
    assert json.loads(destination.read_text(encoding="utf-8")) == snapshot


def test_freeze_rejects_non_finite_activation_without_replacing_destination(
    tmp_path,
):
    source = tmp_path / "research.json"
    destination = tmp_path / "validation.json"
    source.write_text(json.dumps(_calibration_map()), encoding="utf-8")
    destination.write_text("sentinel", encoding="utf-8")

    with pytest.raises(ValueError, match="activation"):
        freeze_calibration_snapshot(source, destination, now=float("nan"))

    assert destination.read_text(encoding="utf-8") == "sentinel"


def test_frozen_snapshot_rejects_content_changed_after_hashing(tmp_path):
    snapshot = _snapshot(tmp_path)
    path = tmp_path / "validation.json"
    snapshot["strategies"][STRATEGY]["timeframes"]["5m"]["0.7-0.8"][
        "samples"
    ] = 61
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    loaded, reason = load_frozen_calibration_snapshot(path, now=NOW + 1)

    assert loaded is None
    assert reason == "calibration_snapshot_hash_mismatch"


def test_frozen_snapshot_rejects_expiry_and_expected_hash_mismatch(tmp_path):
    snapshot = _snapshot(tmp_path)
    path = tmp_path / "validation.json"

    assert load_frozen_calibration_snapshot(
        path, now=NOW + 72 * 3600 + 1
    ) == (None, "calibration_snapshot_expired")
    assert load_frozen_calibration_snapshot(
        path, now=NOW + 72 * 3600
    ) == (None, "calibration_snapshot_expired")
    assert load_frozen_calibration_snapshot(
        path, now=float("nan")
    ) == (None, "calibration_snapshot_invalid")
    assert load_frozen_calibration_snapshot(
        path, now=NOW + 1, expected_content_hash="wrong"
    ) == (None, "calibration_snapshot_hash_mismatch")
    assert load_frozen_calibration_snapshot(
        path, now=NOW + 1, expected_content_hash=snapshot["content_hash"]
    )[0] == snapshot


def test_gate_allows_only_cohorts_that_meet_every_profitability_threshold(tmp_path):
    eligible_key = cohort_key(_row())
    insufficient_row = {**_row(), "asset": "ETH"}
    concentrated_row = {**_row(), "asset": "SOL"}
    report = _report({
        eligible_key: _cohort(),
        cohort_key(insufficient_row): _cohort(markets=49),
        cohort_key(concentrated_row): _cohort(share=0.30),
    })

    snapshot, payload = _gate(tmp_path, report)

    assert payload["decision"] == "ALLOW"
    assert list(payload["eligible_cohorts"]) == [eligible_key]
    assert payload["rejected_cohorts"][cohort_key(insufficient_row)][
        "reason"
    ] == "insufficient_independent_markets"
    assert payload["rejected_cohorts"][cohort_key(concentrated_row)][
        "reason"
    ] == "positive_pnl_too_concentrated"
    assert payload["source_discovery_config_hashes"] == {
        STRATEGY: BASE_HASH,
    }
    assert payload["target_base_config_hashes"] == {STRATEGY: BASE_HASH}
    assert payload["eligible_cohorts"][eligible_key][
        "source_discovery_config_hash"
    ] == BASE_HASH
    assert payload["content_hash"] == canonical_payload_hash(payload)
    assert evaluate_profitability_gate(
        _row(), payload, NOW + 1, _expected(snapshot)
    )["decision"] == "ALLOW"


def test_gate_rejects_cohort_not_bound_to_snapshot_base_config(tmp_path):
    snapshot = _snapshot(tmp_path)
    snapshot["strategies"][STRATEGY]["cohort"][
        "strategy_config_hash"
    ] = "different-base"
    snapshot["content_hash"] = canonical_payload_hash(snapshot)

    payload = build_profitability_gate(
        _report(),
        snapshot,
        {STRATEGY: BASE_HASH},
        NOW,
        COHORT_VERSION,
    )

    key = cohort_key(_row())
    assert payload["decision"] == "NO_TRADE"
    assert payload["eligible_cohorts"] == {}
    assert payload["rejected_cohorts"][key]["reason"] == (
        "calibration_cohort_mismatch"
    )


def test_gate_rejects_report_from_different_discovery_base_config(tmp_path):
    snapshot = _snapshot(tmp_path)
    report = _report()
    report["selected_config_hashes"][STRATEGY] = "different-discovery-base"

    with pytest.raises(ValueError, match="source discovery config"):
        build_profitability_gate(
            report,
            snapshot,
            {STRATEGY: BASE_HASH},
            NOW,
            COHORT_VERSION,
        )


def test_gate_rejects_empty_report_with_mismatched_discovery_base(tmp_path):
    snapshot = _snapshot(tmp_path)
    report = _report(cohorts={})
    report["selected_config_hashes"][STRATEGY] = "different-discovery-base"

    with pytest.raises(ValueError, match="source discovery config"):
        build_profitability_gate(
            report,
            snapshot,
            {STRATEGY: BASE_HASH},
            NOW,
            COHORT_VERSION,
        )


def test_gate_rejects_eligible_cohort_missing_selected_config_hash(tmp_path):
    snapshot = _snapshot(tmp_path)
    report = _report()
    report["selected_config_hashes"] = {}

    with pytest.raises(ValueError, match="selected_config_hashes.*strategy"):
        build_profitability_gate(
            report,
            snapshot,
            {STRATEGY: BASE_HASH},
            NOW,
            COHORT_VERSION,
        )


def test_gate_rejects_report_cohort_key_that_disagrees_with_dimensions(tmp_path):
    snapshot = _snapshot(tmp_path)

    payload = build_profitability_gate(
        _report(cohorts={"wrong-key": _cohort()}),
        snapshot,
        {STRATEGY: BASE_HASH},
        NOW,
        COHORT_VERSION,
    )

    assert payload["decision"] == "NO_TRADE"
    assert payload["rejected_cohorts"]["wrong-key"]["reason"] == (
        "cohort_key_mismatch"
    )


@pytest.mark.parametrize(
    ("change", "expected_reason"),
    [
        ("expired", "profitability_gate_unavailable"),
        ("config", "profitability_gate_unavailable"),
        ("model", "profitability_gate_unavailable"),
        ("snapshot", "profitability_gate_unavailable"),
        ("cohort_version", "profitability_gate_unavailable"),
        ("invalid_now", "profitability_gate_unavailable"),
        ("unknown_cohort", "profitability_cohort_not_eligible"),
    ],
)
def test_gate_fail_closed_for_expiry_mismatch_or_unknown_cohort(
    tmp_path, change, expected_reason
):
    snapshot, payload = _gate(tmp_path)
    row = _row()
    expected = _expected(snapshot)
    now = NOW + 1
    if change == "expired":
        now = NOW + 72 * 3600 + 1
    elif change == "config":
        expected["strategy_base_config_hash"] = "wrong"
    elif change == "model":
        expected["probability_model_id"] = "wrong"
    elif change == "snapshot":
        expected["calibration_snapshot_hash"] = "wrong"
    elif change == "cohort_version":
        expected["profitability_cohort_version"] = "wrong"
    elif change == "invalid_now":
        now = float("nan")
    else:
        row["asset"] = "ETH"

    result = evaluate_profitability_gate(row, payload, now, expected)

    assert result["decision"] == "BLOCK"
    assert result["reason"] == expected_reason


def test_missing_gate_blocks_without_attempting_cohort_match():
    result = evaluate_profitability_gate(
        _row(),
        None,
        NOW,
        {
            "strategy_base_config_hash": BASE_HASH,
            "probability_model_id": PROBABILITY_MODEL_IDS[STRATEGY],
            "calibration_snapshot_hash": "snapshot",
            "profitability_cohort_version": COHORT_VERSION,
        },
    )
    assert result["decision"] == "BLOCK"
    assert result["reason"] == "profitability_gate_unavailable"
    assert result["gate_content_hash"] is None
    assert result["calibration_snapshot_hash"] is None


def test_gate_publish_is_atomic_and_loader_checks_hash_and_expiry(tmp_path):
    _, payload = _gate(tmp_path)
    path = tmp_path / "data" / "profitability-gates.json"

    publish_profitability_gate(payload, path)

    assert not path.with_suffix(".json.tmp").exists()
    assert load_profitability_gate(path, NOW + 1) == (payload, None)
    corrupted = json.loads(path.read_text(encoding="utf-8"))
    corrupted["thresholds"]["minimum_independent_markets"] = 1
    path.write_text(json.dumps(corrupted), encoding="utf-8")
    assert load_profitability_gate(path, NOW + 1) == (
        None,
        "profitability_gate_hash_mismatch",
    )
    publish_profitability_gate(payload, path)
    assert load_profitability_gate(path, NOW + 72 * 3600 + 1) == (
        None,
        "profitability_gate_expired",
    )
    assert load_profitability_gate(path, NOW + 72 * 3600) == (
        None,
        "profitability_gate_expired",
    )


def test_loader_rejects_rehashed_gate_with_weakened_thresholds(tmp_path):
    _, payload = _gate(tmp_path)
    path = tmp_path / "gate.json"
    payload["thresholds"]["minimum_independent_markets"] = 1
    payload["content_hash"] = canonical_payload_hash(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_profitability_gate(path, NOW + 1) == (
        None,
        "profitability_gate_invalid",
    )


def test_gate_loader_rejects_non_finite_current_time(tmp_path):
    _, payload = _gate(tmp_path)
    path = tmp_path / "gate.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_profitability_gate(path, float("nan")) == (
        None,
        "profitability_gate_invalid",
    )


def test_gate_loader_rejects_rehashed_payload_missing_cohort_version(tmp_path):
    _, payload = _gate(tmp_path)
    path = tmp_path / "gate.json"
    del payload["profitability_cohort_version"]
    payload["content_hash"] = canonical_payload_hash(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_profitability_gate(path, NOW + 1) == (
        None,
        "profitability_gate_invalid",
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "delete_top_discovery",
        "change_top_discovery",
        "delete_entry_discovery",
        "change_entry_discovery",
    ),
)
def test_rehashed_gate_cannot_break_discovery_target_identity(
    tmp_path,
    mutation,
):
    snapshot, payload = _gate(tmp_path)
    key = cohort_key(_row())
    if mutation == "delete_top_discovery":
        del payload["source_discovery_config_hashes"][STRATEGY]
    elif mutation == "change_top_discovery":
        payload["source_discovery_config_hashes"][STRATEGY] = "different"
    elif mutation == "delete_entry_discovery":
        payload["eligible_cohorts"][key].pop(
            "source_discovery_config_hash",
            None,
        )
    else:
        payload["eligible_cohorts"][key][
            "source_discovery_config_hash"
        ] = "different"
    payload["content_hash"] = canonical_payload_hash(payload)
    path = tmp_path / "gate.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_profitability_gate(path, NOW + 1) == (
        None,
        "profitability_gate_invalid",
    )
    result = evaluate_profitability_gate(
        _row(),
        payload,
        NOW + 1,
        _expected(snapshot),
    )
    assert result["decision"] == "BLOCK"
    assert result["reason"] == "profitability_gate_unavailable"


def test_build_rejects_report_with_blocking_exclusions(tmp_path):
    snapshot = _snapshot(tmp_path)
    with pytest.raises(ValueError, match="blocking_exclusions"):
        build_profitability_gate(
            _report(blocking={"source_file_missing": 1}),
            snapshot,
            {STRATEGY: BASE_HASH},
            NOW,
            COHORT_VERSION,
        )


def test_cli_blocking_report_exits_two_without_replacing_gate_or_snapshot(
    tmp_path,
):
    report = tmp_path / "profitability-discovery.json"
    research = tmp_path / "probability-calibration-research.json"
    validation = tmp_path / "probability-calibration-validation.json"
    gate = tmp_path / "profitability-gates.json"
    report.write_text(
        json.dumps(_report(blocking={"source_file_missing": 1})),
        encoding="utf-8",
    )
    research.write_text(json.dumps(_calibration_map()), encoding="utf-8")
    validation.write_text("old-snapshot", encoding="utf-8")
    gate.write_text("old-gate", encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "poly_arb_bot.cli",
            "profitability-freeze",
            "--profitability-report",
            str(report),
            "--calibration-map",
            str(research),
            "--validation-calibration",
            str(validation),
            "--gate-file",
            str(gate),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "source_file_missing" in completed.stderr
    assert validation.read_text(encoding="utf-8") == "old-snapshot"
    assert gate.read_text(encoding="utf-8") == "old-gate"


def test_cli_missing_selected_config_hash_exits_two_without_replacing_outputs(
    tmp_path,
):
    report = tmp_path / "profitability-discovery.json"
    research = tmp_path / "probability-calibration-research.json"
    validation = tmp_path / "probability-calibration-validation.json"
    gate = tmp_path / "profitability-gates.json"
    report_payload = _report()
    report_payload["selected_config_hashes"] = {}
    report.write_text(json.dumps(report_payload), encoding="utf-8")
    calibration_payload = _calibration_map()
    for strategy, entry in calibration_payload["strategies"].items():
        entry["cohort"]["strategy_config_hash"] = (
            canonical_strategy_config_hash(strategy)
        )
    research.write_text(json.dumps(calibration_payload), encoding="utf-8")
    validation.write_text("old-snapshot", encoding="utf-8")
    gate.write_text("old-gate", encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PROFITABILITY_COHORT_VERSION"] = COHORT_VERSION

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "poly_arb_bot.cli",
            "profitability-freeze",
            "--profitability-report",
            str(report),
            "--calibration-map",
            str(research),
            "--validation-calibration",
            str(validation),
            "--gate-file",
            str(gate),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "selected_config_hashes" in completed.stderr
    assert STRATEGY in completed.stderr
    assert validation.read_text(encoding="utf-8") == "old-snapshot"
    assert gate.read_text(encoding="utf-8") == "old-gate"


def test_cli_valid_empty_report_publishes_no_trade_gate(tmp_path):
    report = tmp_path / "profitability-discovery.json"
    research = tmp_path / "probability-calibration-research.json"
    validation = tmp_path / "probability-calibration-validation.json"
    gate = tmp_path / "profitability-gates.json"
    report_payload = _report(cohorts={})
    report_payload["selected_config_hashes"] = {}
    report.write_text(json.dumps(report_payload), encoding="utf-8")
    research.write_text(json.dumps(_calibration_map()), encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PROFITABILITY_COHORT_VERSION"] = COHORT_VERSION

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "poly_arb_bot.cli",
            "profitability-freeze",
            "--profitability-report",
            str(report),
            "--calibration-map",
            str(research),
            "--validation-calibration",
            str(validation),
            "--gate-file",
            str(gate),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(gate.read_text(encoding="utf-8"))["decision"] == "NO_TRADE"
    assert json.loads(validation.read_text(encoding="utf-8"))["content_hash"]


def test_cli_freeze_targets_base_hash_when_previous_gate_is_enabled(tmp_path):
    report = tmp_path / "profitability-discovery.json"
    research = tmp_path / "probability-calibration-research.json"
    validation = tmp_path / "probability-calibration-validation.json"
    gate = tmp_path / "profitability-gates.json"
    previous_validation = tmp_path / "previous-validation.json"
    previous_gate = tmp_path / "previous-gate.json"
    base_hashes = {
        strategy: canonical_strategy_base_config_hash(strategy)
        for strategy in PROBABILITY_MODEL_IDS
    }
    report_payload = _report(cohorts={})
    report_payload["selected_config_hashes"] = {
        STRATEGY: base_hashes[STRATEGY],
    }
    calibration_payload = _calibration_map()
    for strategy, entry in calibration_payload["strategies"].items():
        entry["cohort"]["strategy_config_hash"] = base_hashes[strategy]
    report.write_text(json.dumps(report_payload), encoding="utf-8")
    research.write_text(json.dumps(calibration_payload), encoding="utf-8")
    previous_snapshot_payload = {"version": 2}
    previous_snapshot_payload["content_hash"] = canonical_payload_hash(
        previous_snapshot_payload,
    )
    previous_validation.write_text(
        json.dumps(previous_snapshot_payload), encoding="utf-8",
    )
    previous_gate_payload = {
        "version": 1,
        "calibration_snapshot_hash": previous_snapshot_payload["content_hash"],
    }
    previous_gate_payload["content_hash"] = canonical_payload_hash(
        previous_gate_payload,
    )
    previous_gate.write_text(
        json.dumps(previous_gate_payload), encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PROFITABILITY_GATE_ENABLE"] = "1"
    environment["PROFITABILITY_COHORT_VERSION"] = COHORT_VERSION
    environment["PROBABILITY_VALIDATION_CALIBRATION_PATH"] = str(
        previous_validation,
    )
    environment["PROFITABILITY_GATE_PATH"] = str(previous_gate)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "poly_arb_bot.cli",
            "profitability-freeze",
            "--profitability-report",
            str(report),
            "--calibration-map",
            str(research),
            "--validation-calibration",
            str(validation),
            "--gate-file",
            str(gate),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    published = json.loads(gate.read_text(encoding="utf-8"))
    assert published["target_base_config_hashes"] == base_hashes


def test_cli_empty_cohort_version_is_config_error_without_replacing_outputs(
    tmp_path,
):
    report = tmp_path / "profitability-discovery.json"
    research = tmp_path / "probability-calibration-research.json"
    validation = tmp_path / "probability-calibration-validation.json"
    gate = tmp_path / "profitability-gates.json"
    report.write_text(json.dumps(_report()), encoding="utf-8")
    research.write_text(json.dumps(_calibration_map()), encoding="utf-8")
    validation.write_text("old-snapshot", encoding="utf-8")
    gate.write_text("old-gate", encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PROFITABILITY_COHORT_VERSION"] = ""

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "poly_arb_bot.cli",
            "profitability-freeze",
            "--profitability-report",
            str(report),
            "--calibration-map",
            str(research),
            "--validation-calibration",
            str(validation),
            "--gate-file",
            str(gate),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 3
    assert "PROFITABILITY_FREEZE_ERROR" in completed.stderr
    assert validation.read_text(encoding="utf-8") == "old-snapshot"
    assert gate.read_text(encoding="utf-8") == "old-gate"


def test_cli_path_collision_is_config_error_and_preserves_source(tmp_path):
    report = tmp_path / "profitability-discovery.json"
    research = tmp_path / "probability-calibration-research.json"
    gate = tmp_path / "profitability-gates.json"
    report.write_text(json.dumps(_report()), encoding="utf-8")
    source_text = json.dumps(_calibration_map())
    research.write_text(source_text, encoding="utf-8")
    gate.write_text("old-gate", encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "poly_arb_bot.cli",
            "profitability-freeze",
            "--profitability-report",
            str(report),
            "--calibration-map",
            str(research),
            "--validation-calibration",
            str(research),
            "--gate-file",
            str(gate),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 3
    assert "paths must be distinct" in completed.stderr
    assert research.read_text(encoding="utf-8") == source_text
    assert gate.read_text(encoding="utf-8") == "old-gate"
