# Observed CLOB Arbitrage Validation Design

## Goal

Use live official Polymarket CLOB books to determine whether a post-cost
crypto complete-set arbitrage pattern survives sequential execution and repeats
across independent market windows. This remains Shadow-only: no order is sent.

## Scope

The first implementation covers `paired_lock` buy-both execution. It does not
change directional, lottery, inventory, maker, or split/sell acceptance rules.
The existing counterfactual size/delay grid remains research-only and cannot
count as an observed fill.

## Evidence Semantics

The following are not execution evidence:

- configured leg fill probabilities;
- an opportunity evaluation;
- a counterfactual latency-stress result;
- Python defaults such as `filled/filled`;
- a completed settlement created without observed legs.

Observed execution evidence requires two real order-book observations from the
same market, condition, subscription generation, and WebSocket session.

## Execution Model

When a new `paired_lock` episode first satisfies all existing acceptance gates,
the C++ engine creates one immutable Shadow attempt:

1. Capture market identity, generation, session, target size, dynamic fee rate,
   execution buffer, book timestamps, and monotonic start time.
2. Simulate leg 1 immediately against the current multi-level ask book. The leg
   fills only when the complete target size is available and the book is fresh,
   synchronized, uncrossed, and from the captured generation/session.
3. Schedule leg 2 for `start + configured leg_interval_us` (50ms by default).
4. At the deadline, use the latest live CLOB ask book. Recompute full-depth VWAP
   and the independently rounded fee. Do not reuse the original quote.
5. Complete only when the second leg has full depth and the observed all-in cost
   remains below the locked budget.

The locked budget is:

```text
maximum_net_cost = guaranteed_payout - minimum_locked_profit

observed_net_cost =
    leg_1_cost
  + leg_2_cost
  + rounded_leg_1_fee
  + rounded_leg_2_fee
  + execution_buffer

observed_locked_profit = guaranteed_payout - observed_net_cost
```

`both_legs_filled` is true only when:

```text
leg_1_full_depth
and leg_2_full_depth
and observed_net_cost <= maximum_net_cost
```

## Failure and Orphan Handling

An attempt is invalidated before leg 2 when the market expires, the WebSocket
disconnects, generation/session changes, either token loses its full snapshot,
or the latest book becomes stale/crossed. Invalidation is not a completed trade.

If leg 1 was observed filled and leg 2 cannot fill within the locked budget, the
attempt becomes `ORPHANED`. The engine computes a conservative liquidation value
from the latest full-depth bid VWAP for leg 1. If no complete exit depth exists,
the conservative orphan loss is the full observed leg-1 cost. Orphans never count
as locked completions or positive completed trades.

Only one active attempt is allowed per market and continuous qualification
episode. A new attempt requires the opportunity to become disqualified and then
qualify again, or a new generation/session.

## Canonical Events

The C++ engine writes these JSONL events to `logs/shadow-audit.jsonl`:

- `shadow_arb_attempt`: immutable initial opportunity and leg-1 observation;
- `shadow_arb_leg_result`: observed leg-2 book and pass/fail result;
- `shadow_arb_complete`: both legs observed, final costs and locked profit;
- `shadow_arb_orphaned`: leg 1 observed but leg 2 failed;
- `shadow_arb_invalidated`: attempt lost session, generation, freshness, or market validity.

Every event carries:

- stable attempt ID and event ID;
- market ID, condition ID, asset, timeframe, window, close time;
- token IDs, generation, session, sequence, timestamps;
- target size and configured/actual delay;
- per-leg fill quantity, VWAP, raw fee, rounded fee, and book age;
- execution buffer, net cost, payout, locked profit, and decision reason;
- `real_order_submissions=0`, `real_orders=0`, `real_fills=0`.

The Python `ShadowExecutionStateMachine` must no longer turn a paired opportunity
into a completed lifecycle using default environment values. For `paired_lock`,
it consumes the canonical C++ observed events only.

## Research Funnel

The incremental research state reports observed counts, never nullable execution
claims:

```text
evaluations
depth_passed
fee_passed
latency_survived
independent_episodes
shadow_attempts
leg_1_filled
both_legs_filled
orphaned
invalidated
completed
positive_completed
```

`latency_survived` is derived from observed leg-2 results, not the configured
probability model. Counterfactual latency statistics remain in a separate section.

## Repeatability Classification

Patterns are grouped by strategy, asset, timeframe, target size, and configured
delay. Duplicate events and continuous re-evaluations do not increase the sample.

Classifications:

- `OBSERVED`: at least one independent attempt;
- `PROVISIONAL`: at least 5 attempts across at least 3 close windows;
- `RESEARCH_CANDIDATE`: at least 20 attempts across at least 10 close windows,
  Wilson 95% lower bound for both-leg fill rate at least 80%, orphan rate at most
  5%, positive total observed Shadow PnL, and the 95% lower confidence bound of
  mean per-attempt PnL above zero.

Per-attempt PnL includes zero for invalidated attempts before any observed fill,
observed locked profit for completed attempts, and conservative liquidation PnL
for orphans. This prevents survivorship bias.

## Web Dashboard

The dashboard displays the observed execution funnel, active attempts, recent
orphan/invalidation reasons, both-leg fill rate with Wilson interval, mean PnL
confidence interval, distinct close windows, and classification. It labels all
values `SHADOW / OBSERVED CLOB` and keeps real orders, fills, and real PnL at zero.

## Persistence and Recovery

Active attempts are in-memory only. A restart, disconnect, resubscribe, or market
reload invalidates them; they are never resumed against a new book session.
Completed audit events remain canonical and are incrementally aggregated with
stable event-ID deduplication. Existing configured-probability observations remain
historical counterfactual data and are excluded from observed execution metrics.

## Verification

Deterministic C++ tests cover:

- both books retain depth after 50ms and complete profitably;
- second-leg price moves above the locked budget;
- second-leg depth disappears;
- session/generation changes before the deadline;
- stale/crossed books;
- independently rounded fees and exact net-cost math;
- orphan liquidation with and without exit depth;
- one attempt per continuous episode.

Python tests cover event deduplication, funnel identities, confidence intervals,
classification thresholds, legacy exclusion, and Web field mapping. Release also
requires the full Python suite, full C++ build/tests, JavaScript parse, Bash syntax,
`shadow-acceptance`, official REST discovery, and official WebSocket observation on
the VPS. A lack of real opportunities is valid; fabricated attempts are not.
