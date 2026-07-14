# Latest Asset Shadow PnL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show each supported asset's most recent completed Shadow simulated PnL beside the asset/timeframe market matrix.

**Architecture:** Preserve asset metadata while building the canonical completed-trade ledger, then derive a latest-completion record per supported asset in `web_monitor`. The dashboard renders that API object as one additional matrix column and never derives PnL from evaluations or open positions.

**Tech Stack:** Python 3, stdlib JSON, pytest, HTML/CSS, vanilla JavaScript.

## Global Constraints

- Source only canonical `shadow_complete` events.
- Missing completed data is `null` in the API and `N/A` in the UI.
- This is simulated PnL and must not alter real-order or acceptance counts.
- Keep the existing seven assets and four timeframe columns.

---

### Task 1: Canonical Latest PnL Aggregation

**Files:**
- Modify: `poly_arb_bot/shadow_report.py`
- Modify: `poly_arb_bot/web_monitor.py`
- Test: `tests/test_web_monitor.py`

**Interfaces:**
- Consumes: `report["trade_ledger"]`, newest-first completed Shadow records.
- Produces: `status["asset_latest_pnl"]`, keyed by supported asset with `pnl`, `strategy`, `ts`, `market_id`, and `timeframe`.

- [ ] **Step 1: Write the failing API test**

Create completed BTC records at timestamps 101 and 102, an ETH record at 103, and assert the API selects BTC timestamp 102, ETH timestamp 103, and returns `None` for SOL.

- [ ] **Step 2: Verify the test fails**

Run: `python -m pytest -q tests/test_web_monitor.py -k latest_completed_pnl`

Expected: failure because `asset_latest_pnl` does not exist.

- [ ] **Step 3: Implement minimal aggregation**

Preserve `asset` and `timeframe` in the canonical ledger. Iterate the newest-first ledger once and retain the first valid completed row for each supported asset.

- [ ] **Step 4: Verify the API test passes**

Run: `python -m pytest -q tests/test_web_monitor.py -k latest_completed_pnl`

Expected: one passing test.

### Task 2: Matrix PnL Column

**Files:**
- Modify: `web/index.html`
- Test: `tests/test_web_dashboard_source.py`

**Interfaces:**
- Consumes: `d.asset_latest_pnl[asset]` from `/api/status`.
- Produces: `LATEST SIM PNL` matrix cells with signed value, state color, and metadata tooltip.

- [ ] **Step 1: Write the failing dashboard source test**

Assert the dashboard contains `LATEST SIM PNL`, reads `asset_latest_pnl`, renders `N/A`, and applies `good`/`warn` based on the PnL sign.

- [ ] **Step 2: Verify the test fails**

Run: `python -m pytest -q tests/test_web_dashboard_source.py -k latest_asset_pnl`

Expected: failure because the column is absent.

- [ ] **Step 3: Implement minimal rendering**

Append one table header and one cell per asset. Format PnL with `money`, use existing colors, and escape tooltip metadata.

- [ ] **Step 4: Verify the dashboard test passes**

Run: `python -m pytest -q tests/test_web_dashboard_source.py -k latest_asset_pnl`

Expected: one passing test.

### Task 3: Regression Verification And Publication

**Files:**
- Verify all modified files.

- [ ] **Step 1: Run the full Python suite**

Run: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 2: Parse inline JavaScript**

Extract the inline script from `web/index.html` and run `node --check`.

Expected: exit code 0.

- [ ] **Step 3: Check patch hygiene**

Run: `git diff --check`

Expected: exit code 0.

- [ ] **Step 4: Commit and push only feature files**

Stage the report, monitor, dashboard, tests, and this plan. Do not stage existing untracked `AGENTS.md` or `data/` diagnostics.
