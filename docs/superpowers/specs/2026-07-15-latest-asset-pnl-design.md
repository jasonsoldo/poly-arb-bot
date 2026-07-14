# Latest Asset Shadow PnL Design

## Goal

Extend the existing asset/timeframe matrix with one `LATEST SIM PNL` column. Each
asset row shows the realized simulated PnL from that asset's most recent completed
Shadow trade.

## Data Semantics

- Source only canonical completed Shadow lifecycle events already consumed by
  `shadow_report`.
- Select the latest completed trade per asset by completion timestamp.
- Exclude evaluations, ACCEPT events, active positions, and real PnL.
- Return `null` when an asset has no completed simulation.
- Preserve strategy, completion timestamp, market ID, and timeframe as metadata
  for an explanatory tooltip.

## API And UI

`/api/status` exposes an `asset_latest_pnl` object keyed by supported asset. Each
value contains `pnl`, `strategy`, `ts`, `market_id`, and `timeframe`, or is `null`.

The `MARKETS / ASSET x TIMEFRAME` table adds a final `LATEST SIM PNL` column:

- positive values use the existing good color;
- negative values use the existing warning color;
- zero is neutral;
- missing completed data displays `N/A`;
- the cell tooltip identifies strategy, timeframe, and completion time.

The column is explicitly simulated and does not affect market counts, strategy
acceptance, completed totals, or real-order statistics.

## Verification

- Python test covers latest-per-asset selection, timestamp ordering, and `null`
  for assets without completed simulations.
- Dashboard source test confirms the new column and API field are rendered.
- Full Python tests, JavaScript syntax check, and `git diff --check` must pass.
