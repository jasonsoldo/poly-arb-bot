# C++ Low-Latency Strategy Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move all three real-time Shadow strategy evaluations onto an event-driven C++ pipeline connected by Unix domain socket, while eliminating repeated full-file analytics scans and unnecessary checkpoint writes.

**Architecture:** `reference_price_engine` publishes compact versioned snapshots over a bounded latest-value Unix socket channel. `market_ws_engine` combines those snapshots with current-generation CLOB books and becomes the canonical producer for Directional, Lottery, and Paired Lock evaluations. Python remains a verification, lifecycle, settlement, calibration, acceptance, and Web layer with incremental persistence.

**Tech Stack:** C++17, Boost.Asio/Beast, Boost.PropertyTree, OpenSSL SHA-256, Python 3.10+, pytest, Bash, systemd.

## Global Constraints

- Production remains `SHADOW / DRY RUN`; `real_order_submissions`, `real_orders`, and `real_fills` remain zero.
- The three strategy definitions, decisions, rejection reasons, statistics, and Web sections remain independent.
- Paired Lock remains independent of reference-price acceptance.
- Reference, settlement, fee, depth, freshness, time-window, and market-state failures remain fail closed.
- The CLOB and reference WebSocket hot paths remain C++.
- Tests must fail before production changes are written.
- Existing untracked `AGENTS.md` and `data/*` diagnostics must not be staged.

---

### Task 1: Define And Test The Reference IPC Protocol

**Files:**
- Create: `cpp/reference_ipc/reference_snapshot.hpp`
- Create: `cpp/reference_ipc/reference_snapshot_test.cpp`
- Modify: `scripts/build_cpp.sh`
- Modify: `scripts/build_cpp.ps1`
- Test: `tests/test_reference_ipc_source.py`

**Interfaces:**
- Produces: `reference_ipc::Snapshot`, `reference_ipc::AssetSnapshot`, `reference_ipc::encode_line(const Snapshot&)`, and `reference_ipc::decode_line(std::string_view)`.
- Consumes: no runtime engine state.

- [ ] **Step 1: Write failing protocol tests**

Add a C++ test that round-trips protocol version, producer session, sequence, timestamps, BTC source states, aggregates, model diagnostics, and bounded anchors. Add a Python source test asserting the protocol rejects missing version, sequence rollback, and malformed frames.

```cpp
reference_ipc::Snapshot input;
input.protocol_version = 1;
input.producer_session = "session-a";
input.sequence = 7;
input.produced_monotonic_ns = 100;
input.produced_wall_ms = 200;
input.assets["BTC"].consensus_price = 64000;
const auto output = reference_ipc::decode_line(reference_ipc::encode_line(input));
assert(output.sequence == 7);
assert(output.assets.at("BTC").consensus_price == 64000);
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest -q tests/test_reference_ipc_source.py
```

Expected: FAIL because the protocol header and C++ test target do not exist.

- [ ] **Step 3: Implement the minimal protocol**

Use newline-delimited compact JSON with explicit numeric and nullable fields. Enforce protocol version `1`, nonempty session, positive sequence, known source statuses, and bounded anchor arrays. Keep serialization header-only so both engines use exactly the same schema.

- [ ] **Step 4: Build and run protocol tests**

Run `bash scripts/build_cpp.sh` on Linux and the existing PowerShell C++ build on Windows. Expected: protocol test exits zero and existing binaries still build.

- [ ] **Step 5: Commit Task 1**

```bash
git add cpp/reference_ipc/reference_snapshot.hpp cpp/reference_ipc/reference_snapshot_test.cpp scripts/build_cpp.sh scripts/build_cpp.ps1 tests/test_reference_ipc_source.py
git commit -m "Add versioned reference IPC protocol"
```

---

### Task 2: Publish Reference Snapshots Over Unix Socket

**Files:**
- Modify: `cpp/reference_price_engine/reference_price_engine.cpp`
- Modify: `tests/test_reference_price_engine_source.py`
- Create: `tests/test_reference_ipc_integration.py`

**Interfaces:**
- Consumes: `reference_ipc::Snapshot` and `reference_ipc::encode_line` from Task 1.
- Produces: Unix socket server at configured path, latest-value coalescing, and one-second diagnostic file snapshots.

- [ ] **Step 1: Write failing server behavior tests**

Cover immediate snapshot on connect, strictly increasing sequence, reconnect, latest-frame replacement for a slow client, socket cleanup, and diagnostic file throttling.

```python
frames = read_reference_frames(socket_path, count=3)
assert [row["sequence"] for row in frames] == sorted({row["sequence"] for row in frames})
assert all(row["protocol_version"] == 1 for row in frames)
```

- [ ] **Step 2: Verify RED**

Run `python -m pytest -q tests/test_reference_price_engine_source.py tests/test_reference_ipc_integration.py`. Expected: FAIL because no socket server or frame counters exist.

- [ ] **Step 3: Implement asynchronous latest-value publication**

Add `boost::asio::local::stream_protocol::acceptor`, per-client asynchronous writes, one pending latest frame per client, 20 ms coalescing, and producer-session identity. Keep network callbacks nonblocking. Change `venue-status.json` publication from 100 ms to one second except forced source connection transitions.

- [ ] **Step 4: Verify integration and write reduction**

On Linux, run the engine for ten seconds with one normal and one deliberately slow client. Expected: normal client remains current, slow client memory is bounded, and diagnostic writes are at most 15 in ten seconds excluding connection transitions.

- [ ] **Step 5: Commit Task 2**

```bash
git add cpp/reference_price_engine/reference_price_engine.cpp tests/test_reference_price_engine_source.py tests/test_reference_ipc_integration.py
git commit -m "Stream compact reference snapshots over Unix socket"
```

---

### Task 3: Add Reconnecting Reference Consumer To CLOB Engine

**Files:**
- Modify: `cpp/market_ws_engine/market_ws_engine.cpp`
- Modify: `tests/test_market_ws_engine_source.py`
- Create: `tests/test_market_reference_ipc_integration.py`

**Interfaces:**
- Consumes: protocol decoder from Task 1 and socket frames from Task 2.
- Produces: current reference snapshot, transport health counters, reference sequence/session identity, and fail-closed readiness.

- [ ] **Step 1: Write failing consumer tests**

Cover fragmented frames, multiple frames in one read, malformed JSON, stale snapshot, sequence rollback, producer-session replacement, EOF, reconnect, and source-specific freshness.

```python
server.send(fragment(frame, [3, 11, 29]))
health = wait_for_health(path)
assert health["reference_connected"] is True
assert health["reference_sequence"] == frame["sequence"]
```

- [ ] **Step 2: Verify RED**

Run `python -m pytest -q tests/test_market_ws_engine_source.py tests/test_market_reference_ipc_integration.py`. Expected: FAIL because `ReferenceIpcClient` and health fields are absent.

- [ ] **Step 3: Implement the client**

Use asynchronous local stream connect/read with newline framing and bounded input size. Replace state only for a complete valid frame. On EOF or protocol failure, mark transport unavailable and reconnect with bounded delay. Never carry readiness across producer session replacement.

- [ ] **Step 4: Verify GREEN and paired-lock isolation**

Run the integration tests plus existing market engine tests. Expected: reference failures block only Directional/Lottery state; Paired Lock evaluations continue unchanged.

- [ ] **Step 5: Commit Task 3**

```bash
git add cpp/market_ws_engine/market_ws_engine.cpp tests/test_market_ws_engine_source.py tests/test_market_reference_ipc_integration.py
git commit -m "Consume reference snapshots in CLOB engine"
```

---

### Task 4: Port Directional And Lottery Math To A Pure C++ Module

**Files:**
- Create: `cpp/strategy/ev_strategy.hpp`
- Create: `cpp/strategy/ev_strategy_test.cpp`
- Create: `poly_arb_bot/cpp_strategy_parity.py`
- Create: `tests/fixtures/strategy_parity.json`
- Create: `tests/test_cpp_strategy_parity.py`
- Modify: `scripts/build_cpp.sh`
- Modify: `scripts/build_cpp.ps1`

**Interfaces:**
- Produces: `strategy::probability_model`, `strategy::evaluate_directional`, `strategy::evaluate_lottery`, and canonical audit fields.
- Consumes: immutable market, reference, book, cost, and runtime configuration structs.

- [ ] **Step 1: Generate deterministic parity fixtures and failing tests**

Fixtures cover Up/Down, all four timeframes, boundary entry prices, each fail-closed gate, fee/slippage/buffers, source-specific freshness, and model diagnostics. Python computes expected outputs using the existing canonical evaluator.

```python
for fixture in fixtures:
    cpp = run_cpp_fixture(fixture)
    assert cpp["decision"] == fixture["python"]["decision"]
    assert cpp["reason"] == fixture["python"]["reason"]
    assert cpp["estimated_probability"] == pytest.approx(
        fixture["python"]["estimated_probability"], abs=1e-12
    )
```

- [ ] **Step 2: Verify RED**

Run `python -m pytest -q tests/test_cpp_strategy_parity.py`. Expected: FAIL because the C++ parity executable does not exist.

- [ ] **Step 3: Implement exact model and strategy gates**

Port the current normal-CDF model, model-quality diagnostics, Directional EV, Lottery EV, timeframe windows, and rejection precedence without retuning constants. Load the existing environment values and compute the same canonical sorted config JSON and SHA-256 hash as Python.

- [ ] **Step 4: Verify exact parity**

Run C++ unit tests and Python fixture parity tests. Expected: zero decision/reason mismatches and probability/EV values within `1e-12` for deterministic fixtures.

- [ ] **Step 5: Commit Task 4**

```bash
git add cpp/strategy/ev_strategy.hpp cpp/strategy/ev_strategy_test.cpp poly_arb_bot/cpp_strategy_parity.py tests/fixtures/strategy_parity.json tests/test_cpp_strategy_parity.py scripts/build_cpp.sh scripts/build_cpp.ps1
git commit -m "Port directional strategies to C++"
```

---

### Task 5: Make C++ The Canonical Three-Strategy Audit Producer

**Files:**
- Modify: `cpp/market_ws_engine/market_ws_engine.cpp`
- Modify: `poly_arb_bot/ev_shadow.py`
- Modify: `scripts/run_shadow_loop.sh`
- Modify: `deploy/poly-arb-bot.service`
- Modify: `tests/test_ev_shadow.py`
- Modify: `tests/test_shadow_loop_script.py`
- Modify: `tests/test_market_ws_engine_source.py`

**Interfaces:**
- Consumes: reference state from Task 3 and strategy functions from Task 4.
- Produces: canonical Directional/Lottery/Paired Lock JSONL events and optional Python parity mismatch records.

- [ ] **Step 1: Write failing canonical-producer tests**

Assert C++ emits four independent directional outcome/strategy evaluations per eligible paired market mutation, preserves event identity, suppresses unchanged REJECTs until heartbeat, and blocks ACCEPT under audit backpressure. Assert Python verification mode writes no canonical strategy events.

- [ ] **Step 2: Verify RED**

Run the market source, EV Shadow, and loop script tests. Expected: FAIL because C++ emits only Paired Lock and Python remains canonical.

- [ ] **Step 3: Extend market metadata and emit C++ decisions**

Load condition, asset, interval, window, start, close, Price to Beat, settlement source, active/tradable flags, and fee data. Evaluate only when book or reference input version changes. Add bounded audit queue, fingerprint heartbeat suppression, event identity, and health counters.

- [ ] **Step 4: Add Python verification-only mode**

Consume C++ events, recompute expected Python output, and append only mismatches to `logs/strategy-parity.jsonl`. Default systemd Shadow configuration uses verification mode during the VPS parity window.

- [ ] **Step 5: Verify deterministic and restart behavior**

Run targeted tests, restart event-ID tests, log-rotation tests, and a local synthetic stream. Expected: no duplicate canonical events, all three strategy counts are nonzero, and real-order invariants remain zero.

- [ ] **Step 6: Commit Task 5**

```bash
git add cpp/market_ws_engine/market_ws_engine.cpp poly_arb_bot/ev_shadow.py scripts/run_shadow_loop.sh deploy/poly-arb-bot.service tests/test_ev_shadow.py tests/test_shadow_loop_script.py tests/test_market_ws_engine_source.py
git commit -m "Evaluate all shadow strategies in C++"
```

---

### Task 6: Eliminate Repeated Full-State Checkpoint Writes

**Files:**
- Modify: `poly_arb_bot/ev_shadow.py`
- Modify: `poly_arb_bot/shadow_execution.py`
- Modify: `poly_arb_bot/strategy_shadow_lifecycle.py`
- Modify: `tests/test_ev_shadow.py`
- Modify: `tests/test_shadow_execution.py`
- Modify: `tests/test_strategy_shadow_lifecycle.py`

**Interfaces:**
- Produces: dirty-state tracking, five-second bounded checkpoints, forced shutdown checkpoint, and rotation-safe offsets.

- [ ] **Step 1: Write failing persistence tests**

Use a counting state writer to prove idle polling performs zero writes, many state changes inside five seconds coalesce, graceful shutdown persists, and file truncation/inode replacement resets offsets without duplicate lifecycle events.

- [ ] **Step 2: Verify RED**

Run the three targeted test files. Expected: FAIL because each loop currently rewrites full state every `0.5s`.

- [ ] **Step 3: Implement dirty checkpoints**

Track state mutations explicitly. Persist at most once every five seconds during steady operation, immediately for completed lifecycle transitions that cannot be reconstructed safely, and once from `finally`/signal shutdown handling.

- [ ] **Step 4: Verify GREEN**

Expected: idle write count zero; recovery and deduplication tests pass.

- [ ] **Step 5: Commit Task 6**

```bash
git add poly_arb_bot/ev_shadow.py poly_arb_bot/shadow_execution.py poly_arb_bot/strategy_shadow_lifecycle.py tests/test_ev_shadow.py tests/test_shadow_execution.py tests/test_strategy_shadow_lifecycle.py
git commit -m "Coalesce shadow state checkpoints"
```

---

### Task 7: Replace Web Full-File Rebuilds With Incremental Analytics

**Files:**
- Modify: `poly_arb_bot/web_monitor.py`
- Modify: `poly_arb_bot/shadow_report.py`
- Modify: `tests/test_web_monitor.py`
- Modify: `tests/test_shadow_report.py`

**Interfaces:**
- Produces: incremental analytics cursor, compact persisted summary, rotation recovery, and explicit rebuilding status that does not degrade engine health.

- [ ] **Step 1: Write failing incremental tests**

Instrument file reads and assert the second refresh reads only appended bytes. Cover restart from summary, rotation, duplicate event IDs, malformed final line, and separate engine/analytics health.

```python
first = analytics.refresh()
append_events(log, new_rows)
second = analytics.refresh()
assert second.bytes_read < log.stat().st_size / 100
assert second.counts["evaluations"] == first.counts["evaluations"] + len(new_rows)
```

- [ ] **Step 2: Verify RED**

Run `python -m pytest -q tests/test_web_monitor.py tests/test_shadow_report.py`. Expected: FAIL because the report cache rebuilds the complete file after its five-second TTL.

- [ ] **Step 3: Implement incremental summaries**

Persist count, PnL, rejection, duration, source-age, ledger-tail, and equity state with file identity and byte offset. Read only appended complete lines during normal refresh. Run historical recovery in one bounded background job; expose `analytics_status` separately from `system_status`.

- [ ] **Step 4: Verify latency and correctness**

Use a generated large JSONL fixture. Expected: status request completes under `250ms` after warm-up and produces the same report as a clean full build.

- [ ] **Step 5: Commit Task 7**

```bash
git add poly_arb_bot/web_monitor.py poly_arb_bot/shadow_report.py tests/test_web_monitor.py tests/test_shadow_report.py
git commit -m "Make dashboard analytics incremental"
```

---

### Task 8: Full Verification, VPS Parity Window, And Canonical Cutover

**Files:**
- Modify: `deploy/VPS_DEPLOY.md`
- Modify: `deploy/poly-arb-bot.logrotate`
- Modify: `tests/test_deploy_files.py`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: deployment commands, performance checks, parity cutover gate, and rollback procedure.

- [ ] **Step 1: Write failing deployment assertions**

Assert the deployment guide creates the socket directory, removes stale socket files, starts reference before the market engine, enables parity verification first, checks zero mismatches, and preserves real-order invariants.

- [ ] **Step 2: Verify RED, then update deployment files**

Run deployment tests, implement the exact commands and systemd ordering, then rerun until green.

- [ ] **Step 3: Run repository verification**

Run:

```bash
python -m pytest -q
bash scripts/build_cpp.sh
sed -n '/<script>/,/<\/script>/p' web/index.html | sed '1d;$d' > /tmp/poly-arb-web.js
node --check /tmp/poly-arb-web.js
bash -n scripts/run_shadow_loop.sh
systemd-analyze verify deploy/poly-arb-bot.service deploy/poly-arb-web.service
python -m poly_arb_bot.cli shadow-acceptance
```

Expected: all tests and builds pass, acceptance reports `PASS`, and all real-order counters are zero.

- [ ] **Step 4: Run official VPS integration and performance acceptance**

Run official Gamma/CLOB/CEX/Chainlink discovery and streams, then collect:

```bash
pidstat -u -d -w -p "$(pgrep -d, -f 'market_ws_engine|reference_price_engine|poly_arb_bot')" 1 60
grep -c '"event_type":"strategy_parity_mismatch"' logs/strategy-parity.jsonl
python -m poly_arb_bot.cli shadow-acceptance
```

Required results after warm-up:

```text
strategy parity mismatches = 0
reference IPC receive age p95 < 50 ms
CLOB mutation to evaluation p95 < 250 us
steady aggregate writes < 200 KiB/s excluding rotation
Web normal refresh does not sustain > 80% of one CPU
shadow-acceptance = PASS
real submissions/orders/fills = 0
```

- [ ] **Step 5: Cut over and verify rollback**

Disable Python canonical Directional/Lottery production only after all gates pass. Restart both services, verify canonical counts continue, then exercise the documented rollback once without enabling real orders.

- [ ] **Step 6: Commit Task 8**

```bash
git add deploy/VPS_DEPLOY.md deploy/poly-arb-bot.logrotate tests/test_deploy_files.py
git commit -m "Document C++ strategy cutover gates"
```

- [ ] **Step 7: Push only after final evidence**

```bash
git push origin main
```

Push is allowed only after local verification and the applicable official integration checks pass. VPS-only parity and performance results must remain explicit deployment gates rather than being claimed from local mocks.
