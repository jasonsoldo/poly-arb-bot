# Real-Market Arbitrage Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an always-on, real-CLOB research engine that discovers or falsifies repeatable complete-set arbitrage without loosening strict Shadow execution gates or claiming unsubmitted orders filled.

**Architecture:** C++ owns per-book enumeration, delayed second-leg observations, episode state, and compact canonical audit events. Python incrementally aggregates independent events into discovery and validation cohorts; scheduled directional/lottery observations remain calibration samples and never become orders or PnL. Web and acceptance expose the execution and research planes separately.

**Tech Stack:** C++17, Boost.Asio/Beast, Boost.PropertyTree, Python 3.10+, JSONL, vanilla JavaScript, pytest, Bash/systemd.

## Global Constraints

- Runtime remains `SHADOW / DRY RUN`; real submissions, orders, and fills remain zero.
- All hot-path book and delay math runs in C++; no blocking I/O runs in a WebSocket callback.
- Official dynamic fees are rounded independently per leg; missing fees fail closed.
- Identity includes market, condition, tokens, generation, session, size, delay, and leg order.
- Public-book observations are named `book_executable`, never `filled`.
- Fixed grid: sizes `1, 2, 5, 10`, delays `0, 50, 100, 250ms`, orders `UP_THEN_DOWN`, `DOWN_THEN_UP`.
- Strict directional, lottery, and paired-lock Shadow gates are not relaxed for sample volume.
- Probability research can ignore portfolio loss/position limits because it opens no position, but still requires valid model inputs.
- Every task follows RED, GREEN, refactor, then commit. Tests are never removed to clear release.

---

### Task 1: Pure C++ Delayed Book-Execution Model

**Files:**
- Create: `cpp/strategy/observed_arb.hpp`
- Create: `cpp/strategy/observed_arb_test.cpp`
- Modify: `scripts/build_cpp.sh`

**Interfaces:**
- Produces `observed_arb::AttemptIdentity`, `BookLeg`, `Attempt`, `Outcome`, and `LegOrder`.
- Produces `start_buy_both(identity, order, size, payout, buffer, first, now_us, due_us)`.
- Produces `observe_buy_both(attempt, second, first_exit, now_us)`.

- [ ] **Step 1: Write failing deterministic tests**

Cover exact cost, both leg orders, delayed price movement, fee rounding, partial depth, stale/snapshot/session/generation invalidation, conservative orphan exit, and full-loss fallback.

- [ ] **Step 2: Verify RED**

```bash
g++ -std=c++17 -O2 -Wall -Wextra -pedantic -Icpp cpp/strategy/observed_arb_test.cpp -o build/observed_arb_test && build/observed_arb_test
```

Expected: compile failure because the header does not exist.

- [ ] **Step 3: Implement the minimal pure model**

`BookLeg` carries requested/available quantity, VWAP, gross value, rounded fee, source age, readiness flags, generation, and session. `Outcome` is `BOOK_EXECUTABLE`, `ORPHANED`, or `INVALIDATED` with exact cost/PnL and stable reason. It performs no I/O and uses no configured fill probabilities.

- [ ] **Step 4: Add the binary to `scripts/build_cpp.sh` and verify GREEN**

Run `bash scripts/build_cpp.sh`; expect `observed arbitrage tests passed` plus all existing test outputs.

- [ ] **Step 5: Commit**

```bash
git add cpp/strategy/observed_arb.hpp cpp/strategy/observed_arb_test.cpp scripts/build_cpp.sh
git commit -m "Add delayed book-execution arbitrage model"
```

### Task 2: C++ Episode and Delay Integration

**Files:**
- Modify: `cpp/market_ws_engine/market_ws_engine.cpp`
- Modify: `tests/test_market_ws_engine_source.py`

**Interfaces:**
- Consumes Task 1.
- Emits `arb_episode_started`, `arb_episode_ended`, `arb_shadow_attempt`, `arb_shadow_leg_result`, `arb_shadow_book_executable`, `arb_shadow_orphaned`, `arb_shadow_invalidated`, and `arb_research_summary`.

- [ ] **Step 1: Write failing source-contract tests**

Assert the engine includes the new model, uses bounded pending state, enumerates the exact grids, emits canonical identity/cost fields, and clears attempts on disconnect/reload/expiry.

- [ ] **Step 2: Verify RED**

Run `python -m pytest -q tests/test_market_ws_engine_source.py`; expect missing observed-event contracts.

- [ ] **Step 3: Implement transition-only episodes**

On each accepted book mutation, evaluate both taker leg orders. Start only on false-to-true qualification, capture leg 1 immediately, and schedule leg 2 with a monotonic due-time queue serviced by the existing 250ms timer.

- [ ] **Step 4: Emit compact delayed outcomes**

Use the latest book at due time. Emit initial/delayed cost chains, size, delay, order, versions, ages, fees, generation/session, conservative orphan PnL, and all real counters as zero.

- [ ] **Step 5: Verify GREEN and commit**

Run targeted pytest and full C++ build, then commit the two files.

### Task 3: Remove Synthetic Filled-Filled Lifecycle

**Files:**
- Modify: `poly_arb_bot/shadow_execution.py`
- Modify: `tests/test_shadow_execution.py`

**Interfaces:**
- Consumes Task 2 canonical events.
- Keeps book-executable evidence separate from fills and completed PnL.

- [ ] **Step 1: Write failing regressions**

Assert a legacy `shadow_opportunity` without explicit evidence is ignored, environment defaults cannot manufacture completion, and canonical book-executable/orphan/invalidated events preserve producer IDs.

- [ ] **Step 2: Verify RED**

Run `python -m pytest -q tests/test_shadow_execution.py`; expect failure on current default `filled/filled` behavior.

- [ ] **Step 3: Implement fail-closed consumption**

Remove default filled arguments and environment fallbacks. Record canonical outcomes as research evidence without incrementing fill counters. Leave directional/lottery settlement unchanged.

- [ ] **Step 4: Verify GREEN and commit**

Run shadow execution and strategy lifecycle tests, then commit.

### Task 4: Statistical Discovery and Falsification

**Files:**
- Modify: `poly_arb_bot/arbitrage_research.py`
- Modify: `tests/test_arbitrage_research.py`

**Interfaces:**
- Consumes canonical events incrementally.
- Produces funnels, patterns, chronological cohorts, confidence intervals, and no-evidence diagnostics.
- Adds `_wilson_interval(successes, trials)`, `_mean_confidence_interval(values)`, and `_classification(pattern)`.

- [ ] **Step 1: Write failing aggregation tests**

Cover event dedupe, leg-order isolation, attempt/book-executable/orphan/invalidation counts, Wilson bounds, mean-PnL bounds, independent close windows, config resets, chronological validation, counterfactual exclusion, and no-evidence output.

- [ ] **Step 2: Verify RED**

Run `python -m pytest -q tests/test_arbitrage_research.py`; expect missing canonical fields and statistics.

- [ ] **Step 3: Implement versioned incremental state**

Migrate old state without counting synthetic completions as observed execution. Key patterns by strategy, asset, timeframe, size, delay, leg order, and config hash. Bound retained samples and preserve file offset/identity across rotation.

- [ ] **Step 4: Implement exact classifications**

Use the design thresholds. Maker cannot exceed `MAKER_RESEARCH_CANDIDATE`; split-sell remains capability-blocked without collateral/split evidence. Emit `NO REPEATABLE ARBITRAGE FOUND` when applicable.

- [ ] **Step 5: Verify GREEN and commit**

Run targeted tests and commit.

### Task 5: Full-Market Probability Observation Plane

**Files:**
- Modify: `cpp/market_ws_engine/market_ws_engine.cpp`
- Modify: `poly_arb_bot/strategy_shadow_lifecycle.py`
- Modify: `tests/test_cpp_canonical_strategy_source.py`
- Modify: `tests/test_strategy_shadow_lifecycle.py`
- Modify: `tests/test_strategy_calibration.py`

**Interfaces:**
- Emits `shadow_prediction_observation` at one fixed time bucket per strategy/market/window/config.
- Resolves through the existing lifecycle to `shadow_prediction_complete` or `shadow_prediction_orphaned`.

- [ ] **Step 1: Write failing scheduling tests**

Assert valid model evaluations are observed even when strict execution is portfolio-blocked; invalid references yield diagnostics but no probability sample; duplicate updates in one bucket do not duplicate; settlement joins by producer ID.

- [ ] **Step 2: Verify RED**

Run the three targeted files and confirm missing observation semantics.

- [ ] **Step 3: Emit scheduled observations in C++**

Use fixed seconds-to-close buckets per timeframe. Preserve probability inputs, model/config hashes, data quality, strict decision/reason, and `opens_position=false`. Do not bypass source, Price-to-Beat, settlement reference, or model validity.

- [ ] **Step 4: Resolve and calibrate**

Recognize the new event in the existing lifecycle. Group calibration by strategy, model, config, asset, timeframe, and close window; exclude orphaned outcomes.

- [ ] **Step 5: Verify GREEN and commit**

Run targeted tests and commit.

### Task 6: Dashboard and Acceptance Semantics

**Files:**
- Modify: `poly_arb_bot/web_monitor.py`
- Modify: `web/index.html`
- Modify: `poly_arb_bot/shadow_acceptance.py`
- Modify: `tests/test_web_monitor.py`
- Modify: `tests/test_web_dashboard_source.py`
- Modify: `tests/test_shadow_acceptance.py`

**Interfaces:**
- Displays separate `STRICT SHADOW EXECUTION`, `PROBABILITY CALIBRATION`, and `ARBITRAGE DISCOVERY` planes.

- [ ] **Step 1: Write failing API/render tests**

Assert book-executable is never labelled fill/order/PnL, probability observations are separate from accepts, funnels show confidence bounds and leg order, and the no-evidence banner is exact.

- [ ] **Step 2: Verify RED**

Run the three target files and confirm missing fields/labels.

- [ ] **Step 3: Implement incremental API and truthful panels**

Expose current-session versus history, exact costs, capacity, delay, order, attempts, executable rate, Wilson interval, orphan rate, PnL interval, close windows, cohort, and classification. Keep counterfactual rows visibly research-only.

- [ ] **Step 4: Extend acceptance**

Require canonical parsing, funnel identities, observed/counterfactual separation, no synthetic fills, zero real counters, and fresh health. Insufficient episodes is `INCOMPLETE`.

- [ ] **Step 5: Verify GREEN and commit**

Run targeted tests and commit.

### Task 7: Full Verification and VPS Evidence

**Files:**
- Modify only if a scoped defect is exposed.

**Interfaces:**
- Produces release evidence, not profitability claims.

- [ ] **Step 1: Run full local verification**

Run full pytest with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, full MSYS2 C++ build, JavaScript parse, Bash syntax, and systemd verification where available.

- [ ] **Step 2: Run official integration**

Verify current/next Gamma discovery, CLOB REST books and fees, and live Market WebSocket. Mocks do not satisfy this gate.

- [ ] **Step 3: Observe a VPS market rotation**

Build, restart services, observe at least one 5m rotation, run `shadow-acceptance`, and capture readiness identity, p95 hot-path latency, canonical attempt/outcome counts, and zero real counters.

- [ ] **Step 4: Report evidence honestly**

If thresholds are unmet, report `NO REPEATABLE ARBITRAGE FOUND` or `INSUFFICIENT EVIDENCE`. Do not weaken thresholds or call book evidence fills.

- [ ] **Step 5: Push only after every local gate passes**

Inspect `git status`, commit only scoped files, and push `main`.
