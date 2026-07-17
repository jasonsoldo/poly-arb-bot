# Repeatable Arbitrage Discovery Design

## Goal

Turn the existing complete-set shadow engines into an observable research pipeline that can distinguish repeated market ticks from independent, executable arbitrage episodes. Keep all production execution disabled.

## Scope

This change covers:

- Polymarket Market Channel transport stability.
- Complete-set methods only: `paired_lock`, `split_sell_lock`, and `maker_complete_set_arb`.
- Independent episode counting, execution-funnel counts, and counterfactual size/latency observations.
- Canonical audit, health state, acceptance checks, and Web monitoring.

Directional and lottery strategies continue to run independently as required by the project rules, but they are not classified as locked arbitrage patterns.

## WebSocket Protocol

The engine sends one initial subscription containing every active asset ID immediately after the WebSocket handshake. It uses official dynamic subscribe/unsubscribe messages only for later market rotation and resync. The Market Channel application heartbeat is `PING` every 10 seconds.

Any transport error invalidates all session readiness. A new session remains NOT READY until fresh Up and Down snapshots arrive.

## Pattern Identity

An independent pattern episode is identified by:

```text
strategy + market_id + generation + session + episode_sequence
```

An episode starts only when all method-specific executable conditions become true. Repeated evaluations while those conditions remain true belong to the same episode. The episode ends when any required condition fails, the market rotates, the session changes, or the generation changes.

## Execution Funnel

Each complete-set method exposes the following real audit-derived funnel:

```text
evaluations
depth_passed
fee_passed
latency_survived
independent_episodes
shadow_attempts
both_legs_filled
completed
positive_completed
```

Unavailable stages remain `N/A`; they are never inferred as success. Evaluations and repeated heartbeats do not count as independent episodes or completed trades.

## Counterfactual Observations

For target quantities `1`, `2`, `5`, and `10`, the C++ hot path computes executable multi-level VWAP from the current local books. It records post-fee, post-buffer profit for complete-set buy and split/sell methods. It also records execution stress at `0`, `50`, `100`, and `250` milliseconds by applying the configured fill-decay model.

Counterfactual results are research observations only. They do not create inventory, Shadow positions, PnL, orders, or ACCEPT decisions.

## Repeatability Summary

The Web API groups independent episodes by strategy, asset, timeframe, size, and delay bucket. It reports:

- independent episode count
- distinct close-window count
- median and p95 duration
- median post-cost profit
- latency survival count and rate
- completed Shadow count and post-cost PnL where lifecycle data exists

A group is a `RESEARCH CANDIDATE` only after at least three independent close windows. This label is not an execution approval.

## Safety

- `real_order_submissions = 0`
- `real_orders = 0`
- `real_fills = 0`
- No threshold is relaxed.
- Stale, unsynced, crossed, incomplete, fee-missing, or depth-insufficient books fail closed.
- The Web dashboard displays only canonical audit, lifecycle, and health data.

## Verification

- Regression tests prove one full initial subscription and a 10-second heartbeat.
- C++ unit/source tests cover episode reset and counterfactual math.
- Python tests cover deduplication, funnel identities, and repeatability grouping.
- JavaScript parsing and field-mapping tests pass.
- Official Gamma discovery and Market Channel integration run successfully.
- `shadow-acceptance` remains fail closed and real-order invariants remain zero.
