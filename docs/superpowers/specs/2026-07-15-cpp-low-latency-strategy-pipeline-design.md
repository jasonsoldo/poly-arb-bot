# C++ Low-Latency Strategy Pipeline Design

## Goal

Remove JSON-file polling from the real-time decision path, stop the current write amplification and Web CPU spikes, and evaluate all three Shadow strategies from fresh CLOB and reference data inside C++ without enabling real orders.

## Scope

This change covers the real-time transport and evaluation path:

```text
reference_price_engine
    -> Unix domain socket
    -> market_ws_engine
    -> paired_lock / late_window_directional_ev / low_price_lottery_ev
    -> canonical JSONL audit
```

Python remains responsible for completed-position lifecycle, settlement verification, calibration reports, acceptance checks, and Web aggregation. The existing Python directional evaluator remains available only as a parity verifier during migration.

Real order submission remains disabled. This design does not add credentials, signing, balances, allowances, or a live order client.

## Transport

`reference_price_engine` will own a Boost.Asio Unix domain stream socket at `state/reference-price.sock`. The path is inside the repository runtime directory so the systemd service user can create and remove it without `/run` permissions.

The producer will send newline-delimited compact JSON frames. Each frame contains:

- protocol version
- monotonically increasing snapshot sequence
- producer session ID
- monotonic production timestamp
- wall-clock production timestamp
- per-asset source state required by reference quorum
- fast price, consensus price, settlement reference, divergence, volatility, momentum, sample count, and sample span
- the bounded recent opening and settlement anchors needed for current/next markets

Frames are coalesced to at most one publication every 20 ms. The producer keeps only the newest pending frame for each client, so a slow consumer cannot build an unbounded queue or delay market-data processing.

On connect, the consumer immediately receives the latest complete snapshot. On EOF, protocol error, sequence rollback, producer-session replacement, or stale data, directional strategies fail closed while paired-lock continues independently.

`venue-status.json` remains a diagnostics and Web snapshot. It is no longer consumed by the real-time strategy evaluator and is published once per second or immediately on connection-state transitions.

## C++ Strategy Evaluation

`market_ws_engine` will extend each market with the existing discovery metadata required by directional strategies: condition ID, asset, interval, window, start and close timestamps, Price to Beat, settlement source, fee rate, active state, and accepting-orders state.

The engine will maintain the latest reference snapshot beside the current-generation Up/Down books. A directional evaluation is triggered after either:

- a valid CLOB book mutation for that market; or
- a newer reference snapshot for that market's asset.

Evaluation is skipped when neither input version changed. This prevents duplicate computation during heartbeat traffic.

The C++ probability and EV implementation must preserve the current configured model exactly:

```text
log_distance = log(consensus_price / price_to_beat)
expected_move = volatility_per_sqrt_second * sqrt(seconds_to_close)
final_z = log_distance / expected_move
        + momentum_bps_30s * momentum_z_per_bps
        + paired_book_imbalance * imbalance_z
up_probability = normal_cdf(final_z)
```

Directional and Lottery keep separate time windows, entry-price rules, buffers, liquidity requirements, costs, decisions, and rejection reasons. Paired-lock remains reference-independent.

Configuration values continue to come from the existing environment variables. C++ emits the same config version and canonical SHA-256 config hash as Python. A parity test must fail if defaults, canonical serialization, or decision semantics diverge.

## Audit And Backpressure

C++ becomes the canonical producer for `shadow_eval` events for all three strategies. Event IDs retain run, generation, WebSocket session, market, strategy, outcome, and evaluation sequence identity.

REJECT events are emitted on decision fingerprint change and then at the configured reject heartbeat. ACCEPT events use the configured accept heartbeat and retain opportunity lifecycle events. Audit output is buffered and flushed on bounded intervals, ACCEPT/opportunity transitions, orderly shutdown, and fatal errors. A slow audit sink must not block the WebSocket callback; when its bounded queue is full the engine fails closed for new strategy ACCEPT decisions and reports audit backpressure in health state.

Python `ev_shadow` gains a verification-only mode that consumes C++ directional events and recomputes the decision without writing duplicate canonical events. During migration, mismatches are written to a small parity log with both input and output values. Canonical Python directional production is disabled only after parity tests and an official VPS integration run report zero semantic mismatches.

## Persistence And Web

Python state writers persist only after state changes. Cursor and lifecycle checkpoints are rate-limited to five seconds during steady operation and forced on graceful shutdown. They must recover safely from log rotation by retaining inode/size identity and event IDs.

Web performance statistics use incremental offsets and durable compact summaries. They do not rebuild reports by scanning an entire changing JSONL file every five seconds. Full historical rebuild is an explicit background recovery operation after startup or rotation and cannot change the engine's trading health status from ONLINE to DEGRADED merely because analytics are rebuilding.

## Failure Behavior

- Missing reference socket: paired-lock continues; Directional and Lottery reject with `reference_transport_unavailable`.
- Stale snapshot: Directional and Lottery reject with `reference_data_stale`.
- Sequence rollback or producer-session change: invalidate reference readiness until a complete newer snapshot arrives.
- Malformed frame: discard it, increment protocol errors, and retain the last snapshot only until its freshness deadline.
- Audit queue full: no new ACCEPT; health reports `audit_backpressure`.
- Web or Python lifecycle stopped: C++ market and reference ingestion continue; real orders remain zero.
- Reference producer stopped: no cached reference state may outlive its source-specific freshness threshold.

## Migration

1. Add the socket protocol and reconnect behavior while retaining file output.
2. Add C++ probability and strategy decision parity tests using captured deterministic fixtures.
3. Run C++ and Python evaluators together in verification-only mode.
4. Require zero mismatches for deterministic fixtures and the VPS integration window before making C++ canonical.
5. Disable Python canonical strategy-audit production, then optimize checkpoints and Web summaries.

Rollback is controlled by restoring Python canonical production and retaining the diagnostic file snapshot. Rollback never enables live orders.

## Verification

Automated verification must cover:

- fragmented and combined Unix-socket frames
- reconnect, producer-session replacement, sequence rollback, and stale snapshots
- slow-consumer latest-frame coalescing and bounded memory
- exact C++/Python probability diagnostics and decisions for both outcomes and both strategies
- all fail-closed reference, book, fee, depth, timing, and market-state gates
- event ID stability and duplicate suppression
- log rotation and checkpoint recovery
- Web incremental counts and no repeated full-file rebuild
- existing paired-lock behavior unchanged
- Python suite, C++ build/tests, JavaScript parse, Bash syntax, and systemd validation
- official CEX, Chainlink, Gamma, and Polymarket integration
- `shadow-acceptance = PASS`
- `real_order_submissions = 0`, `real_orders = 0`, and `real_fills = 0`

VPS performance acceptance after warm-up:

```text
reference IPC receive age p95 < 50 ms
CLOB mutation to strategy evaluation p95 < 250 us
Web process must not sustain > 80% of one CPU during normal refresh
steady aggregate disk writes < 200 KiB/s excluding log rotation
no audit backpressure
```

These latency targets measure local pipeline behavior. Network and exchange timestamp uncertainty remain separate metrics and are not relabeled as local latency.

## Non-Goals

- Do not tune probability coefficients or EV thresholds in this migration.
- Do not lower fee, freshness, depth, settlement, or reference quorum requirements.
- Do not merge the three strategy definitions.
- Do not add a generic EDGE or SCORE.
- Do not enable real execution.
