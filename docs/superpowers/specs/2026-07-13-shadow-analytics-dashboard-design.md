# Shadow Analytics Dashboard Design

## Objective

Build a dense cyberpunk eye-care dashboard inspired by the supplied reference
layout. Every displayed value must come from current market files, C++ health,
reference-price status, shadow audit, or shadow execution records. Missing or
insufficient data is displayed as `N/A`; no placeholder chart, score, latency,
profit, fill, or win statistic is permitted.

## Canonical Data Sources

- `data/live_markets.json`: official current/next market identity, asset,
  timeframe, tokens, fee, and close time.
- `data/shadow-health.json`: CLOB connectivity, ready books, generation,
  session, resyncs, event counters, and freshness.
- `data/venue-status.json`: per-asset Binance and Chainlink prices, source ages,
  divergence, and engine latency.
- `logs/shadow-audit.jsonl`: paired evaluations, FOK status, VWAP, fees,
  locked profit, EEV, duration, freshness, and rejection reason.
- `logs/shadow-execution.jsonl`: simulated two-leg state transitions and
  completed or orphaned execution outcomes.
- `state/orders.json`: real order state. Until live execution exists, real
  orders, fills, realized PNL, and real equity remain zero.

## Metric Definitions

### Counts

- Evaluations: `shadow_eval` records.
- FOK passed: evaluations with both full-size simulated fills.
- Opportunities: `shadow_opportunity` records.
- Simulated executions: unique events that reached execution `COMPLETE`.
- Real orders: submitted authenticated orders only.

### PNL and Equity

Simulated realized PNL is counted only once for each unique execution event that
reaches `COMPLETE`. Its value is the paired opportunity's execution result,
using EEV when no later realized simulation field exists. Rejected, orphaned,
held, or duplicate events contribute no realized PNL.

The simulated equity curve is the chronological cumulative sum of completed
simulated PNL. With no completed executions it is empty or a zero baseline.
Real equity and real realized PNL use real fills only and remain zero otherwise.

### Win Rate and Sharpe

Win rate is profitable completed simulations divided by all completed
simulations. Zero samples displays `N/A`.

Sharpe uses chronological fixed one-hour buckets of completed simulated PNL:

`mean(bucket_return) / sample_stddev(bucket_return) * sqrt(24 * 365)`

It is displayed only with at least 24 non-empty hourly buckets and non-zero
sample standard deviation. The dashboard always displays the sample count.

### Strategy Score

The execution score is 0 when books are stale/unsynchronized, depth or FOK
fails, fee data is absent, EEV is non-positive, or execution state is orphaned.
Otherwise it is a 0-100 bounded weighted score:

- 35% EEV relative to configured minimum EEV.
- 20% available depth relative to target size.
- 15% source freshness.
- 15% Up/Down book skew.
- 15% two-leg fill probability and orphan-risk penalty.

The API returns the component values so the score is auditable.

### Latency

Latency rankings use observed samples only. Each venue exposes latest, p50,
p95, p99, and sample count. Polymarket latency comes from CLOB/WS timestamps;
Binance and Chainlink latency comes from RTDS source-to-receive timestamps; the
engine value comes from measured processing time. No samples displays `N/A`
and no bar.

## Reference Price Freshness

Binance and Chainlink are evaluated independently per asset. A stale Chainlink
source must not erase a fresh Binance price, and vice versa. Unsupported assets
remain `supported: false` with `N/A`. The API includes each source's value, age,
and stale flag.

The C++ reference engine must update recognized RTDS symbols and publish source
timestamps. The Web layer must not transform a valid finite price into null
unless that specific source exceeds the freshness threshold.

## Backend Aggregation

Create one focused analytics module that streams JSONL without loading entire
files into memory. It deduplicates by execution event ID, builds cumulative
metrics and bounded recent records, and returns:

- `performance`
- `equity_curve`
- `trade_ledger`
- `pnl_meter`
- `strategy_score`
- `latency_rankings`
- `pipeline_steps`
- `rejection_reasons`

The Web status endpoint merges this report with current market matrix, CLOB
health, reference prices, and real order state. File size/mtime caching prevents
recomputing unchanged logs on every two-second refresh.

## Dashboard Layout

- Header: markets, evaluations, FOK passed, opportunities, simulated complete,
  real executed, and system status.
- Left column: simulated PNL, win rate, Sharpe with sample count, best/worst
  completed simulations, real PNL, and real equity.
- Center top: selected paired market, Up/Down VWAP, fees, locked PNL, EEV,
  score, and risk state.
- Center middle: real pipeline step states and timestamps.
- Center bottom: recent paired trade/evaluation ledger and real equity curve.
- Right top: seven-asset by four-timeframe matrix with market, READY, and
  opportunity counts.
- Right middle: simulated PNL meter and rejection distribution.
- Right bottom: real latency ranking with p50/p95/p99/sample count.
- Footer: file freshness, WS session/generation, reconnect/resync counts, and
  refresh timestamp.

The palette remains near-black and low-saturation green/cyan with yellow/pink
reserved for blocked/stale/error states. Cards use square, dense borders and no
decorative gradients, fake lines, or animated placeholder bars.

## Failure Behavior

- Malformed JSONL rows are counted and skipped.
- Duplicate execution events are counted once.
- Missing files return empty metrics and `N/A`, not server errors.
- Truncated/rotated logs invalidate the cache and rebuild safely.
- Stale health blocks system status but does not erase historical analytics.
- Unsupported reference sources remain explicit rather than degrading the
  supported sources.
- Shadow metrics never change real PNL, real fills, or real order counts.

## Verification

Tests must prove exact metric formulas, deduplication, minimum Sharpe samples,
zero/empty behavior, independent reference freshness, dynamic matrix counts,
pipeline state derivation, no fake chart points, and source-level rendering of
all dashboard modules.

Release verification requires the complete Python suite, JavaScript syntax,
shell syntax, C++ compilation where dependencies are installed, API fixture
smoke tests, and a real VPS check showing fresh CLOB/reference state. Real order
submissions must remain zero.
