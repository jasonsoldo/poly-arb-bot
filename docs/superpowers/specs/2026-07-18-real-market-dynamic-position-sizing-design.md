# Real-Market Dynamic Position Sizing Design

## Goal

Replace the fixed 10-share Shadow position with deterministic, strategy-specific sizing computed from the current Polymarket CLOB, current model output, official market fees, and explicit Shadow capital limits.

The feature remains `SHADOW / DRY RUN`. It must never submit a real order or describe an executable-book observation as a fill.

## Non-Negotiable Data Semantics

Dynamic sizing may consume only:

- the current Up or Down CLOB ask ladder received by the active WebSocket session;
- the current paired book readiness, freshness, and synchronization state;
- the official market fee schedule already attached to the market;
- current probability-model output derived from live reference feeds;
- current model input-quality diagnostics;
- current market metadata, including timeframe, close time, and minimum order size when available;
- explicit Shadow configuration values such as simulated capital and risk caps.

Configuration values are limits, not market observations. The audit and Web UI must label simulated capital as `SHADOW CAPITAL`, never wallet balance.

The implementation must not synthesize missing depth, fee, probability, liquidity, price, minimum order size, or fill state. Missing required inputs produce a strategy-specific `REJECT` reason.

## Shared Cost Solver

The C++ hot path owns the sizing calculation. Python consumes the canonical C++ audit and independently verifies arithmetic before opening a Shadow position.

For a candidate quantity `q`, the solver walks the real ask ladder and calculates:

```text
buy_notional(q) = sum(level_price * quantity_taken)
buy_vwap(q) = buy_notional(q) / q
fee(q) = official_fee_formula(each fill component)
all_in_cost(q) = buy_notional(q) + fee(q) + execution_buffers(q)
all_in_price(q) = all_in_cost(q) / q
```

The solver may evaluate only quantities supported by the current live depth. It must stop before a level that would breach the configured slippage ceiling, strategy capital cap, or market-specific constraint.

The emitted size is rounded down to the market quantity precision. If the rounded result is below the official minimum order size, the decision is `REJECT` with `dynamic_size_below_market_minimum`.

## Probability Strategy Sizing

`late_window_directional_ev` and `low_price_lottery_ev` share the cost solver but keep independent probability models, caps, and audit namespaces.

### Conservative Probability

Raw model confidence must not directly multiply the position. The conservative outcome probability is:

```text
market_probability = current executable outcome price
quality = clamp(input_quality_score, 0, 1)
shrunk_probability = market_probability
    + quality * (estimated_probability - market_probability)
conservative_probability = clamp(
    shrunk_probability - configured_probability_haircut,
    0,
    1
)
```

The haircut is a declared risk setting and is included in the strategy config hash. If probability or input quality is unavailable, dynamic sizing fails closed.

### Fractional Kelly Capital Budget

For a binary contract with all-in per-share price `c` and conservative probability `p`:

```text
full_kelly_fraction = max(0, (p - c) / (1 - c))
strategy_fraction = min(
    configured_max_capital_fraction,
    configured_fractional_kelly * full_kelly_fraction
)
capital_budget = shadow_capital_usd * strategy_fraction
capital_limited_quantity = capital_budget / c
```

The final probability-strategy quantity is the largest real-depth quantity for which all of the following remain true:

```text
conservative_probability - all_in_price(q) >= minimum_net_ev
all_in_cost(q) <= capital_budget
slippage(q) <= maximum_slippage
q <= strategy_maximum_quantity
q <= real_executable_depth
```

Lottery receives a separate, lower fractional-Kelly multiplier and maximum capital fraction. No probability strategy may borrow the paired-lock sizing rule.

## Paired-Lock Sizing

`paired_lock` does not use external reference prices or a model probability. It walks the real Up and Down ask ladders at equal share quantity and calculates:

```text
net_cost(q) = up_notional(q) + down_notional(q)
    + up_fee(q) + down_fee(q) + execution_buffer(q)
guaranteed_payout(q) = q
locked_profit(q) = guaranteed_payout(q) - net_cost(q)
locked_roi(q) = locked_profit(q) / net_cost(q)
```

The dynamic paired quantity is the largest equal quantity that satisfies real depth, freshness, synchronization, capital, minimum locked profit, minimum locked ROI, and execution-stress EEV constraints. Missing one leg or missing fee data is a rejection, never a fallback to fixed size.

## Binding Constraints and Audit

Every canonical evaluation must include:

```text
sizing_mode = real_market_dynamic_v1
requested_max_size
dynamic_target_size
executable_depth_size
capital_limited_size
slippage_limited_size
market_minimum_size
shadow_capital_usd
capital_budget_usd
input_quality_score
estimated_probability
conservative_probability
probability_haircut
full_kelly_fraction
applied_kelly_fraction
dynamic_vwap
dynamic_fee
dynamic_buffer
dynamic_all_in_cost
dynamic_all_in_price
dynamic_expected_profit
dynamic_maximum_loss
size_binding_constraint
```

Fields that do not apply to `paired_lock` are `null`, not zero. Paired-lock emits both-leg cost fields and its locked-profit chain.

The binding constraint uses one of these stable values:

```text
capital_budget
executable_depth
slippage_limit
strategy_quantity_cap
market_minimum
net_ev_threshold
locked_profit_threshold
locked_roi_threshold
execution_eev_threshold
```

## Lifecycle Rules

Python Shadow lifecycle opens a position only when:

- the C++ canonical decision is `ACCEPT`;
- `sizing_mode` is `real_market_dynamic_v1`;
- `dynamic_target_size` is positive and equals the audited `target_size`;
- the reported notional, fee, and total entry cost recompute within a strict tolerance;
- the event belongs to the current market and has a stable canonical event identity.

The lifecycle stores all sizing fields with the position. Profit exits use the actual stored quantity, current real bid depth, current sell fee, and exit buffer. Settlement completes the remaining quantity using the official outcome. Real-order invariants remain zero.

## Web Display

The dashboard displays, per latest strategy evaluation and active position:

- dynamic quantity;
- Shadow capital budget;
- live executable depth;
- conservative probability and raw model probability;
- all-in entry price;
- expected profit and maximum loss;
- binding constraint;
- `BOOK EXECUTABLE`, `ACTIVE SHADOW`, or `REJECT`, never `FILLED` for an observed book.

Fixed labels such as `ENTRY SIZE $10.00` must be removed. Missing sizing data displays `N/A`.

## Configuration

The deployment environment explicitly defines:

```text
SHADOW_SIZING_CAPITAL_USD
DIRECTIONAL_FRACTIONAL_KELLY
DIRECTIONAL_MAX_CAPITAL_FRACTION
DIRECTIONAL_PROBABILITY_HAIRCUT
DIRECTIONAL_MAX_DYNAMIC_QUANTITY
LOTTERY_FRACTIONAL_KELLY
LOTTERY_MAX_CAPITAL_FRACTION
LOTTERY_PROBABILITY_HAIRCUT
LOTTERY_MAX_DYNAMIC_QUANTITY
PAIRED_MAX_CAPITAL_FRACTION
PAIRED_MAX_QUANTITY
```

Missing, non-finite, zero, or negative Shadow capital fails closed with `sizing_capital_unavailable`. Invalid strategy sizing parameters fail startup validation rather than silently using a fabricated default.

## Performance Boundary

Sizing runs in C++ on the market-update path. It reuses the in-memory order books and does not perform REST, file, Python, or Web calls. It evaluates only the changed market and uses a single pass over relevant price levels. The existing CLOB-to-strategy p95 acceptance budget remains 5,000 microseconds.

## Verification

Tests must prove:

- deeper real books permit larger positions than shallow books;
- worse prices, fees, buffers, slippage, lower input quality, and larger haircuts never increase size;
- the same canonical input deterministically produces the same quantity;
- missing capital, fee, probability, depth, or current snapshots fails closed;
- directional, lottery, and paired-lock use independent formulas and configuration hashes;
- Python rejects inconsistent size or cost audits;
- early profit exit uses the stored dynamic quantity;
- Web shows canonical sizing fields and no fixed-size placeholder;
- real submissions, real orders, and real fills remain zero;
- C++ p95 remains within the configured low-latency budget;
- official REST and WebSocket integration uses current market books and produces non-synthetic audit fields.

## Release Gate

No push or VPS deployment is allowed until Python tests, C++ build/tests, JavaScript parse, Bash syntax, systemd validation, scanner integration, official REST/WebSocket integration, and `shadow-acceptance` pass. Official integration may legitimately produce zero ACCEPT decisions, but it must prove real inputs, deterministic sizing decisions, and fail-closed behavior.
