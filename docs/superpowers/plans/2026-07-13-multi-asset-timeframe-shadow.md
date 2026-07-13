# Multi-Asset, Multi-Timeframe Shadow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Monitor paired-lock opportunities for BTC, ETH, SOL, XRP, BNB, DOGE, and HYPE across official 5m, 15m, 1h, and 4h markets.

**Architecture:** Python discovers and validates official Gamma current/next markets and atomically publishes a normalized market contract. Existing C++ engines consume that contract for CLOB evaluation and stream per-asset RTDS reference prices; the Web API builds its matrix and source status from those canonical files.

**Tech Stack:** Python 3.9, pytest, C++17, Boost.Beast, OpenSSL, Polymarket Gamma/CLOB/RTDS, vanilla HTML/JavaScript, Bash/systemd.

## Global Constraints

- Assets are exactly `BTC,ETH,SOL,XRP,BNB,DOGE,HYPE`.
- Timeframes are exactly `5m,15m,1h,4h`.
- Discover through official Gamma Series and Events APIs; never treat a guessed event slug as a tradable market.
- Emit at most current and next per available asset/timeframe combination, with an absolute maximum of 56 markets.
- Do not overwrite a valid market file when discovery fails or produces no valid markets.
- All latency-sensitive order-book, VWAP, FOK, fee, EEV, and reference-stream processing remains C++.
- Missing fees, books, tokens, or stale data fail closed.
- Real order submissions and realized PNL remain zero.

---

### Task 1: Canonical Asset and Series Contract

**Files:**
- Modify: `poly_arb_bot/market_scanner.py`
- Modify: `poly_arb_bot/live_signals.py`
- Test: `tests/test_market_scanner.py`

**Interfaces:**
- Produces: `ASSET_CONFIG`, mapping canonical symbols to Gamma title, Binance symbol, Chainlink symbol, and series prefix.
- Produces: `MarketScanner.updown_series_slugs(intervals) -> list[tuple[str, str, str]]`, yielding `(asset, interval, slug)`.
- Produces: `LiveMarketSpec.interval: str` and `LiveMarketSpec.series_id: str | None`.

- [ ] **Step 1: Write failing contract tests**

```python
def test_series_matrix_contains_all_seven_assets_and_four_timeframes():
    rows = MarketScanner().updown_series_slugs(["5m", "15m", "1h", "4h"])
    assert len(rows) == 28
    assert ("BTC", "5m", "btc-up-or-down-5m") in rows
    assert ("HYPE", "4h", "hype-up-or-down-4h") in rows

def test_spec_carries_asset_interval_and_series_id():
    spec = MarketScanner().spec_from_market(MARKET, interval="4h", series_id="42")
    assert (spec.asset, spec.interval, spec.series_id) == ("XRP", "4h", "42")
```

- [ ] **Step 2: Verify RED**

Run: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest -q tests/test_market_scanner.py`

Expected: FAIL because the series method returns BTC-only strings and `LiveMarketSpec` has no interval/series fields.

- [ ] **Step 3: Implement the minimum canonical mapping**

Use one dictionary for all seven assets. Validate intervals against `INTERVAL_SECONDS`; generate the 28 explicit series candidates. Extend `spec_from_market` with keyword arguments `interval=None, series_id=None` and persist them in `LiveMarketSpec`.

- [ ] **Step 4: Verify GREEN and compatibility**

Run: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest -q tests/test_market_scanner.py tests/test_live_signals.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add poly_arb_bot/market_scanner.py poly_arb_bot/live_signals.py tests/test_market_scanner.py
git commit -m "Expand canonical crypto market matrix"
```

### Task 2: Official Current/Next Discovery and Retention

**Files:**
- Modify: `poly_arb_bot/cli.py`
- Modify: `poly_arb_bot/polymarket_data.py`
- Test: `tests/test_market_scanner.py`
- Test: `tests/test_atomic_market_file.py`

**Interfaces:**
- Consumes: `(asset, interval, slug)` candidates from Task 1.
- Produces: atomic `live_markets.json` records with `asset`, `interval`, and `series_id`.
- Produces: diagnostics `series_not_found`, `event_not_found`, `invalid_market`, and existing CLOB rejection reasons.

- [ ] **Step 1: Write failing multi-series scan tests**

Create fake Gamma responses where BTC 5m and ETH 1h exist, HYPE 15m is absent, and each existing series has three future events. Assert only two per series are considered, metadata is retained, missing series is diagnosed, and the output is not replaced when no valid market remains.

- [ ] **Step 2: Verify RED**

Run: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest -q tests/test_market_scanner.py tests/test_atomic_market_file.py`

Expected: FAIL because `scan_updown_markets` assumes string slugs, uses a one-hour query horizon, and does not attach series metadata.

- [ ] **Step 3: Implement official matrix discovery**

Iterate the Task 1 tuples, resolve each official series, and query through `now + 2 * interval_seconds` so current and next 4h events are eligible. Pass `asset`, `interval`, and `series_id` into parsing. Deduplicate by market and token IDs, cap at 56, preserve the last valid atomic file on empty/error outcomes, and print deterministic diagnostic totals.

- [ ] **Step 4: Verify GREEN**

Run: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest -q tests/test_market_scanner.py tests/test_atomic_market_file.py tests/test_polymarket_data.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add poly_arb_bot/cli.py poly_arb_bot/polymarket_data.py tests/test_market_scanner.py tests/test_atomic_market_file.py
git commit -m "Discover official multi-asset current markets"
```

### Task 3: Seven-Asset C++ Reference Stream

**Files:**
- Modify: `cpp/reference_price_engine/reference_price_engine.cpp`
- Modify: `tests/test_reference_price_engine_source.py`
- Create: `tests/fixtures/reference_prices.jsonl`

**Interfaces:**
- Produces: `venue-status.json` with `assets.<ASSET>.binance`, `chainlink`, `divergence_bps`, source ages, and supported flags.
- Preserves: top-level `updated_at_ms` and `engine_latency_us`.

- [ ] **Step 1: Write failing source and fixture tests**

Assert all seven Binance symbols and Chainlink symbols are present, status is keyed by canonical asset, missing Chainlink remains JSON `null`, and no BTC-specific output keys remain the canonical interface.

- [ ] **Step 2: Verify RED**

Run: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest -q tests/test_reference_price_engine_source.py`

Expected: FAIL because the engine stores one BTC pair.

- [ ] **Step 3: Implement fixed-size per-asset state**

Use a compile-time seven-entry asset table and `std::map<std::string, PriceState>`. Subscribe to the Binance wildcard and explicit Chainlink symbols, normalize incoming symbols, update only the matching asset, and atomically serialize all seven records. Do not add dynamic configuration or another process.

- [ ] **Step 4: Verify tests and compile**

Run tests as in Step 2. Then on MSYS2/VPS run `bash scripts/build_cpp.sh`.

Expected: tests PASS and `built build/reference_price_engine`.

- [ ] **Step 5: Commit**

```powershell
git add cpp/reference_price_engine/reference_price_engine.cpp tests/test_reference_price_engine_source.py tests/fixtures/reference_prices.jsonl
git commit -m "Stream seven crypto reference prices"
```

### Task 4: Dynamic Web API Matrix and Freshness

**Files:**
- Modify: `poly_arb_bot/web_monitor.py`
- Modify: `tests/test_web_monitor.py`

**Interfaces:**
- Consumes: normalized `live_markets.json`, C++ audit JSONL, and `venue-status.json` from Tasks 2 and 3.
- Produces: `market_matrix[asset][interval]` with market, readiness, decision, and current/next counts.
- Produces: `reference_prices.assets[asset]`, with stale values nulled per asset rather than globally.

- [ ] **Step 1: Write failing API tests**

Construct BTC 5m current/next and HYPE 4h current fixtures plus fresh/stale per-asset prices. Assert matrix cells contain the correct counts and only the stale asset's prices become `None`.

- [ ] **Step 2: Verify RED**

Run: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest -q tests/test_web_monitor.py`

Expected: FAIL because no dynamic matrix exists and freshness is BTC-global.

- [ ] **Step 3: Implement API aggregation**

Build the 7x4 matrix from canonical market metadata, merge latest audit by market ID, and apply source-age checks independently to every asset. Keep cumulative audit reporting unchanged.

- [ ] **Step 4: Verify GREEN**

Run the command from Step 2.

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add poly_arb_bot/web_monitor.py tests/test_web_monitor.py
git commit -m "Expose multi-asset shadow matrix"
```

### Task 5: Dynamic Dashboard

**Files:**
- Modify: `web/index.html`
- Modify: `tests/test_web_dashboard_source.py`

**Interfaces:**
- Consumes: `market_matrix` and `reference_prices.assets` from Task 4.
- Produces: seven asset rows, four timeframe columns, selected-cell paired metrics, and per-asset Binance/Chainlink status.

- [ ] **Step 1: Write failing dashboard source test**

Assert the source contains all seven canonical asset labels, renders matrix cells from `market_matrix`, uses no hard-coded BTC cell IDs, and reads per-asset reference data.

- [ ] **Step 2: Verify RED**

Run: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest -q tests/test_web_dashboard_source.py`

Expected: FAIL because the dashboard has no dynamic seven-asset matrix.

- [ ] **Step 3: Implement minimal dynamic rendering**

Render rows from a fixed seven-symbol display order and columns from the fixed four timeframes. Select the first current market with an audit record for the detail panel. Display `N/A`, `STALE`, `NO MARKET`, or real values; never draw placeholder latency bars or profitability.

- [ ] **Step 4: Verify source and JavaScript syntax**

Run the test from Step 2. Extract the script and run `node --check` on it.

Expected: PASS and no JavaScript syntax errors.

- [ ] **Step 5: Commit**

```powershell
git add web/index.html tests/test_web_dashboard_source.py
git commit -m "Render seven-asset timeframe monitor"
```

### Task 6: Runtime Configuration, Full Verification, and Push

**Files:**
- Modify: `scripts/run_shadow_loop.sh`
- Modify: `deploy/VPS_DEPLOY.md`
- Modify: `tests/test_shadow_loop_script.py`

**Interfaces:**
- Runs: scanner with `--intervals 5m,15m,1h,4h`.
- Preserves: Shadow-only execution and current/next hot reload.

- [ ] **Step 1: Write failing runtime test**

Assert the production loop and deployment commands request all four intervals and retain `current,next`.

- [ ] **Step 2: Verify RED**

Run: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest -q tests/test_shadow_loop_script.py`

Expected: FAIL because runtime currently requests only 5m/15m.

- [ ] **Step 3: Update runtime and deployment documentation**

Change only the interval argument and expected diagnostics. Keep refresh, safety, and systemd behavior unchanged.

- [ ] **Step 4: Run complete local verification**

Run:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest -q
node --check $env:TEMP\poly_dashboard_check.js
```

Run under MSYS2/VPS:

```bash
bash -n scripts/run_shadow_loop.sh
bash scripts/build_cpp.sh
```

Expected: all tests PASS, syntax checks return zero, and both C++ WebSocket engines build where Boost/OpenSSL are installed.

- [ ] **Step 5: Run official API smoke test**

```bash
python -m poly_arb_bot.cli scan-updown --output data/live_markets.json --intervals 5m,15m,1h,4h --slug-window current,next
python -c 'import json; d=json.load(open("data/live_markets.json")); print(len(d["markets"])); [print(m["asset"],m["interval"],m["market_id"]) for m in d["markets"]]'
```

Expected: only official CLOB-valid current/next markets, no duplicates, and no more than 56 rows. Zero rows is a failed release gate unless official diagnostics prove every configured series unavailable.

- [ ] **Step 6: Commit and push**

```powershell
git add scripts/run_shadow_loop.sh deploy/VPS_DEPLOY.md tests/test_shadow_loop_script.py
git commit -m "Run all crypto shadow timeframes"
git push origin main
```
