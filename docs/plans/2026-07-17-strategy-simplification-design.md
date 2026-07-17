# Strategy Simplification Design

## Goal

Keep the three project-required strategies independent while removing runtime and dashboard concepts that blur probability research, locked arbitrage, and execution.

## Runtime roles

- `late_window_directional_ev`: probability research only; no claim of arbitrage.
- `low_price_lottery_ev`: probability research only; no claim of hedge or arbitrage.
- `paired_lock`: the only current locked-arbitrage Shadow execution candidate.
- `split_sell_lock`: read-only complete-set arbitrage observer.
- `maker_complete_set_arb`: read-only quote-geometry and trade-through observer.

The runtime no longer evaluates terminal directional hedging or creates one-sided `inventory_rebalancing_arb` positions. Existing legacy inventory may still be read for containment, but it is not a strategy and cannot accumulate new exposure.

## Dashboard roles

The primary strategy area contains only the three required strategies. Directional and lottery cards are explicitly labelled probability research. Paired lock is labelled an arbitrage execution candidate.

Split-sell and maker observations appear only in a separate repeatable-arbitrage research section. Their events are not orders, fills, completed trades, or PnL.

Terminal hedge and inventory strategy cards, counters, and audit panels are removed.

## Acceptance

Acceptance continues to require all three primary strategies and the repeatable-arbitrage research pipeline. It no longer requires terminal-hedge or inventory evaluation events. Real order, submission, and fill invariants remain zero.

