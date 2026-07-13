# Multi-Asset, Multi-Timeframe Shadow Design

## Objective

Extend the paired-lock shadow system from BTC 5m/15m to seven assets and four
timeframes while preserving official market discovery, fail-closed validation,
and zero real order submissions.

Assets:

- BTC
- ETH
- SOL
- XRP
- BNB
- DOGE
- HYPE

Timeframes:

- 5m
- 15m
- 1h
- 4h

## Market Discovery

The scanner must discover recurring markets through the official Gamma Series
and Events APIs. It must not assume that all 28 asset/timeframe combinations
exist and must not synthesize a tradable market from a guessed event slug.

For each configured combination, the scanner will:

1. Resolve the official recurring series by an explicit asset/timeframe series
   slug candidate.
2. Query events in a window wide enough to include the current and next event.
3. Select at most current and next, based on official start/end timestamps.
4. Parse the condition ID and Up/Down token IDs from the official event market.
5. Validate that both token IDs are distinct and have CLOB order books.
6. Emit only valid markets to `data/live_markets.json`.

Missing series, inactive events, malformed tokens, and missing order books are
counted by reason. A failed scan must not replace a previously valid market file
with an empty result.

Each output market includes `asset`, `interval`, `series_id`, `market_id`,
`condition_id`, `up_token_id`, `down_token_id`, official timestamps, and fee
data. The file remains atomically replaced after complete validation.

## Runtime Data Flow

The Python scanner remains outside the latency-sensitive path. It refreshes the
official current/next market set and atomically publishes configuration.

The C++ market engine hot-reloads that configuration, maintains each token's
REST-bootstrapped and WebSocket-updated order book, and evaluates one paired
Up/Down opportunity per market. Market state is keyed by asset, timeframe,
market ID, subscription generation, and WebSocket session. Late messages from
an old generation or session are discarded.

The paired-lock calculation remains:

`EEV = locked_profit - expected_orphan_loss`

where locked profit includes depth-based Up/Down VWAP, dynamic fees, and the
configured execution buffer. A market cannot produce an opportunity unless
both books are synchronized, fresh, deep enough for the full target size, and
the FOK depth simulation passes.

## Reference Prices

The C++ reference-price engine subscribes to official RTDS Binance and Chainlink
topics for all seven assets. It publishes per-asset Binance price, Chainlink
price, divergence, source age, and engine latency to the atomic venue status
file.

Missing, stale, unsupported, or malformed Chainlink data is represented as
`null`, never zero or an inherited price. Reference prices are observability and
validation inputs only; they do not change paired-lock arbitrage mathematics.

## Rotation and Capacity

Current and next are tracked independently for every available
asset/timeframe combination. The theoretical maximum is 56 markets, but the
runtime subscribes only to combinations returned by official APIs and verified
against CLOB.

The scanner preloads next markets. The C++ engine hot-reloads additions and
removals without process restart, increments the subscription generation, and
removes ended markets. Scanner or Gamma failure retains the last verified set.

## Monitoring

The Web API exposes:

- Markets grouped by asset and timeframe.
- Current and next status.
- Latest Up/Down VWAP, fees, locked profit, EEV, FOK, freshness, and decision.
- Binance and Chainlink prices per asset, including stale/unsupported states.
- CLOB ready count, subscription generation, WebSocket session, and resyncs.
- Cumulative evaluations, FOK passes, opportunities, shadow executions, and
  rejection reasons.

The dashboard renders a seven-row by four-column matrix from API data. It does
not hard-code BTC counts. Shadow opportunity, simulated execution, real orders,
and realized PNL remain separate. Real order submissions and realized PNL stay
zero.

## Failure Behavior

- Unknown asset or timeframe: reject configuration.
- Missing official series: record `series_not_found` and continue.
- No current/next event: record `event_not_found` and continue.
- Invalid or duplicate token IDs: reject that market.
- Missing CLOB book or fee schedule: reject that market.
- Stale or unsynchronized order book: reject evaluation.
- Stale reference source: publish `null` and degraded health.
- Empty scan caused by API failure: keep the previous verified market file.
- Duplicate market or token across combinations: reject the duplicate.

## Verification

Automated tests must cover:

- Official series discovery for all configured assets and timeframes.
- Missing combinations without guessed tradable markets.
- Current/next selection and 56-market maximum.
- Asset/timeframe metadata and token validation.
- Atomic output retention on empty or failed scans.
- C++ source-level subscription and per-asset reference parsing.
- Generation isolation and dynamic market reload behavior.
- Dynamic Web matrix and per-asset reference-price rendering.
- Existing paired-lock, FOK, EEV, audit, execution-state, and fail-closed tests.

Release verification requires the complete Python suite, JavaScript syntax
check, shell syntax check, and both C++ engines compiled in an environment with
Boost and OpenSSL. A VPS smoke test must show official current markets, CLOB
READY books, seven-asset reference status where supported, advancing shadow
audit counts, and `real_order_submissions = 0`.
