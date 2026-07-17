# Repeatable Arbitrage Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stabilize the official Polymarket Market Channel connection and expose audit-derived, independent repeatable-arbitrage evidence without enabling real trading.

**Architecture:** The C++ market engine remains the hot path and emits compact complete-set episode and counterfactual observations. Python incrementally aggregates canonical JSONL into execution funnels and repeatability groups. The Web client renders those fields without inventing values.

**Tech Stack:** C++17, Boost.Asio/Beast, Python 3, JSONL, vanilla JavaScript, pytest.

## Global Constraints

- Production mode remains SHADOW / DRY RUN.
- Real submissions, orders, and fills remain zero.
- No fee, depth, freshness, sync, or execution-risk threshold is relaxed.
- Complete-set research remains separate from directional and lottery strategy metrics.
- Only current official Polymarket REST and WebSocket behavior is accepted.

---

### Task 1: Market Channel transport

**Files:**
- Modify: `cpp/market_ws_engine/market_ws_engine.cpp`
- Test: `tests/test_market_ws_engine_source.py`

**Interfaces:**
- Consumes: active token IDs from `books_`.
- Produces: one initial `subscription(assets, "")`, dynamic updates after handshake, and 10-second `PING` heartbeats.

- [ ] Add a failing source regression test that rejects 20-token initial chunking and 5-second Market Channel heartbeats.
- [ ] Run `python -m pytest tests/test_market_ws_engine_source.py -q` and confirm failure.
- [ ] Send one full initial subscription and change the heartbeat interval to 10 seconds.
- [ ] Re-run the focused test and compile the C++ engine.

### Task 2: Counterfactual complete-set observations

**Files:**
- Modify: `cpp/strategy/complete_set_arb.hpp`
- Modify: `cpp/market_ws_engine/market_ws_engine.cpp`
- Test: `cpp/strategy/complete_set_arb_test.cpp`
- Test: `tests/test_market_ws_engine_source.py`

**Interfaces:**
- Consumes: current local Up/Down books, official fees, configured buffer, and configured execution decay.
- Produces: `shadow_arb_counterfactual` JSONL events for sizes `1,2,5,10` and delays `0,50,100,250ms`.

- [ ] Add failing math tests for post-cost profit and latency-stressed EEV.
- [ ] Run the C++ test and confirm failure.
- [ ] Implement the minimal pure counterfactual calculation and compact C++ audit emission.
- [ ] Verify counterfactual events cannot mutate inventory or create execution lifecycle events.
- [ ] Run the C++ tests and focused source tests.

### Task 3: Independent episode and funnel aggregation

**Files:**
- Create: `poly_arb_bot/arbitrage_research.py`
- Modify: `poly_arb_bot/web_monitor.py`
- Test: `tests/test_arbitrage_research.py`
- Test: `tests/test_web_monitor.py`

**Interfaces:**
- Consumes: canonical complete-set evaluation, opportunity, lifecycle, and counterfactual events.
- Produces: `arbitrage_research.funnels`, `arbitrage_research.repeatable_patterns`, and explicit invariant totals.

- [ ] Add failing tests where repeated heartbeats in one session count as one episode and a new session counts as a new episode.
- [ ] Add failing tests for funnel identities and three-close-window candidate classification.
- [ ] Implement incremental deduplication and grouping using producer event IDs.
- [ ] Keep unavailable funnel stages as `None`, never zero-filled success.
- [ ] Run focused report and Web API tests.

### Task 4: Operator dashboard

**Files:**
- Modify: `web/index.html`
- Test: `tests/test_web_dashboard_source.py`

**Interfaces:**
- Consumes: `arbitrage_research` from `/api/status`.
- Produces: real execution funnel, repeatable pattern table, and explicit research-only labels.

- [ ] Add failing source checks for the new API fields and labels.
- [ ] Render independent episodes, close windows, size, delay survival, completed count, and post-cost PnL.
- [ ] Label near misses and counterfactuals as research observations, not orders or profit.
- [ ] Parse the JavaScript with Node.

### Task 5: Release verification

**Files:**
- Modify only files required by failures discovered in this task.

- [ ] Run the full Python test suite with plugin autoload disabled.
- [ ] Run C++ build/tests.
- [ ] Run JavaScript parse and Bash syntax checks.
- [ ] Run official Gamma discovery within the global deadline.
- [ ] Run an official Market Channel soak and verify stable session/readiness plus nonzero book updates.
- [ ] Run `shadow-acceptance` against fresh integration output and confirm every real-order field remains zero.
- [ ] Review `git diff`, commit only intended tracked files, and push only after every required gate passes.
