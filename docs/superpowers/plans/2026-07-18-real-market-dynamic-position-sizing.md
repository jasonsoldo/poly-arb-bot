# Real-Market Dynamic Position Sizing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed 10-share Shadow size with deterministic strategy-specific quantities derived from current Polymarket books, official fees, model quality, and explicit Shadow capital limits.

**Architecture:** Add a header-only C++ sizing solver that walks current in-memory book levels and returns a fully audited sizing result. Enrich market discovery with official CLOB V2 minimum order size, tick size, and fee details; wire the solver into directional, lottery, and paired-lock evaluations; make Python lifecycle verify canonical sizing arithmetic; and display only those canonical fields in Web.

**Tech Stack:** C++17, Boost.Beast CLOB engine, Python 3, pytest, vanilla JavaScript, Bash/systemd.

## Global Constraints

- All market prices, levels, fee parameters, and minimum sizes come from official current CLOB data.
- Dynamic sizing is C++ hot-path logic and performs no network, file, Python, or Web call.
- Missing required inputs fail closed; fixed-size fallback is forbidden.
- `SHADOW / DRY RUN` invariants remain `real_order_submissions = real_orders = real_fills = 0`.
- A book-executable observation is not a fill.
- No push occurs before all relevant tests and official integrations pass.

---

### Task 1: Preserve Official CLOB V2 Sizing Metadata

**Files:**
- Modify: `poly_arb_bot/clob_client.py`
- Modify: `poly_arb_bot/live_signals.py`
- Modify: `poly_arb_bot/cli.py`
- Modify: `poly_arb_bot/market_scanner.py`
- Test: `tests/test_price_sources.py`
- Test: `tests/test_clob_market_filter.py`
- Test: `tests/test_market_scanner.py`

**Interfaces:**
- Produces: `LiveMarketSpec.min_order_size`, `tick_size`, `fee_exponent`, and `fee_taker_only` in `live_markets.json`.
- Consumes: CLOB V2 `getClobMarketInfo(conditionID)` fields `mos`, `mts`, and `fd`.

- [ ] **Step 1: Write failing parser and publication tests**

Assert that CLOB market info `{mos: 5, mts: .01, fd: {r: .07, e: 2, to: true}}` survives filtering and atomic market publication as typed fields.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python -m pytest -q tests/test_price_sources.py tests/test_clob_market_filter.py tests/test_market_scanner.py`

Expected: failures for missing `LiveMarketSpec` fields or missing payload keys.

- [ ] **Step 3: Implement minimal typed propagation**

Add optional typed fields to `ClobBook` and `LiveMarketSpec`. Prefer `getClobMarketInfo` as the canonical market-parameter source; reject missing or invalid fee details and minimum order size. Serialize the fields through the existing atomic market writer.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the same pytest command and require all tests to pass.

### Task 2: Add the C++ Real-Book Sizing Solver

**Files:**
- Create: `cpp/strategy/dynamic_position_sizing.hpp`
- Create: `cpp/strategy/dynamic_position_sizing_test.cpp`
- Modify: `scripts/build_cpp.sh`

**Interfaces:**
- Consumes: ordered `std::map<double,double>` ask levels, official fee rate, minimum size, model probability and quality, strategy capital configuration.
- Produces: `sizing::Result size_probability_position(...)` and `sizing::Result size_paired_lock(...)`.

- [ ] **Step 1: Write failing deterministic solver tests**

Cover deep versus shallow books, probability shrinkage, monotonic fee/slippage/haircut behavior, capital binding, minimum-size rejection, exact per-level fee rounding, and paired equal-share sizing.

- [ ] **Step 2: Compile the test and verify RED**

Run:

```bash
g++ -std=c++17 -O3 -Wall -Wextra cpp/strategy/dynamic_position_sizing_test.cpp -o build/dynamic_position_sizing_test
```

Expected: compilation failure because the solver header and functions do not exist.

- [ ] **Step 3: Implement the minimal single-pass solver**

Define explicit `Config`, `ProbabilityInput`, `PairedInput`, and `Result` structs. Walk only real positive finite levels in ascending ask order. At each level boundary and final partial level, calculate official taker fee, execution buffer, conservative probability, fractional Kelly budget, all-in price, EV, and the current binding constraint. Return the largest valid rounded-down quantity.

- [ ] **Step 4: Compile and run the C++ test**

Run: `./build/dynamic_position_sizing_test`

Expected: `dynamic position sizing tests passed`.

- [ ] **Step 5: Add the test to `scripts/build_cpp.sh`**

Require the build script to compile and execute the sizing test before building the WebSocket engine.

### Task 3: Replace Fixed Sizes in the C++ Strategy Hot Path

**Files:**
- Modify: `cpp/market_ws_engine/market_ws_engine.cpp`
- Modify: `cpp/strategy/ev_strategy.hpp`
- Test: `tests/test_market_ws_engine_source.py`
- Test: `tests/test_cpp_canonical_strategy_source.py`
- Test: `cpp/strategy/ev_strategy_test.cpp`

**Interfaces:**
- Consumes: Task 1 market metadata and Task 2 sizing results.
- Produces: canonical `real_market_dynamic_v1` audit fields and dynamic `target_size` for all three strategies.

- [ ] **Step 1: Write failing source and strategy tests**

Require no probability audit to assign `target_size = size_`; require official minimum size in `Market`; require sizing configuration in the strategy hash; require canonical sizing fields in emitted JSON; and require paired-lock cost math to use its dynamic quantity.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python -m pytest -q tests/test_market_ws_engine_source.py tests/test_cpp_canonical_strategy_source.py`

Expected: failures showing the fixed `size_` path and absent audit fields.

- [ ] **Step 3: Wire probability sizing before evaluation**

For each outcome and strategy, call `size_probability_position`, build VWAP/fee/slippage for the returned quantity, then call the existing strategy evaluator. If sizing rejects, preserve other strategy blockers and prepend the stable sizing rejection. Store accepted dynamic quantity in the active probability-position tracker so profit exits sell exactly that quantity.

- [ ] **Step 4: Wire paired-lock sizing**

Call `size_paired_lock` on synchronized Up/Down books. Compute FOK stress and EEV at the returned equal quantity. Emit `shadow_opportunity` only for the dynamic quantity and preserve a rejection event when no valid quantity exists.

- [ ] **Step 5: Verify focused Python and C++ tests GREEN**

Run the focused pytest command, `bash scripts/build_cpp.sh`, and require no warning promoted to failure.

### Task 4: Enforce Canonical Dynamic Size in Shadow Lifecycle

**Files:**
- Modify: `poly_arb_bot/strategy_shadow_lifecycle.py`
- Test: `tests/test_strategy_shadow_lifecycle.py`

**Interfaces:**
- Consumes: canonical sizing fields from Task 3.
- Produces: ACTIVE positions carrying immutable dynamic sizing evidence.

- [ ] **Step 1: Write failing lifecycle tests**

Assert acceptance of a mathematically consistent dynamic event; rejection of missing sizing mode, size mismatch, cost mismatch, non-finite values, and below-minimum quantity; and exact use of stored quantity by profit exit and settlement completion.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m pytest -q tests/test_strategy_shadow_lifecycle.py`

- [ ] **Step 3: Implement strict lifecycle recomputation**

For new config version events, require `sizing_mode == real_market_dynamic_v1`, recompute entry cost from audited notional, fee, and buffer within tolerance, and persist all sizing evidence. Keep backward parsing only for historical completed rows; do not open new fixed-size positions.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the same pytest command and require all tests to pass.

### Task 5: Expose Dynamic Sizing in Web and Acceptance

**Files:**
- Modify: `poly_arb_bot/web_monitor.py`
- Modify: `poly_arb_bot/shadow_acceptance.py`
- Modify: `web/index.html`
- Test: `tests/test_web_monitor.py`
- Test: `tests/test_shadow_acceptance.py`

**Interfaces:**
- Produces: latest strategy sizing evidence, active dynamic exposure, and a machine-checkable dynamic-sizing integrity gate.

- [ ] **Step 1: Write failing Web and acceptance tests**

Require dynamic quantity, capital budget, conservative probability, live depth, all-in entry, expected profit, maximum loss, and binding constraint. Require `dynamic_position_sizing_integrity` to fail when current accepted probability or paired events lack canonical sizing evidence.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python -m pytest -q tests/test_web_monitor.py tests/test_shadow_acceptance.py`

- [ ] **Step 3: Implement canonical aggregation and rendering**

Map only canonical fields. Remove fixed `$10.00` or 10-share placeholders. Render `N/A` for missing values and preserve `BOOK EXECUTABLE NOT FILL` semantics.

- [ ] **Step 4: Run focused tests and JavaScript parse**

Run the focused pytest command and extract/parse the inline script with Node.

### Task 6: Full Verification, Official Integration, Commit, and Push

**Files:**
- Modify: `deploy/env.example`
- Modify: `deploy/VPS_DEPLOY.md`

- [ ] **Step 1: Declare explicit Shadow sizing configuration**

Document capital and per-strategy caps as simulation controls. State that they are not wallet balance and do not authorize live orders.

- [ ] **Step 2: Run the full local suite**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
bash scripts/build_cpp.sh
bash -n scripts/*.sh
node --check <extracted-dashboard-script>
```

- [ ] **Step 3: Run official current-market integration**

Run the official Gamma Series scanner, official CLOB market-info/book probes, and WebSocket Shadow engine against current markets. Confirm `min_order_size`, fee details, real book levels, and dynamic audit fields are non-synthetic. Zero ACCEPT is valid; absent or fabricated inputs are not.

- [ ] **Step 4: Run machine acceptance**

Run: `python -m poly_arb_bot.cli shadow-acceptance`

Require PASS, low-latency budget pass, dynamic sizing integrity pass, and all real-order counters equal zero.

- [ ] **Step 5: Commit and push only the verified files**

Stage only files listed in this plan, commit with `Add real-market dynamic position sizing`, fetch/rebase safely if needed, and push `main` only after all previous steps pass.
