# Real-Market Shadow Profitability Gating Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fail-closed profitability gate that separates calibration research from portfolio-limited Shadow PnL and validates only frozen, forward, real-order-book-costed cohorts.

**Architecture:** Correct the probability strategy cash ledger first, then reconcile canonical strategy and lifecycle events into one independent-market research report. Freeze an exact calibration snapshot and profitability gate, annotate every C++/Python strategy evaluation with the same gate result, and let the Python lifecycle enforce deployable position admission while continuing an isolated research lifecycle. Report and acceptance code consume only explicitly marked deployable completions.

**Tech Stack:** Python 3 standard library, pytest, C++17, Boost.PropertyTree, OpenSSL SHA-256, JSONL canonical audit, HTML/CSS/vanilla JavaScript.

## Global Constraints

- Production remains `SHADOW / DRY RUN`; `real_order_submissions`, `real_orders`, and `real_fills` must remain exactly `0`.
- Do not change `paired_lock` acceptance, cost, reference-price, audit, PnL, or Dashboard semantics.
- Probability strategy realized cash PnL uses ask-side VWAP notional plus dynamic taker fees; latency, settlement, uncertainty, and execution buffers remain risk/entry gates and are not realized cash costs.
- Profit exits qualify on risk-adjusted proceeds but record realized PnL from bid-side VWAP proceeds minus dynamic exit fees only.
- Use one deterministic first eligible probability-strategy entry per `market_id` for profitability samples.
- Candidate discovery requires at least 50 independent markets, positive mean net return, a positive one-sided 95% block-bootstrap lower bound, and no single market contributing more than 25% of positive PnL.
- The deterministic bootstrap groups markets by UTC four-hour close-time blocks, resamples 10,000 times, and uses the fifth percentile.
- Validation requires at least 48 hours, 300 new independent markets, 50 markets per enabled cohort, positive total and mean risk-normalized PnL, positive one-sided 95% lower bound, and maximum drawdown no greater than 10% of starting Shadow capital.
- The validation gate and calibration snapshot expire 72 hours after activation.
- Missing, corrupt, expired, mismatched, or insufficient gate data must reject deployable position admission while research observations continue.
- Gate and calibration files publish by temporary write, flush, `fsync`, and atomic `os.replace`.
- Strategy, probability model, gate, calibration snapshot, and profitability cohort identities must be bound into the emitted probability-strategy config hash.
- Use only Python standard-library statistics/random support; do not add NumPy, pandas, SciPy, or a model-training dependency.
- Preserve existing files and behavior outside the probability strategies and their monitoring surfaces.

---

## File and Interface Map

### New files

- `poly_arb_bot/profitability_analysis.py`
  - Reconcile entry audits with completed lifecycle events.
  - Recompute cash PnL.
  - Enforce one independent sample per market.
  - Build deterministic cohort and bootstrap metrics.
- `poly_arb_bot/profitability_gate.py`
  - Freeze calibration snapshots.
  - Build, hash, atomically publish, load, and match profitability gates.
- `poly_arb_bot/profitability_acceptance.py`
  - Classify a frozen forward window as `PASS`, `FAIL`, or `INCOMPLETE`.
- `tests/test_profitability_analysis.py`
- `tests/test_profitability_gate.py`
- `tests/test_profitability_acceptance.py`

### Modified files

- `cpp/strategy/dynamic_position_sizing.hpp`
  - Expose cash cost separately from risk-adjusted all-in cost.
- `cpp/strategy/dynamic_position_sizing_test.cpp`
  - Lock the cash/risk cost distinction.
- `cpp/market_ws_engine/market_ws_engine.cpp`
  - Emit cash cost, load frozen gate/snapshot bindings, evaluate cohort eligibility, and annotate audits.
- `poly_arb_bot/ev_shadow.py`
  - Mirror config hashing, frozen calibration selection, gate matching, and audit fields.
- `poly_arb_bot/probability_calibration_map.py`
  - Validate frozen snapshot content hash and expiry.
- `poly_arb_bot/strategy_shadow_lifecycle.py`
  - Maintain isolated research and deployable lifecycles.
- `poly_arb_bot/shadow_execution.py`
  - Publish rolling research calibration separately and preserve frozen validation files.
- `poly_arb_bot/shadow_report.py`
  - Split research and deployable performance.
- `poly_arb_bot/web_monitor.py`
- `web/index.html`
  - Display research and portfolio-limited results separately.
- `poly_arb_bot/shadow_acceptance.py`
  - Include profitability acceptance status without treating `INCOMPLETE` as PASS.
- `poly_arb_bot/cli.py`
  - Add `profitability-analysis`, `profitability-freeze`, and `profitability-acceptance`.
- `deploy/env.example`
- `deploy/VPS_DEPLOY.md`
  - Document the frozen validation workflow.

---

### Task 1: Correct Probability Shadow Cash Accounting

**Files:**
- Modify: `cpp/strategy/dynamic_position_sizing.hpp:11-151`
- Modify: `cpp/strategy/dynamic_position_sizing_test.cpp:49-65`
- Modify: `cpp/market_ws_engine/market_ws_engine.cpp:255-375, 1401-1625`
- Modify: `poly_arb_bot/ev_shadow.py:146-299`
- Modify: `poly_arb_bot/strategy_shadow_lifecycle.py:54-154, 514-772, 945-1069`
- Modify: `tests/test_strategy_shadow_lifecycle.py:34-49, 153-223, 253-373`
- Modify: `tests/test_market_ws_engine_source.py:296-333`
- Modify: `tests/test_canonical_config_hash.py`

**Interfaces:**
- Produces: C++ audit fields `dynamic_cash_cost`, `dynamic_risk_adjusted_cost`, `exit_cash_proceeds`, and `exit_risk_adjusted_proceeds`.
- Produces: lifecycle position fields `entry_cost` (cash), `risk_adjusted_entry_cost`, `deployable_pnl`, and `cash_ledger_version`.
- Produces: canonical probability strategy hash key `shadow_cash_ledger_version=2`.
- Consumes: existing `dynamic_buy_notional`, `dynamic_fee`, `dynamic_buffer`, `dynamic_all_in_cost`, `exit_vwap`, `exit_total_fee`, and `exit_execution_buffer`.

- [ ] **Step 1: Write failing C++ cash-cost tests**

Add these assertions to `test_probability_size_accounts_for_vwap_fee_and_slippage()`:

```cpp
assert(std::abs(
    result.dynamic_cash_cost -
    (result.dynamic_buy_notional + result.dynamic_fee)
) < 1e-12);
assert(std::abs(
    result.dynamic_all_in_cost -
    (result.dynamic_cash_cost + result.dynamic_buffer)
) < 1e-12);
assert(result.dynamic_maximum_loss == result.dynamic_cash_cost);
```

- [ ] **Step 2: Write failing lifecycle cash-PnL tests**

Change the probability fixture to include both costs and add a regression:

```python
row.update({
    "dynamic_buy_notional": 4.0,
    "dynamic_fee": 0.1,
    "dynamic_buffer": 0.2,
    "dynamic_cash_cost": 4.1,
    "dynamic_risk_adjusted_cost": 4.3,
    "dynamic_all_in_cost": 4.3,
    "dynamic_maximum_loss": 4.1,
})
assert lifecycle.consume(row, {"m1": market()}) is True
position = next(iter(lifecycle.data["positions"].values()))
assert position["entry_cost"] == 4.1
assert position["risk_adjusted_entry_cost"] == 4.3
```

For the exit fixture (`10 × 0.45`, fee `0.02`, buffer `0.01`), assert:

```python
assert complete["exit_cash_proceeds"] == 4.48
assert complete["exit_risk_adjusted_proceeds"] == 4.47
assert complete["realized_simulated_pnl"] == 0.38
```

- [ ] **Step 3: Run focused tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_strategy_shadow_lifecycle.py `
  tests/test_market_ws_engine_source.py `
  tests/test_canonical_config_hash.py -q
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_cpp.ps1
```

Expected: Python fails on missing cash fields/old PnL, and the C++ test fails because `Result` has no `dynamic_cash_cost`.

- [ ] **Step 4: Implement the minimal C++ cost split**

Add to `sizing::Result`:

```cpp
double dynamic_cash_cost = 0;
```

In `detail::copy_cost`:

```cpp
result.dynamic_cash_cost = cost.notional + cost.fee;
result.dynamic_all_in_cost = result.dynamic_cash_cost + cost.buffer;
result.dynamic_maximum_loss = result.dynamic_cash_cost;
```

Do not change paired-lock `dynamic_all_in_cost`; its execution buffer remains part of locked-cost acceptance.

Emit the probability audit chain:

```cpp
out << ",\"dynamic_buy_notional\":" << sizing_result.dynamic_buy_notional
    << ",\"dynamic_cash_cost\":" << sizing_result.dynamic_cash_cost
    << ",\"dynamic_risk_adjusted_cost\":" << sizing_result.dynamic_all_in_cost;
```

- [ ] **Step 5: Implement lifecycle cash PnL and conservative exit qualification**

For probability positions:

```python
entry_cost = float(row["dynamic_cash_cost"])
risk_adjusted_entry_cost = float(row["dynamic_risk_adjusted_cost"])
```

Pass `risk_adjusted_entry_cost` to `_portfolio_block_reason`, but persist `entry_cost` for realized PnL. For exits:

```python
exit_cash_proceeds = round(size * exit_vwap - exit_fee, 12)
exit_risk_adjusted_proceeds = round(exit_cash_proceeds - exit_buffer, 12)
risk_adjusted_profit = round(
    exit_risk_adjusted_proceeds - position["risk_adjusted_entry_cost"], 12)
pnl = round(exit_cash_proceeds - position["entry_cost"], 12)
```

Use `risk_adjusted_profit` and `exit_risk_adjusted_proceeds` for the exit threshold/EV comparison; record `pnl` as `realized_simulated_pnl`.

- [ ] **Step 6: Rotate the cash-ledger config identity**

Add `("shadow_cash_ledger_version", "SHADOW_CASH_LEDGER_VERSION", "2")` to Python `_CANONICAL_STRATEGY_CONFIG_ENV` and `{"shadow_cash_ledger_version", environment_value("SHADOW_CASH_LEDGER_VERSION", "2")}` to the C++ hash inputs. Bump audit `config_version` to `shadow-buy-rules-v10`, bump lifecycle version to `shadow-portfolio-v8`, and require v10 for new probability positions.

- [ ] **Step 7: Run focused tests and verify they pass**

Run the commands from Step 3.

Expected: all focused Python tests pass; all C++ strategy tests build and pass.

- [ ] **Step 8: Commit**

```powershell
git add cpp/strategy/dynamic_position_sizing.hpp `
  cpp/strategy/dynamic_position_sizing_test.cpp `
  cpp/market_ws_engine/market_ws_engine.cpp `
  poly_arb_bot/ev_shadow.py `
  poly_arb_bot/strategy_shadow_lifecycle.py `
  tests/test_strategy_shadow_lifecycle.py `
  tests/test_market_ws_engine_source.py `
  tests/test_canonical_config_hash.py
git commit -m "fix: separate shadow cash pnl from risk buffers"
```

---

### Task 2: Build the Independent-Market Profitability Analyzer

**Files:**
- Create: `poly_arb_bot/profitability_analysis.py`
- Create: `tests/test_profitability_analysis.py`
- Modify: `poly_arb_bot/cli.py:861-977`

**Interfaces:**
- Produces: `reconcile_probability_trades(strategy_audit_path: Path, execution_path: Path, config_hashes: dict[str, str] | None = None) -> dict`.
- Produces: `cohort_key(row: dict) -> str`.
- Produces: `block_bootstrap_lower_bound(rows: list[dict], seed_material: str, resamples: int = 10_000) -> float | None`.
- Produces: `aggregate_metrics(rows: list[dict], seed_material: str) -> dict`.
- Produces: `largest_positive_market_share(rows: list[dict]) -> float | None`.
- Produces: `build_profitability_report(strategy_audit_path: Path, execution_path: Path, config_hashes: dict[str, str] | None = None) -> dict`.
- Produces CLI: `python -m poly_arb_bot.cli profitability-analysis --strategy-audit-file logs/strategy-audit.jsonl --execution-log logs/shadow-execution.jsonl --output data/profitability-discovery.json`.
- Consumes: v9/v10 strategy audits and `shadow_complete` lifecycle history, including rotated JSONL files through `jsonl_history.history_paths`.

- [ ] **Step 1: Write failing reconciliation tests**

Create fixtures with:

- two `shadow_complete` rows for one market;
- their matching entry audit rows;
- one duplicate event;
- one missing fee;
- one mismatched outcome;
- one profit exit whose recorded PnL incorrectly includes a buffer.

Assert:

```python
result = reconcile_probability_trades(audit, execution)
assert len(result["trades"]) == 1
assert result["trades"][0]["market_id"] == "m1"
assert result["trades"][0]["net_pnl_usd"] == pytest.approx(0.38)
assert result["trades"][0]["net_return_per_dollar_risked"] == pytest.approx(0.38 / 4.1)
assert result["excluded"]["duplicate_market"] == 1
assert result["excluded"]["fee_schedule_unavailable"] == 1
assert result["excluded"]["outcome_mismatch"] == 1
```

- [ ] **Step 2: Write failing bucket and bootstrap tests**

Lock these bucket rules:

```python
assert probability_bucket(0.1007) == "0.1-0.2"
assert fill_price_bucket(0.027) == "0.0-0.1"
assert seconds_to_close_bucket(15) == "0-30"
assert seconds_to_close_bucket(75) == "60-90"
assert seconds_to_close_bucket(588) == "300-600"
```

Use exact close-time blocks `[0, 0, 14400, 14400]` and assert the same `seed_material` produces byte-identical report JSON and a different seed changes only bootstrap sampling fields.

- [ ] **Step 3: Run the analyzer tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_profitability_analysis.py -q
```

Expected: import failure because `profitability_analysis.py` does not exist.

- [ ] **Step 4: Implement audit reconciliation**

Implement:

```python
def reconcile_probability_trades(
    strategy_audit_path, execution_path, config_hashes=None
):
    # Index canonical shadow_eval rows by event_id.
    # Select probability shadow_complete rows from the requested/latest hash.
    # Join entry_event_id -> entry audit.
    # Recompute cash entry/exit/settlement values.
    # Sort by entry timestamp and retain the first valid row per market_id.
    # Return trades plus explicit exclusion counters and selected hashes.
```

Cash recomputation rules:

```python
entry_cash = dynamic_buy_notional + dynamic_fee
settlement_cash = payout
exit_cash = exit_fill_quantity * exit_vwap - exit_total_fee
net_pnl_usd = completion_cash - entry_cash
net_return_per_dollar_risked = net_pnl_usd / entry_cash
```

Validate `fee_rate`, `dynamic_fee`, depth, target size, strategy/model/config identities, settlement/exit evidence, and all real-order invariants.

- [ ] **Step 5: Implement fixed cohort keys and deterministic bootstrap**

Use deciles for calibration probability and expected fill. Use seconds-to-close bins:

```python
SECONDS_BINS = (0, 30, 60, 90, 180, 300, 600, float("inf"))
```

Group close timestamps by:

```python
block_id = int(close_ts // 14_400)
```

Derive the random seed from SHA-256 of `seed_material`; resample whole blocks 10,000 times and return the fifth percentile of mean `net_return_per_dollar_risked`.

- [ ] **Step 6: Implement report aggregation**

Return:

```python
{
    "version": 1,
    "generated_at": generated_at,
    "source": source_identity,
    "selected_config_hashes": selected_config_hashes,
    "independent_markets": len(trades),
    "excluded": dict(excluded),
    "blocking_exclusions": {
        reason: count for reason, count in excluded.items()
        if reason in BLOCKING_EXCLUSIONS and count > 0
    },
    "cash_ledger": cash_ledger_summary,
    "overall": aggregate_metrics(trades, seed_material),
    "cohorts": {
        cohort_key: {
            "dimensions": cohort_dimensions,
            "independent_markets": len(cohort_rows),
            "mean_net_return": statistics.fmean(
                item["net_return_per_dollar_risked"] for item in cohort_rows),
            "net_pnl_usd": sum(item["net_pnl_usd"] for item in cohort_rows),
            "lower_bound_95": block_bootstrap_lower_bound(
                cohort_rows, cohort_seed),
            "largest_positive_market_share": largest_positive_market_share(
                cohort_rows),
        }
    },
    "real_order_submissions": 0,
    "real_orders": 0,
    "real_fills": 0,
}
```

Define:

```python
BLOCKING_EXCLUSIONS = frozenset({
    "invalid_json",
    "missing_event_id",
    "missing_entry_event",
    "strategy_config_mismatch",
    "probability_model_mismatch",
    "outcome_mismatch",
    "fee_schedule_unavailable",
    "pnl_recalculation_mismatch",
    "real_order_invariant",
})
```

`duplicate_market` and unrelated-strategy rows remain reported exclusions but do not block a gate freeze.

- [ ] **Step 7: Add the read-only CLI command**

Add `profitability-analysis` to `cli.py`. It writes JSON to `--output` when supplied and prints JSON otherwise. It must never modify gate/config files.

- [ ] **Step 8: Run focused tests and a local diagnostic**

Run:

```powershell
python -m pytest tests/test_profitability_analysis.py -q
python -m poly_arb_bot.cli profitability-analysis `
  --strategy-audit-file logs/strategy-audit.jsonl `
  --execution-log logs/shadow-execution.jsonl `
  --output data/profitability-diagnostic.local.json
```

Expected: tests PASS; diagnostic exits `0` even when local history is stale, but reports its selected hashes, sample count, and exclusions explicitly.

- [ ] **Step 9: Commit**

```powershell
git add poly_arb_bot/profitability_analysis.py `
  poly_arb_bot/cli.py `
  tests/test_profitability_analysis.py
git commit -m "feat: add independent-market profitability analysis"
```

---

### Task 3: Freeze Calibration and Publish a Hashed Profitability Gate

**Files:**
- Create: `poly_arb_bot/profitability_gate.py`
- Create: `tests/test_profitability_gate.py`
- Modify: `poly_arb_bot/probability_calibration_map.py:162-275`
- Modify: `tests/test_probability_calibration_map.py:116-246`
- Modify: `poly_arb_bot/cli.py:861-977`

**Interfaces:**
- Produces: `canonical_payload_hash(payload: dict, excluded_fields: tuple[str, ...] = ("content_hash",)) -> str`.
- Produces: `freeze_calibration_snapshot(source: Path, destination: Path, now: float) -> dict`.
- Produces: `build_profitability_gate(report: dict, calibration_snapshot: dict, target_base_config_hashes: dict[str, str], now: float, cohort_version: str) -> dict`.
- Produces: `publish_profitability_gate(payload: dict, path: Path) -> None`.
- Produces: `load_profitability_gate(path: Path, now: float) -> tuple[dict | None, str | None]`.
- Produces: `evaluate_profitability_gate(row: dict, payload: dict | None, now: float, expected: dict) -> dict`.
- Produces CLI: `profitability-freeze`.
- Consumes: Task 2 report, probability calibration map version 2, current probability model IDs.

- [ ] **Step 1: Write failing atomic/hash/expiry tests**

Assert:

```python
snapshot = freeze_calibration_snapshot(source, destination, now=1000)
assert snapshot["validation_activated_at"] == 1000
assert snapshot["validation_expires_at"] == 1000 + 72 * 3600
assert snapshot["content_hash"] == canonical_payload_hash(snapshot)
assert not destination.with_suffix(".json.tmp").exists()
```

Corrupt one bucket after hashing and assert `load_frozen_calibration_snapshot` rejects it with `calibration_snapshot_hash_mismatch`.

- [ ] **Step 2: Write failing gate eligibility tests**

Create three report cohorts:

- eligible: 60 markets, positive mean/lower bound, largest share `0.10`;
- insufficient: 49 markets;
- concentrated: largest share `0.30`.

Assert only the first is `ALLOW`. Also assert missing, expired, config-mismatched, model-mismatched, snapshot-mismatched, and unknown-cohort inputs return one of:

```text
profitability_gate_unavailable
profitability_cohort_not_eligible
```

- [ ] **Step 3: Run tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_profitability_gate.py `
  tests/test_probability_calibration_map.py -q
```

Expected: missing module/functions.

- [ ] **Step 4: Implement canonical hashing and atomic publication**

Canonicalize with sorted keys and compact separators:

```python
encoded = json.dumps(
    filtered, sort_keys=True, separators=(",", ":"), ensure_ascii=True
).encode("utf-8")
return hashlib.sha256(encoded).hexdigest()
```

Atomic publication must call `flush()`, `os.fsync(handle.fileno())`, then `os.replace`.

- [ ] **Step 5: Implement the frozen calibration snapshot**

Copy the validated map payload, add activation/expiry, calculate `content_hash`, and never mutate the source rolling map. Add `load_frozen_calibration_snapshot(path, now, expected_content_hash=None)` to `probability_calibration_map.py`.

- [ ] **Step 6: Implement gate construction and matching**

Before constructing any gate, reject a report with a non-empty `blocking_exclusions` object and return CLI exit code `2`; do not replace an existing gate.

Gate construction copies only eligible cohorts and records every rejected cohort with its exact reason. It records both `source_discovery_config_hashes` from the report and explicit `target_base_config_hashes` supplied for the newly built runtime. Matching derives the Task 2 cohort key from live audit fields and validates:

```python
expected = {
    "strategy_base_config_hash": expected_base_hash,
    "probability_model_id": PROBABILITY_MODEL_IDS[row["strategy"]],
    "calibration_snapshot_hash": calibration_snapshot["content_hash"],
    "profitability_cohort_version": cohort_version,
}
```

Return:

```python
{
    "decision": "ALLOW" or "BLOCK",
    "reason": "profitability_cohort_eligible" or failure_reason,
    "cohort_key": cohort_key(row),
    "gate_content_hash": payload["content_hash"],
    "calibration_snapshot_hash": payload["calibration_snapshot_hash"],
}
```

- [ ] **Step 7: Add `profitability-freeze` CLI**

Add CLI arguments:

```text
--profitability-report  data/profitability-discovery.json
--gate-file             data/profitability-gates.json
--calibration-map       data/probability-calibration-research.json
--validation-calibration data/probability-calibration-validation.json
```

The command consumes the report, rolling calibration map, and target base hashes and atomically writes:

```text
data/probability-calibration-validation.json
data/profitability-gates.json
```

It exits `0` for an empty but valid `NO_TRADE` gate, `2` for insufficient/corrupt input, and `3` for I/O/config errors.

- [ ] **Step 8: Run focused tests**

Run:

```powershell
python -m pytest tests/test_profitability_gate.py `
  tests/test_probability_calibration_map.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

```powershell
git add poly_arb_bot/profitability_gate.py `
  poly_arb_bot/probability_calibration_map.py `
  poly_arb_bot/cli.py `
  tests/test_profitability_gate.py `
  tests/test_probability_calibration_map.py
git commit -m "feat: freeze calibration and profitability gates"
```

---

### Task 4: Add C++/Python Gate Parity Without Hiding Research Signals

**Files:**
- Modify: `cpp/market_ws_engine/market_ws_engine.cpp:160-375, 806-901, 1081-1207, 1401-1625, 3058-3176, 3389-3453`
- Modify: `poly_arb_bot/ev_shadow.py:146-299, 573-825`
- Modify: `tests/test_market_ws_engine_source.py:296-344`
- Modify: `tests/test_ev_shadow.py:645-820`
- Modify: `tests/test_cpp_strategy_parity.py`
- Modify: `tests/test_canonical_config_hash.py`

**Interfaces:**
- Produces audit fields `profitability_gate_decision`, `profitability_gate_reason`, `profitability_cohort_key`, `profitability_gate_hash`, `calibration_snapshot_hash`, and `deployable_candidate`.
- Produces: `base_decision` remains the existing strategy `decision`; gate annotation does not erase research ACCEPT signals.
- Produces: final emitted probability `config_hash` bound to cash-ledger version, profitability cohort version, gate hash, and frozen calibration snapshot hash.
- Produces: Python `canonical_strategy_base_config_hash(strategy: str | None = None) -> str` and final `canonical_strategy_config_hash(strategy: str | None = None) -> str`.
- Produces: C++ `strategy_base_config_hash(strategy_name)` and final `strategy_config_hash(strategy_name)`.
- Consumes: Task 3 gate/snapshot schemas and Task 2 bucket rules.

- [ ] **Step 1: Write failing Python parity tests**

For an otherwise accepted directional row, assert:

```python
assert row["decision"] == "ACCEPT"
assert row["profitability_gate_decision"] == "BLOCK"
assert row["profitability_gate_reason"] == "profitability_gate_unavailable"
assert row["deployable_candidate"] is False
```

With an eligible fixture, assert `ALLOW`, the exact cohort key, and `deployable_candidate is True`.

- [ ] **Step 2: Write failing C++ source/parity tests**

Require C++ source to:

- load both frozen JSON files;
- validate content hashes and expiry;
- implement the same decile/seconds bins;
- include gate state in `should_emit_strategy` fingerprint;
- emit all gate audit fields;
- leave `decision.decision` unchanged.

Extend the compiled parity fixture to compare Python/C++ gate decision and cohort key.

- [ ] **Step 3: Run tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_ev_shadow.py `
  tests/test_market_ws_engine_source.py `
  tests/test_cpp_strategy_parity.py `
  tests/test_canonical_config_hash.py -q
```

Expected: missing gate audit fields/parity.

- [ ] **Step 4: Implement shared binding semantics**

Use environment variables:

```text
PROFITABILITY_GATE_ENABLE=0
PROFITABILITY_GATE_PATH=data/profitability-gates.json
PROFITABILITY_COHORT_VERSION=1
PROBABILITY_VALIDATION_CALIBRATION_PATH=data/probability-calibration-validation.json
```

When gate enable is `1`, load only the frozen calibration snapshot for deployable evaluation. The strategy base decision still uses the frozen calibrated probability; missing/expired snapshot yields the existing `probability_calibration_unavailable` REJECT.

Calculate gate content hash excluding only `content_hash`. The gate stores the target base hashes, not the final gate-bound hashes. Calculate final strategy hash from the base config plus:

```text
shadow_cash_ledger_version
profitability_cohort_version
profitability_gate_content_hash
probability_validation_calibration_content_hash
```

This avoids a circular gate-hash dependency.

Validate the frozen calibration map's `cohort.strategy_config_hash` against the base hash. Validate the gate's `target_base_config_hashes` against the same base hash. Emit the final hash only after both file content hashes are known.

When `PROFITABILITY_GATE_ENABLE=0`, final hash equals base hash. This allows the gate-disabled research collection phase to produce calibration rows that can later be frozen against the same base identity.

- [ ] **Step 5: Implement C++ gate parsing and matching**

Add small C++ structs:

```cpp
struct ProfitabilityGateResult {
    bool allowed = false;
    std::string reason = "profitability_gate_unavailable";
    std::string cohort_key;
    std::string gate_hash;
    std::string calibration_snapshot_hash;
};
```

Match only the two probability strategies. Do not call `apply_sizing_rejection` with gate failures and do not modify `decision.decision`; attach the result to the emitted audit.

- [ ] **Step 6: Implement Python mirror**

In `evaluate_market_event`, load the same files, call `evaluate_profitability_gate`, and append identical audit fields. Bind the same content hashes into `canonical_strategy_config_hash`.

- [ ] **Step 7: Run parity tests and C++ build**

Run:

```powershell
python -m pytest tests/test_ev_shadow.py `
  tests/test_market_ws_engine_source.py `
  tests/test_cpp_strategy_parity.py `
  tests/test_canonical_config_hash.py -q
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_cpp.ps1
```

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add cpp/market_ws_engine/market_ws_engine.cpp `
  poly_arb_bot/ev_shadow.py `
  tests/test_market_ws_engine_source.py `
  tests/test_ev_shadow.py `
  tests/test_cpp_strategy_parity.py `
  tests/test_canonical_config_hash.py
git commit -m "feat: annotate probability evaluations with profitability gates"
```

---

### Task 5: Isolate Research and Deployable Shadow Lifecycles

**Files:**
- Modify: `poly_arb_bot/strategy_shadow_lifecycle.py:54-154, 339-772, 927-1069, 1156-1185`
- Modify: `poly_arb_bot/shadow_execution.py:143-179`
- Modify: `tests/test_strategy_shadow_lifecycle.py`
- Modify: `tests/test_shadow_execution.py`

**Interfaces:**
- Produces state collection `research_positions`.
- Produces persistent state collections `research_claimed_markets` and `deployable_claimed_markets`.
- Produces event `shadow_research_complete` with `deployable_pnl=false`.
- Produces probability `shadow_complete` with `deployable_pnl=true`.
- Produces lifecycle reject reasons from Task 4 gate annotations.
- Consumes: base strategy ACCEPT, gate audit fields, cash cost fields, profit-exit book evidence, settlement samples.

- [ ] **Step 1: Write failing research/deployable isolation tests**

Test three paths:

```python
# Base ACCEPT + gate BLOCK:
assert lifecycle.capture_research_candidate(row, markets) is True
assert lifecycle.consume(row, markets) is False
assert lifecycle.data["positions"] == {}
assert len(lifecycle.data["research_positions"]) == 1

# Base ACCEPT + gate ALLOW:
assert lifecycle.consume(allowed_row, markets) is True
assert next(iter(lifecycle.data["positions"].values()))["deployable_pnl"] is True

# Calibration mode never opens deployable positions:
assert lifecycle.consume(calibration_row, markets) is False
assert lifecycle.data["positions"] == {}
```

After an early exit, feed a second ACCEPT for the same market and assert neither research nor deployable positions reopen.

- [ ] **Step 2: Write failing research settlement/exit tests**

Assert the same bid-side exit event can complete both ledgers independently:

```python
events = [json.loads(line) for line in log.read_text().splitlines()]
assert {row["event_type"] for row in events} == {
    "shadow_complete", "shadow_research_complete",
}
assert next(row for row in events if row["event_type"] == "shadow_complete")[
    "deployable_pnl"] is True
assert next(row for row in events if row["event_type"] == "shadow_research_complete")[
    "deployable_pnl"] is False
```

- [ ] **Step 3: Run focused tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_strategy_shadow_lifecycle.py `
  tests/test_shadow_execution.py -q
```

Expected: missing research lifecycle and deployable markers.

- [ ] **Step 4: Implement a shared probability-position builder**

Extract:

```python
def _probability_position_from_row(
    self, row, market, *, risk_mode, deployable_pnl
):
    return {
        "event_id": row["event_id"],
        "strategy": row["strategy"],
        "market_id": row["market_id"],
        "outcome": row["outcome"],
        "entry_ts": float(row["ts"]),
        "close_ts": float(market["close_ts"]),
        "target_size": float(row["dynamic_target_size"]),
        "entry_cost": float(row["dynamic_cash_cost"]),
        "risk_adjusted_entry_cost": float(row["dynamic_risk_adjusted_cost"]),
        "dynamic_vwap": float(row["dynamic_vwap"]),
        "dynamic_fee": float(row["dynamic_fee"]),
        "dynamic_buffer": float(row["dynamic_buffer"]),
        "estimated_probability": float(row["estimated_probability"]),
        "calibration_input_probability": float(
            row["calibration_input_probability"]),
        "asset": row["asset"],
        "timeframe": row["timeframe"],
        "condition_id": row["condition_id"],
        "strategy_config_hash": row["config_hash"],
        "probability_model_id": row["probability_model_id"],
        "profitability_cohort_key": row.get("profitability_cohort_key"),
        "profitability_gate_hash": row.get("profitability_gate_hash"),
        "calibration_snapshot_hash": row.get("calibration_snapshot_hash"),
        "risk_mode": risk_mode,
        "portfolio_limits_enforced": deployable_pnl,
        "deployable_pnl": deployable_pnl,
        "cash_ledger_version": 2,
        "real_order_submissions": 0,
        "real_orders": 0,
        "real_fills": 0,
    }
```

The builder copies full entry/cost/calibration/gate identity and cash/risk cost fields. Use it for both ledgers so their economics cannot drift.

- [ ] **Step 5: Implement deterministic research capture**

`capture_research_candidate` accepts only base `decision=ACCEPT`, v10, valid dynamic cash evidence, and the first probability strategy candidate for a `market_id`. Persist a stable ID derived from strategy, market, config, model, and frozen calibration hash.

Call it before `consume` in `process_audit_once`.

Use `self.config_hash + "|" + market_id` in separate persistent claimed-market lists. Claim research on its first valid base ACCEPT and claim deployable on its first gate-allowed, portfolio-accepted entry. Never release claims after profit exit or settlement.

- [ ] **Step 6: Enforce gate admission in deployable consume**

Before portfolio checks:

```python
if self.calibration_mode:
    return self._reject(row, "calibration_research_only")
if row.get("profitability_gate_decision") != "ALLOW":
    return self._reject(
        row,
        row.get("profitability_gate_reason") or
        "profitability_gate_unavailable",
    )
```

An empty valid gate therefore remains `NO_TRADE` while research continues.

- [ ] **Step 7: Settle/exit both ledgers with distinct events**

Reuse the same cash PnL helpers. Research events must carry:

```python
{
    "event_type": "shadow_research_complete",
    "risk_mode": "CALIBRATION_RESEARCH",
    "portfolio_limits_enforced": False,
    "deployable_pnl": False,
    "real_order_submissions": 0,
    "real_orders": 0,
    "real_fills": 0,
}
```

Deployable events carry the corresponding true/enforced fields.

Both completion event types also record:

```python
net_pnl_usd = realized_simulated_pnl
net_return_per_dollar_risked = net_pnl_usd / entry_cost
```

- [ ] **Step 8: Stop overwriting frozen calibration**

When validation gate enable is `1`, `shadow_execution.run` publishes rolling research calibration to:

```text
data/probability-calibration-research.json
```

It must never replace `data/probability-calibration-validation.json` or `data/profitability-gates.json`.

- [ ] **Step 9: Run focused tests**

Run the command from Step 3.

Expected: PASS.

- [ ] **Step 10: Commit**

```powershell
git add poly_arb_bot/strategy_shadow_lifecycle.py `
  poly_arb_bot/shadow_execution.py `
  tests/test_strategy_shadow_lifecycle.py `
  tests/test_shadow_execution.py
git commit -m "feat: isolate research and deployable shadow lifecycles"
```

---

### Task 6: Split Research and Portfolio-Limited Reporting

**Files:**
- Modify: `poly_arb_bot/shadow_report.py:45-152, 154-358`
- Modify: `poly_arb_bot/web_monitor.py:121-253, 751-1230`
- Modify: `web/index.html:1-980`
- Modify: `tests/test_shadow_report.py`
- Modify: `tests/test_web_monitor.py`
- Modify: `tests/test_web_dashboard_source.py`

**Interfaces:**
- Produces report keys `research_performance`, `deployable_performance`, `profitability_validation`, and `historical_probability_completions_excluded`.
- Produces Dashboard panels `CALIBRATION RESEARCH` and `PORTFOLIO-LIMITED SHADOW`.
- Consumes: `shadow_research_complete`, explicit `deployable_pnl=true` probability completions, gate/snapshot files, lifecycle state, and `data/profitability-acceptance.json` when present.

- [ ] **Step 1: Write failing report separation tests**

Feed one old unmarked probability completion, one research completion, one deployable completion, and one paired completion. Assert:

```python
assert report["research_performance"]["completed"] == 1
assert report["deployable_performance"]["completed"] == 1
assert report["deployable_performance"]["simulated_pnl"] == 0.5
assert report["historical_probability_completions_excluded"] == 1
assert report["performance_by_strategy"]["paired_lock"]["completed"] == 1
```

The paired metrics must remain unchanged.

- [ ] **Step 2: Write failing Web status/source tests**

Assert status JSON contains:

```python
assert status["profitability"]["research"]["deployable_pnl"] is False
assert status["profitability"]["portfolio_limited"]["deployable_pnl"] is True
assert status["profitability"]["portfolio_limited"]["status"] in {
    "PASS", "FAIL", "INCOMPLETE",
}
```

Assert HTML includes exact labels:

```text
RESEARCH ONLY / NOT DEPLOYABLE PNL
PORTFOLIO-LIMITED SHADOW
SHADOW / NOT REAL MONEY
```

- [ ] **Step 3: Run focused tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_shadow_report.py `
  tests/test_web_monitor.py `
  tests/test_web_dashboard_source.py -q
```

Expected: missing split metrics/panels.

- [ ] **Step 4: Implement report filtering**

For probability strategies:

```python
deployable = [
    row for row in rows
    if row.get("event_type") == "shadow_complete"
    and row.get("deployable_pnl") is True
]
research = [
    row for row in rows
    if row.get("event_type") == "shadow_research_complete"
    and row.get("deployable_pnl") is False
]
```

Do not infer deployable status from old risk-mode strings or missing fields.

- [ ] **Step 5: Expose profitability state in Web status**

Read gate and snapshot with the Task 3 validated loaders. Expose identities, expiry, enabled cohorts, validation start, sample counts, PnL, drawdown, confidence lower bound, and real-order invariants.

Read `data/profitability-acceptance.json` when present. When it is absent, expose status `INCOMPLETE` with reason `profitability_acceptance_not_run`; never infer PASS from a positive PnL card.

- [ ] **Step 6: Replace the ambiguous main PnL presentation**

Keep historical/calibration data accessible, but make the primary strategy PnL card use only `deployable_performance`. Add a separate research card with the required warning label. Never merge curves.

- [ ] **Step 7: Run focused tests**

Run the command from Step 3.

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add poly_arb_bot/shadow_report.py `
  poly_arb_bot/web_monitor.py `
  web/index.html `
  tests/test_shadow_report.py `
  tests/test_web_monitor.py `
  tests/test_web_dashboard_source.py
git commit -m "feat: separate research and deployable shadow pnl"
```

---

### Task 7: Add Machine-Executable Profitability Acceptance

**Files:**
- Create: `poly_arb_bot/profitability_acceptance.py`
- Create: `tests/test_profitability_acceptance.py`
- Modify: `poly_arb_bot/shadow_acceptance.py`
- Modify: `poly_arb_bot/cli.py:861-977`
- Modify: `tests/test_shadow_acceptance.py`

**Interfaces:**
- Produces: `build_profitability_acceptance(execution_path: Path, gate_path: Path, state_path: Path, now: float | None = None) -> dict`.
- Produces: `run(execution_path: Path, gate_path: Path, state_path: Path, output_path: Path, now: float | None = None) -> int` with `0=PASS`, `1=FAIL`, `2=INCOMPLETE`, `3=infrastructure/configuration error`.
- Produces CLI: `profitability-acceptance`.
- Consumes: frozen gate, explicit deployable completions, lifecycle state, Task 2 bootstrap, current config/gate/snapshot hashes.

- [ ] **Step 1: Write failing PASS/FAIL/INCOMPLETE tests**

Build deterministic fixtures for:

- `INCOMPLETE`: 47 hours or 299 markets or 49 markets in one enabled cohort;
- `FAIL`: enough samples but non-positive PnL, drawdown over 10%, corrupt ledger, or nonzero real-order invariant;
- `INCOMPLETE`: positive PnL but lower bound `<= 0`;
- `PASS`: all gates satisfied.

Assert exit codes `2`, `1`, `2`, and `0`.

- [ ] **Step 2: Write failing identity tests**

Assert a gate/config/snapshot mismatch returns exit code `3`, and duplicate `market_id` rows do not increase the independent sample count.

- [ ] **Step 3: Run focused tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_profitability_acceptance.py `
  tests/test_shadow_acceptance.py -q
```

Expected: missing acceptance module/command.

- [ ] **Step 4: Implement exact acceptance classification**

Calculate:

```python
runtime_seconds = now - gate["validation_activated_at"]
independent_markets = len({row["market_id"] for row in deployable_rows})
total_pnl = sum(row["net_pnl_usd"] for row in deployable_rows)
mean_return = statistics.fmean(
    row["net_return_per_dollar_risked"] for row in deployable_rows)
maximum_drawdown_pct = maximum_drawdown_usd / starting_shadow_capital_usd
```

Apply safety/infrastructure failures before sample sufficiency, then economic failures, then confidence/sample incompleteness.

- [ ] **Step 5: Add CLI and shadow-acceptance integration**

`profitability-acceptance` prints the full JSON result and atomically writes `data/profitability-acceptance.json`. Existing `shadow-acceptance` keeps its safety checks individually passing when forward profitability is incomplete, but classifies the combined result as `INCOMPLETE`, never `PASS`.

Add CLI arguments:

```text
--gate-file       data/profitability-gates.json
--strategy-state  state/strategy-shadow.json
--acceptance-output data/profitability-acceptance.json
```

- [ ] **Step 6: Run focused tests**

Run the command from Step 3.

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add poly_arb_bot/profitability_acceptance.py `
  poly_arb_bot/shadow_acceptance.py `
  poly_arb_bot/cli.py `
  tests/test_profitability_acceptance.py `
  tests/test_shadow_acceptance.py
git commit -m "feat: add shadow profitability acceptance"
```

---

### Task 8: Deployment Workflow, Full Verification, and Real-Market Activation

**Files:**
- Modify: `deploy/env.example:1-120`
- Modify: `deploy/VPS_DEPLOY.md`
- Modify: `tests/test_deploy_files.py`
- Modify: `docs/superpowers/specs/2026-07-29-real-market-profitability-gating-design.md` only if implementation discovered a factual interface mismatch; do not relax approved gates.

**Interfaces:**
- Produces: exact operator workflow for diagnostic-only analysis, gate freeze, validation activation, monitoring, and acceptance.
- Consumes: all prior tasks.

- [ ] **Step 1: Write failing deployment-file tests**

Require `deploy/env.example` to contain:

```text
SHADOW_CALIBRATION_MODE=0
DIRECTIONAL_ENFORCE_TIME_WINDOW=1
PROFITABILITY_GATE_ENABLE=1
PROFITABILITY_GATE_PATH=data/profitability-gates.json
PROFITABILITY_COHORT_VERSION=1
PROBABILITY_VALIDATION_CALIBRATION_PATH=data/probability-calibration-validation.json
```

Require the runbook to include diagnostic, freeze, restart, immediate fail-closed check, and 48-hour/300-market acceptance commands.

- [ ] **Step 2: Run deployment tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_deploy_files.py -q
```

Expected: missing profitability settings/workflow.

- [ ] **Step 3: Update env example and runbook**

Document this VPS sequence from `/opt/poly-arb-bot`:

```bash
set -a
. ./.env
set +a
.venv/bin/python -m poly_arb_bot.cli profitability-analysis \
  --strategy-audit-file logs/strategy-audit.jsonl \
  --execution-log logs/shadow-execution.jsonl \
  --output data/profitability-discovery.json
.venv/bin/python -m poly_arb_bot.cli profitability-freeze \
  --profitability-report data/profitability-discovery.json \
  --calibration-map data/probability-calibration-research.json \
  --validation-calibration data/probability-calibration-validation.json \
  --gate-file data/profitability-gates.json
sudo systemctl restart poly-arb-bot poly-arb-web
.venv/bin/python -m poly_arb_bot.cli profitability-acceptance \
  --execution-log logs/shadow-execution.jsonl \
  --gate-file data/profitability-gates.json \
  --strategy-state state/strategy-shadow.json \
  --acceptance-output data/profitability-acceptance.json
```

The first acceptance call is expected to return `INCOMPLETE`; any missing/mismatched gate must return configuration error and zero deployable positions.

- [ ] **Step 4: Run all Python tests**

Run:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest -q
```

Expected: PASS with zero failures.

- [ ] **Step 5: Build and run all C++ tests**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_cpp.ps1
```

Expected: all C++ strategy tests pass and `market_ws_engine` builds.

- [ ] **Step 6: Run local machine acceptance checks**

Run:

```powershell
python -m poly_arb_bot.cli shadow-acceptance `
  --log-file logs/shadow-audit.jsonl `
  --state-file state/orders.json
python -m poly_arb_bot.cli profitability-acceptance `
  --execution-log logs/shadow-execution.jsonl `
  --gate-file data/profitability-gates.json `
  --strategy-state state/strategy-shadow.json `
  --acceptance-output data/profitability-acceptance.json
```

Expected: safety invariants remain zero. Profitability may be `INCOMPLETE` locally because local logs are stale; it must not falsely PASS.

- [ ] **Step 7: Generate the real VPS discovery report without changing admission**

Deploy the tested code on the VPS with `PROFITABILITY_GATE_ENABLE=0` and `SHADOW_CALIBRATION_MODE=1`. In this version calibration mode creates research rows only and cannot create deployable positions. Wait until `data/probability-calibration-research.json` identifies the current target base hashes and contains the configured minimum samples for the occupied buckets.

Then run `profitability-analysis` while gate admission remains disabled. Review:

- selected current config hashes;
- independent market count;
- excluded rows and reasons;
- cash-ledger correction versus displayed legacy PnL;
- eligible cohorts and concentration.

If no cohort passes, publish an empty valid gate and leave the system `NO_TRADE`. Do not reduce thresholds.

- [ ] **Step 8: Freeze and activate a new forward cohort**

Only after reviewing the diagnostic:

1. freeze the exact calibration snapshot/gate;
2. set `SHADOW_CALIBRATION_MODE=0`;
3. set `DIRECTIONAL_ENFORCE_TIME_WINDOW=1`;
4. set `PROFITABILITY_GATE_ENABLE=1`;
5. rotate `PROFITABILITY_COHORT_VERSION`;
6. rebuild/restart bot and Web;
7. verify emitted hashes equal Python hashes;
8. verify deployable completed count starts at zero;
9. verify research observations continue;
10. verify all real-order counters remain zero.

- [ ] **Step 9: Run forward acceptance after the approved window**

At 48 hours and until gate expiry:

```bash
set -a
. ./.env
set +a
.venv/bin/python -m poly_arb_bot.cli profitability-acceptance \
  --execution-log logs/shadow-execution.jsonl \
  --gate-file data/profitability-gates.json \
  --strategy-state state/strategy-shadow.json \
  --acceptance-output data/profitability-acceptance.json
```

Report exactly `PASS`, `FAIL`, or `INCOMPLETE`. A positive chart with a non-positive confidence lower bound remains `INCOMPLETE`; a `PASS` is Shadow evidence only and does not authorize live trading.

- [ ] **Step 10: Commit deployment documentation**

```powershell
git add deploy/env.example `
  deploy/VPS_DEPLOY.md `
  tests/test_deploy_files.py
git commit -m "docs: add profitability validation runbook"
```

---

## Final Review Checklist

- [ ] Every probability completed row used for deployable PnL has `deployable_pnl=true`.
- [ ] Every research completion has `deployable_pnl=false`.
- [ ] Old unmarked probability completions are excluded from current deployable PnL.
- [ ] Entry and exit buffers affect qualification/risk but not realized cash PnL.
- [ ] Dynamic fee and VWAP fields reconcile to cash cost.
- [ ] One market contributes at most one profitability sample.
- [ ] Discovery and forward validation windows share no `market_id`.
- [ ] Frozen calibration/gate hashes match in Python, C++, lifecycle, report, and Dashboard.
- [ ] Missing/expired/mismatched gate produces zero deployable positions.
- [ ] Empty eligible cohort set produces `NO_TRADE`.
- [ ] `paired_lock` tests and metrics remain unchanged.
- [ ] Full Python and C++ suites pass.
- [ ] `real_order_submissions = real_orders = real_fills = 0`.
- [ ] No response claims guaranteed, live, or long-run profitability.
