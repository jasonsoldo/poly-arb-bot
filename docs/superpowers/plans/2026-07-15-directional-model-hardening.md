# Directional Model Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make probability readiness time-based and prevent duplicate directional exposure in one settlement window.

**Architecture:** The C++ reference source owns fixed-time sampling and publishes coverage metadata. Python consumes that metadata as a fail-closed probability prerequisite, while the existing portfolio lifecycle enforces one cross-strategy position per close window.

**Tech Stack:** C++17, Boost.Beast, Python 3, pytest, systemd environment configuration.

## Global Constraints

- Keep all real order counters at zero.
- Do not change probability coefficients or EV thresholds.
- Preserve the three independent strategy names and paired-lock behavior.

---

### Task 1: Add failing model-coverage tests

**Files:**
- Modify: `tests/test_reference_price_engine_source.py`
- Modify: `tests/test_ev_shadow.py`
- Modify: `tests/test_strategy_shadow_lifecycle.py`

- [ ] Assert the C++ source buckets samples by one-second timestamp and publishes model span.
- [ ] Assert Python rejects probability calculation when model span is below the configured minimum.
- [ ] Assert the default portfolio limit blocks a second non-paired position with the same `close_ts`.
- [ ] Run targeted tests and confirm they fail for the missing behavior.

### Task 2: Implement time-based model readiness

**Files:**
- Modify: `cpp/reference_price_engine/reference_price_engine.cpp`
- Modify: `poly_arb_bot/ev_shadow.py`
- Modify: `deploy/env.example`

- [ ] Bucket C++ model samples to one sample per second.
- [ ] Publish `model_sample_span_seconds` with effective sample count.
- [ ] Add `MODEL_MIN_SAMPLE_SPAN_SECONDS` to the strategy config hash and environment example.
- [ ] Fail closed with `model_sample_span_insufficient` and audit the actual and required spans.
- [ ] Run targeted tests and confirm they pass.

### Task 3: Enforce one directional risk per close window

**Files:**
- Modify: `poly_arb_bot/strategy_shadow_lifecycle.py`
- Modify: `deploy/env.example`

- [ ] Change the default `COMBINED_MAX_PER_CLOSE_WINDOW` from 2 to 1 in code and deployment defaults.
- [ ] Run lifecycle tests and confirm the second correlated position is rejected.

### Task 4: Verify and publish

**Files:**
- Verify all modified files.

- [ ] Run the full Python test suite.
- [ ] Build all C++ engines.
- [ ] Parse inline JavaScript and validate Bash syntax.
- [ ] Run `git diff --check` and review the staged scope.
- [ ] Commit and push only after every check passes.
