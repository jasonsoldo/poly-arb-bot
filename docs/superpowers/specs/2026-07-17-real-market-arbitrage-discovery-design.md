# Real-Market Arbitrage Discovery and Falsification Design

## Goal

Discover, measure, and falsify repeatable Polymarket crypto arbitrage patterns
from official live CLOB data. A pattern is reported only when exact post-cost
math, observed latency survival, independent market windows, and out-of-sample
statistics support it. The system must report no candidate when evidence fails.

The system remains `SHADOW / DRY RUN`. It submits no real orders.

## Non-Goals

- Do not manufacture ACCEPT events by reducing fees, buffers, or depth rules.
- Do not call directional probability trading or low-price lottery trading arbitrage.
- Do not infer a deterministic cross-timeframe lock merely because markets share an asset.
- Do not call a maker trade-through an actual fill without queue-position evidence.
- Do not use configured fill probabilities as observed execution results.
- Do not optimize arbitrary rules until they fit the same data used for evaluation.

## Runtime Planes

The always-on process separates observation from execution so that strict trading
gates do not suppress research samples and research samples do not become fake
trades.

### Full-Market Research Observation

Every active market is evaluated on live book changes. Structural arbitrage stores
the fixed size/delay grid. Directional and lottery models emit scheduled prediction
observations for every eligible market/time bucket and attach the official outcome
after settlement. These observations ignore portfolio loss and position-count
limits because they open no position, but they still require valid source data and
record missing-input reasons. They count toward calibration, never PnL or fills.

### Strict Shadow Execution

Only candidates passing their unchanged time, depth, freshness, fee, slippage,
model, and net-EV gates create Shadow positions or execution attempts. Increasing
research sample size is not a reason to loosen execution gates.

### Structural Arbitrage Discovery

The C++ engine enumerates complete-set structures on every real CLOB mutation,
including near misses and delayed outcomes. It is independent of directional and
lottery acceptance and cannot inherit their inventory or PnL labels.

## Official Market Primitives

The implementation uses the current official Polymarket interfaces:

- Market WebSocket for live books, price changes, trades, and lifecycle events;
- CLOB market metadata, token IDs, tick size, and fee rate;
- Conditional Token Framework split and merge mechanics;
- Gamma Series and Events for current/next market discovery.

Relevant official documentation:

- https://docs.polymarket.com/market-data/websocket/market-channel
- https://docs.polymarket.com/api-reference/market-data/get-fee-rate
- https://docs.polymarket.com/api-reference/markets/get-clob-market-info
- https://docs.polymarket.com/trading/ctf/split
- https://docs.polymarket.com/trading/ctf/merge
- https://docs.polymarket.com/trading/fees

## Arbitrage Families

### 1. Buy Both, Then Merge

For equal Up and Down quantities `q` in the same condition:

```text
net_cost = up_buy_cost(q)
         + down_buy_cost(q)
         + rounded_up_fee
         + rounded_down_fee
         + execution_buffer
         + merge_cost

locked_profit = q - net_cost
```

This is a deterministic complete-set payoff after both legs are owned. Execution
is sequential and can create an orphan leg, so observed two-leg survival is part
of the evidence.

### 2. Split, Then Sell Both

Starting with collateral `q`, split into equal Up and Down quantities, then sell
both into bids:

```text
net_proceeds = up_sell_proceeds(q)
             + down_sell_proceeds(q)
             - rounded_up_fee
             - rounded_down_fee
             - execution_buffer
             - split_cost

locked_profit = net_proceeds - q
```

This is deterministic only when collateral, split capability, both token balances,
and both observed sell legs are available. In Shadow, balance and chain-operation
requirements are explicit preconditions, not assumed successes.

### 3. Two-Sided Maker Complete Set

The engine evaluates paired resting quotes whose combined acquisition cost would
form a profitable complete set if both fill. Because public data does not reveal
the bot's queue position, this family is `EXECUTION_DEPENDENT_RESEARCH`.

Public `last_trade_price` trade-through can measure whether one or both quote
levels traded, but cannot prove the bot would have filled. This family cannot be
promoted to executable status until authenticated small-order experiments provide
order acknowledgements, queue exposure, and fill evidence under separate approval.

### 4. Inventory Completion

Given an actually observed or Shadow-created unmatched outcome inventory, buy or
sell the complement only when the resulting complete set has positive guaranteed
value after every historical acquisition cost and current execution cost.

Inventory created by directional or lottery strategies is not reclassified as
arbitrage. Discovery statistics keep origin strategy, unmatched maximum loss, and
completion cost visible.

## Excluded Cross-Market Claims

Different 5m, 15m, 1h, and 4h crypto markets generally use different start prices,
end times, and settlement predicates. They are not deterministic complements.
Cross-market parity is evaluated only when machine-parsed official rules prove an
equivalence or implication. Otherwise it remains correlation research and cannot
enter the arbitrage funnel.

## Online C++ Enumeration

On every accepted order-book mutation, the C++ hot path evaluates all active
conditions at target sizes:

```text
1, 2, 5, 10 shares
```

For each size it computes multi-level VWAP, available depth, independently rounded
fees, tick-valid prices, explicit buffers, and complete-set payout for the four
families above. It evaluates observed execution delays:

```text
0ms, 50ms, 100ms, 250ms
```

Sequential taker families evaluate both leg orders independently:

```text
UP_THEN_DOWN
DOWN_THEN_UP
```

Leg order is part of the episode and configuration identity. Results from the two
orders cannot be pooled before reporting their separate survival, orphan, and PnL
statistics.

The delay grid is research output. The deployable delay assumption must later be
bounded by measured order API acknowledgement and fill-confirmation latency, not
the engine's internal evaluation latency.

The engine emits only state changes, episode boundaries, delayed observations, and
periodic summaries. It does not write every repeated evaluation to JSONL.

## Observed Sequential Shadow Execution

For taker complete-set families, a newly qualified episode creates one Shadow
attempt bound to:

- market ID and condition ID;
- Up and Down token IDs;
- subscription generation and WebSocket session;
- target size and fee schedule;
- start monotonic time and due monotonic time;
- initial full book versions and source timestamps.

Leg 1 is evaluated immediately from the live multi-level book. Leg 2 is evaluated
at the configured deadline using the latest live book. The engine does not reuse
the initial second-leg quote.

An observed leg passes only when:

- the complete target quantity is available;
- the book belongs to the captured generation and session;
- a full current-session snapshot was received;
- the book is fresh, synchronized, and uncrossed;
- the actual VWAP and rounded fee preserve the strategy's locked budget.

Both legs book-executable is true only when both observed legs pass. This is a
real-market delayed-book observation, not proof that an unsubmitted order filled.
A session change,
generation change, market expiry, stale book, missing snapshot, or invalid fee
schedule invalidates the attempt.

If leg 1 passes and leg 2 fails, the attempt is orphaned. Conservative orphan PnL
uses full-depth executable exit VWAP from the latest bid book. If full exit depth
is unavailable, the entire leg-1 cost is treated as loss.

## Exact Cost Rules

Every event records per-leg:

- requested quantity and book-executable quantity;
- best price and multi-level VWAP;
- gross cost or proceeds;
- raw fee and officially rounded fee;
- fee formula version and currency;
- slippage from the initial best level;
- source age, local book age, and book skew;
- explicit execution, split, or merge cost.

Fee-unavailable events fail closed. Buffers are separate fields and cannot be
embedded in fee or VWAP.

## Episode Definition

An episode starts when every qualification gate for a strategy-size-delay tuple
becomes true. Repeated book updates while the tuple remains qualified do not create
new episodes. The episode ends when any gate fails.

A second attempt requires a new episode, new market, or new session/generation.
Episode identity includes strategy, market, size, delay, generation, and session.

## Canonical Audit Events

The C++ producer writes:

- `arb_episode_started` and `arb_episode_ended`;
- `arb_shadow_attempt`;
- `arb_shadow_leg_result`;
- `arb_shadow_book_executable`;
- `arb_shadow_orphaned`;
- `arb_shadow_invalidated`;
- `arb_maker_trade_through_observed`;
- `arb_research_summary`.

All events carry stable IDs and canonical market identity. Every event explicitly
contains `real_order_submissions=0`, `real_orders=0`, and `real_fills=0`.

The existing Python paired executor must stop turning `shadow_opportunity` into a
completed trade through default `filled/filled` environment values. Canonical C++
events count as book-executable Shadow observations, not fills. Only separately
approved authenticated order acknowledgements and fill events may increment real
fill statistics.

## Compact Feature Dataset

The discovery dataset stores episode-level and delayed-observation features rather
than every duplicate evaluation. Each row includes:

- strategy family, asset, timeframe, size, and delay;
- time-to-close bucket;
- Up/Down depth, spread, imbalance, VWAP, and book ages;
- fee rate, total costs, initial profit, delayed profit, and capacity;
- completion, orphan, invalidation, and conservative PnL;
- close window, generation, session, and configuration hash.

This is sufficient for deterministic hypothesis testing while controlling disk
growth and avoiding repeated-event sample inflation.

## Discovery and Falsification

The miner searches only fixed, economically meaningful families and a finite,
predeclared parameter grid. It does not generate arbitrary rules from the outcome
labels.

Data is split chronologically by close window:

1. Discovery set identifies promising family/asset/timeframe/size/delay tuples.
2. Parameters and thresholds are frozen.
3. Validation uses later close windows only.
4. Any parameter change starts a new configuration cohort and resets validation.

Required statistics include:

- independent episode and close-window counts;
- observed both-leg book-executable rate and Wilson 95% interval;
- orphan and invalidation rates;
- mean and median conservative PnL;
- 95% confidence interval for mean PnL;
- total PnL, maximum drawdown, and losing streak;
- p50/p95 opportunity duration;
- capacity at each target size;
- performance by asset, timeframe, and time-to-close bucket.

## Classification

### OBSERVED

At least one independent real-market episode exists. This is not evidence of
repeatability.

### PROVISIONAL

- at least 5 observed attempts;
- at least 3 independent close windows;
- positive conservative total PnL.

### RESEARCH_CANDIDATE

- at least 20 observed attempts;
- at least 10 independent close windows;
- Wilson 95% lower bound for both-leg book execution at least 80%;
- orphan rate at most 5%;
- positive conservative total PnL;
- 95% lower confidence bound of mean per-attempt PnL above zero.

### OUT_OF_SAMPLE_VALIDATED

- frozen parameters evaluated on at least 20 later close windows;
- all `RESEARCH_CANDIDATE` conditions still pass in validation;
- no single market contributes more than 20% of validation PnL;
- profitable capacity exists at a configured minimum size;
- configuration hash is unchanged for the entire validation cohort.

Maker patterns cannot receive `OUT_OF_SAMPLE_VALIDATED` from public trade-through
data alone. They remain `MAKER_RESEARCH_CANDIDATE` until separately approved real
order experiments provide queue and fill evidence.

## Research Funnel

For each family:

```text
evaluations
depth_passed
post_fee_positive
independent_episodes
shadow_attempts
leg_1_book_executable
both_legs_book_executable
orphaned
invalidated
completed
positive_completed
provisional_patterns
research_candidates
out_of_sample_validated
```

Counterfactual observations remain separate and never increment observed attempts,
book-executable results, fills, completions, or PnL.

## Web Dashboard

The dashboard displays:

- per-family funnel and explicit semantics;
- current and recent independent episodes;
- exact cost chain and delayed-book result;
- completion/orphan/invalidation reasons;
- confidence intervals and distinct close windows;
- discovery versus validation cohorts;
- capacity by size and delay;
- `NO REPEATABLE ARBITRAGE FOUND` when no pattern passes.

It must never display counterfactual observations as orders, fills, completed
trades, or realized PnL.

## Health and Recovery

Active attempts are in-memory only. Disconnect, resubscribe, generation change,
market reload, or process restart invalidates them. Old READY state is never reused.

Health exposes active attempts, completed attempts, orphan count, invalidations by
reason, pending-delay timer age, audit backpressure, dropped research events, and
disk status. Research event loss marks analytics degraded.

## Performance Budget

- All live book math and delayed execution tracking run in C++.
- No network or disk blocking occurs on the WebSocket callback.
- Audit output uses the existing bounded asynchronous writer.
- p95 CLOB-to-evaluation remains below the existing 5ms acceptance budget.
- The number of pending attempts is bounded by active markets and parameter tuples.

## Verification

Deterministic C++ tests cover exact VWAP, fees, buffers, split/merge math, delayed
book changes, orphan liquidation, stale/session/generation invalidation, episode
deduplication, and compact emission.

Python tests cover incremental aggregation, event deduplication, confidence
intervals, chronological cohorts, classification gates, no-evidence behavior, and
Web field mapping.

Release requires:

- full Python tests;
- full C++ build and tests;
- JavaScript parse and Bash syntax;
- `shadow-acceptance` PASS;
- official Gamma/CLOB REST integration;
- official WebSocket integration on the VPS;
- real orders, submissions, and fills remain zero;
- at least one real-market observation, or an explicit insufficient-evidence result.

The implementation is successful when it measures and falsifies candidate patterns
correctly. It is not required to claim that a profitable sustainable strategy exists.
