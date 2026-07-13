# Shadow Analytics Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace static or conflated monitor values with auditable shadow analytics, independent live-source freshness, and a dense real-data dashboard.

**Architecture:** Extend the existing streaming report module into the canonical analytics aggregator, then merge its output with current CLOB, market, reference-price, and real-order state in `build_status`. Render only API-provided values in the single-file dashboard; missing evidence is `N/A` or an empty state.

**Tech Stack:** Python 3 standard library, JSON/JSONL, existing C++ status producers, vanilla HTML/CSS/JavaScript, pytest.

## Global Constraints

- Every displayed value comes from current files or canonical audit records.
- Missing or insufficient data displays `N/A`; no synthetic chart points, latency bars, PNL, fills, score, or win statistics.
- Shadow results never alter real orders, real fills, realized real PNL, or real equity.
- Binance and Chainlink freshness is evaluated independently per asset.
- Keep the C++ hot path unchanged unless a missing source timestamp cannot be supplied otherwise.
- Real order submissions remain zero.

---

### Task 1: Canonical Shadow Analytics

**Files:**
- Modify: `poly_arb_bot/shadow_report.py`
- Modify: `tests/test_shadow_report.py`

**Interfaces:**
- Consumes: audit JSONL rows with `shadow_eval`, `shadow_opportunity`, and execution state events.
- Produces: `build_report(path: Path) -> dict` with `performance`, `equity_curve`, `trade_ledger`, `pnl_meter`, `strategy_score`, `latency_rankings`, `pipeline_steps`, and existing counters.

- [ ] Write failing fixture tests for unique COMPLETE execution PNL, duplicate suppression, win rate, 24-bucket Sharpe threshold, empty equity, rejection counts, and observed latency percentiles.
- [ ] Run `python -m pytest -q tests/test_shadow_report.py` and confirm the new assertions fail.
- [ ] Implement one-pass JSONL aggregation with bounded recent records and no generated samples.
- [ ] Run `python -m pytest -q tests/test_shadow_report.py` and confirm all report tests pass.

### Task 2: Independent Source Freshness and Status Merge

**Files:**
- Modify: `poly_arb_bot/web_monitor.py`
- Modify: `tests/test_web_monitor.py`

**Interfaces:**
- Consumes: canonical report, `venue-status.json`, `shadow-health.json`, `live_markets.json`, and real order state.
- Produces: `/api/status` fields required by every dashboard panel.

- [ ] Add failing tests proving fresh Binance survives stale Chainlink, fresh Chainlink survives stale Binance, unsupported assets remain explicit, and analytics fields pass through unchanged.
- [ ] Run `python -m pytest -q tests/test_web_monitor.py` and confirm failures expose the current combined-staleness bug.
- [ ] Compute `binance_stale` and `chainlink_stale` independently from file age plus source age, null only the affected source, and derive divergence only when both are fresh.
- [ ] Merge canonical analytics and measured latency without changing real-order counts.
- [ ] Run `python -m pytest -q tests/test_web_monitor.py` and confirm all status tests pass.

### Task 3: Real-Data Dashboard Modules

**Files:**
- Modify: `web/index.html`
- Modify: `tests/test_web_dashboard_source.py`

**Interfaces:**
- Consumes: `/api/status` analytics and health fields from Task 2.
- Produces: responsive cyberpunk eye-care dashboard with real ledger, equity, win rate, Sharpe, score, PNL meter, pipeline, rejection distribution, and latency ranking.

- [ ] Add source tests requiring the new panel IDs and forbidding static equity lines, hard-coded active pipeline states, and numeric fallback values for missing metrics.
- [ ] Run `python -m pytest -q tests/test_web_dashboard_source.py` and confirm the new source tests fail.
- [ ] Replace the static equity area with an SVG/polyline rendered only from `equity_curve`; show a zero/empty state otherwise.
- [ ] Render simulated and real PNL separately, win rate and Sharpe with sample counts, completed execution ledger, auditable strategy-score components, real pipeline timestamps, rejection counts, and latency p50/p95/p99/sample counts.
- [ ] Render Binance and Chainlink in separate columns with source age/status; never collapse one stale source into the other.
- [ ] Run `python -m pytest -q tests/test_web_dashboard_source.py tests/test_web_monitor.py tests/test_shadow_report.py` and confirm all dashboard contract tests pass.

### Task 4: End-to-End Verification

**Files:**
- Modify only if a failing test identifies a defect in Tasks 1-3.

**Interfaces:**
- Consumes: complete repository and representative temporary audit/status fixtures.
- Produces: release evidence without live order submission.

- [ ] Run `python -m pytest -q` with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`; expect the complete suite to pass.
- [ ] Extract the dashboard script and run `node --check`; expect exit code 0.
- [ ] Start the monitor against temporary fixture files, request `/api/status`, and verify PNL, equity, source freshness, and latency fields match the fixture exactly.
- [ ] Inspect `git diff --check` and `git status --short`; confirm only intended source, tests, spec, and plan are included.
- [ ] Commit the verified implementation and push `main` only after all local checks pass.
