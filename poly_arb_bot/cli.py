import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from .binance_source import BinanceSource
from .chainlink_source import ChainlinkSource
from .clob_client import PolymarketClobClient
from .cpp_bridge import score_positions_cpp
from .execution_engine import ExecutionEngine
from .live_signals import LiveMarketSpec, LiveSignalBuilder
from .logger import JsonlLogger
from .market_scanner import INTERVAL_SECONDS, MarketScanner
from .models import MarketSignal, PositionCurve
from .polymarket_data import PolymarketDataClient, parse_jsonish, parse_timestamp_seconds
from .position_manager import PositionManager
from .risk_manager import RiskManager
from .state_store import JsonStateStore
from .strategy import UpDownStrategy
from .strategy_config import StrategyConfig
from .web_monitor import serve
from .shadow_ws import ShadowMarketMonitor


def load_snapshot(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    positions = {
        item["market_id"]: PositionCurve(**item)
        for item in data.get("positions", [])
    }
    signals = [MarketSignal(**item) for item in data.get("signals", [])]
    return positions, signals


def load_live_markets(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return [LiveMarketSpec(**item) for item in data.get("markets", [])]


def write_live_snapshot(markets_path: Path, output_path: Path, binance_base_url: str) -> int:
    markets = load_live_markets(markets_path)
    builder = LiveSignalBuilder(BinanceSource(base_url=binance_base_url), PolymarketClobClient())
    signals = builder.build(markets)
    payload = {
        "positions": [],
        "signals": [signal.__dict__ for signal in signals],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"WROTE {output_path} signals={len(signals)}")
    return 0


def scan_markets(output_path: Path, gamma_base_url: str, limit: int) -> int:
    client = PolymarketDataClient(base_url=gamma_base_url)
    scanner = MarketScanner()
    events = client.events(limit=limit, active=True)
    specs = scanner.specs_from_events(events)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(scanner.to_payload(specs), indent=2), encoding="utf-8")
    print(f"WROTE {output_path} markets={len(specs)} events_scanned={len(events)}")
    if not specs:
        print("NO_MARKETS Parsed zero complete Crypto Up/Down markets; inspect Gamma fields for price_to_beat/open_price.")
    return 0


def scan_updown_markets(output_path: Path, gamma_base_url: str, intervals: str, slug_window: str, base_ts: int = None) -> int:
    client = PolymarketDataClient(base_url=gamma_base_url)
    scanner = MarketScanner()
    interval_list = [item.strip() for item in intervals.split(",") if item.strip()]
    now_ts = int(base_ts or time.time())
    series_slugs = scanner.updown_series_slugs(interval_list)
    events = []
    candidates = {slug: (asset, interval) for asset, interval, slug in series_slugs}
    gamma_request_count = 0
    scan_started = time.monotonic()
    deadline_seconds = float(os.getenv("SCAN_DEADLINE_SECONDS", "45"))
    deadline = scan_started + deadline_seconds
    gamma_workers = max(1, min(12, int(os.getenv("GAMMA_CONCURRENCY", "12"))))

    def remaining():
        return max(0.0, deadline - time.monotonic())

    def require_budget():
        budget = remaining()
        if budget <= 0:
            raise TimeoutError("scan_global_deadline")
        client.http.timeout = min(10.0, budget)

    stage_started = time.monotonic()
    print(f"GAMMA_SERIES_START candidates={len(candidates)} active_workers={min(gamma_workers, len(candidates))}", flush=True)

    def fetch_series(slugs):
        try:
            require_budget()
            return client.series_by_slugs(slugs), 0
        except (OSError, RuntimeError, TimeoutError):
            return [], 1

    series_batches = [list(candidates)[index:index + 7] for index in range(0, len(candidates), 7)]
    gamma_request_count += len(series_batches)
    matching_series = []
    series_errors = 0
    with ThreadPoolExecutor(max_workers=gamma_workers) as executor:
        for rows, errors in executor.map(fetch_series, series_batches):
            matching_series.extend(rows)
            series_errors += errors
    print(
        f"GAMMA_SERIES_DONE elapsed_ms={int((time.monotonic() - stage_started) * 1000)} "
        f"gamma_request_count={gamma_request_count} matched={len(matching_series)} errors={series_errors}", flush=True,
    )
    matched_slugs = set()
    missing_events = 0
    series_by_id = {}
    for series in matching_series:
        series_slug = str(series.get("slug") or "")
        if series_slug not in candidates:
            continue
        matched_slugs.add(series_slug)
        asset, interval = candidates[series_slug]
        series_id = str(series.get("id"))
        series_by_id[series_id] = (asset, interval, series_slug)
    for slug, (asset, interval) in candidates.items():
        if slug not in matched_slugs:
            print(f"DISCOVERY asset={asset} interval={interval} series={slug} status=SERIES_NOT_FOUND", flush=True)
    event_candidates = {}
    hourly_windows = []
    for series_id, (asset, interval, series_slug) in series_by_id.items():
        seconds = INTERVAL_SECONDS[interval]
        current_start = now_ts - now_ts % seconds
        if interval == "1h":
            hourly_windows.append((series_id, current_start - seconds, current_start + 3 * seconds))
            continue
        prefix = series_slug.replace("-up-or-down-", "-updown-")
        for start in (current_start, current_start + seconds):
            event_candidates[f"{prefix}-{start}"] = series_id
    grouped_events = {series_id: [] for series_id in series_by_id}
    if event_candidates or hourly_windows:
        def fetch_events(slugs):
            try:
                require_budget()
                return client.events_by_slugs(slugs), 0
            except (OSError, RuntimeError, TimeoutError):
                return [], 1

        def fetch_hourly(window):
            series_id, start, end = window
            try:
                require_budget()
                return series_id, client.events_by_series_window(series_id, start, end), 0
            except (OSError, RuntimeError, TimeoutError):
                return series_id, [], 1

        event_slugs = list(event_candidates)
        event_batches = [event_slugs[index:index + 20] for index in range(0, len(event_slugs), 20)]
        stage_started = time.monotonic()
        gamma_request_count += len(event_batches) + len(hourly_windows)
        request_count = len(event_batches) + len(hourly_windows)
        print(f"GAMMA_EVENTS_START candidates={len(event_slugs) + len(hourly_windows) * 2} active_workers={min(gamma_workers, request_count)}", flush=True)
        window_events = []
        with ThreadPoolExecutor(max_workers=gamma_workers) as executor:
            futures = {executor.submit(fetch_events, batch): ("events", None) for batch in event_batches}
            futures.update({executor.submit(fetch_hourly, window): ("hourly", window[0]) for window in hourly_windows})
            for future in as_completed(futures):
                kind, series_id = futures[future]
                result = future.result()
                if kind == "events":
                    rows, errors = result
                    window_events.extend(rows)
                else:
                    series_id, rows, errors = result
                    grouped_events[series_id].extend(rows)
                series_errors += errors
        print(
            f"GAMMA_EVENTS_DONE elapsed_ms={int((time.monotonic() - stage_started) * 1000)} "
            f"gamma_request_count={gamma_request_count} events={len(window_events) + sum(map(len, grouped_events.values()))} errors={series_errors}", flush=True,
        )
    else:
        window_events = []
        print(f"GAMMA_EVENTS_START candidates=0 active_workers=0", flush=True)
        print(f"GAMMA_EVENTS_DONE elapsed_ms=0 gamma_request_count={gamma_request_count} events=0 errors={series_errors}", flush=True)
    for event in window_events:
        series_id = event_candidates.get(str(event.get("slug") or ""))
        if series_id in grouped_events:
            grouped_events[series_id].append(event)
    for series_id, (asset, interval, series_slug) in series_by_id.items():
        horizon = INTERVAL_SECONDS[interval] * 2
        selected = current_series_events(grouped_events[series_id], now_ts, limit=2, horizon_seconds=horizon)
        print(
            f"DISCOVERY asset={asset} interval={interval} series={series_slug} "
            f"events_found={len(grouped_events[series_id])} candidates={len(selected)}",
            flush=True,
        )
        if not selected:
            missing_events += 1
        for event in selected:
            event["_asset"] = asset
            event["_interval"] = interval
            event["_series_id"] = series_id
            event["markets"] = tradable_markets(event.get("markets") or [])
            if event["markets"]:
                events.append(event)
    missing_series = len(candidates) - len(matched_slugs) if not series_errors else 0
    specs = scanner.specs_from_events(events)
    unique = {}
    used_tokens = set()
    for spec in specs:
        tokens = {spec.up_token_id, spec.down_token_id}
        if spec.market_id in unique or len(tokens) != 2 or used_tokens.intersection(tokens):
            continue
        unique[spec.market_id] = spec
        used_tokens.update(tokens)
    print(f"MARKET_PARSE_DONE candidates={len(specs)} unique_markets={len(unique)}", flush=True)
    if remaining() <= 0:
        print(f"SCAN_DEADLINE_EXCEEDED elapsed_ms={int((time.monotonic() - scan_started) * 1000)} kept=true", flush=True)
        return 3
    clob = PolymarketClobClient()
    diagnostics = {}
    examples = []
    stage_started = time.monotonic()
    print(
        f"CLOB_VALIDATE_START candidates={len(unique)} active_workers={min(8, len(unique))} "
        f"clob_request_count={1 if unique else 0}", flush=True,
    )
    valid, rejected = filter_specs_with_orderbooks(list(unique.values()), clob, diagnostics, examples)
    print(
        f"CLOB_VALIDATE_DONE elapsed_ms={int((time.monotonic() - stage_started) * 1000)} "
        f"completed_workers={len(unique)} valid={len(valid)} rejected={rejected}", flush=True,
    )
    unique = {spec.market_id: spec for spec in valid}
    if remaining() <= 0:
        print(f"SCAN_DEADLINE_EXCEEDED elapsed_ms={int((time.monotonic() - scan_started) * 1000)} kept=true", flush=True)
        return 3
    if unique:
        print(f"WRITE_START path={output_path} markets={len(unique)}", flush=True)
        write_market_payload_atomic(output_path, scanner.to_payload(unique.values()), now_ts)
        print(f"WRITE_DONE path={output_path} markets={len(unique)}", flush=True)
    else:
        print(f"WRITE_START path={output_path} markets=0", flush=True)
        print(f"WRITE_DONE path={output_path} markets=0 kept=true", flush=True)
    detail = " ".join(f"{key}={value}" for key, value in sorted(diagnostics.items()))
    print(f"CLOB_VALID markets={len(unique)} rejected={rejected} {detail}".rstrip())
    for example in examples[:5]:
        print(f"CLOB_REJECT market_id={example[0]} token_id={example[1]} reason={example[2]}")
    print(
        f"GAMMA_SERIES series_checked={len(series_slugs)} series_error={series_errors} series_not_found={missing_series} "
        f"event_not_found={missing_events} current_events={len(events)} parsed_markets={len(specs)}"
    )
    print(f"{'WROTE' if unique else 'KEPT'} {output_path} markets={len(unique)}")
    if not unique and not rejected:
        print("NO_CURRENT_UPDOWN_MARKETS no current event in configured official recurring series")
    return 0 if unique else 3


def write_market_payload_atomic(output_path: Path, payload, generated_at: int) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    version = int(generated_at * 1000)
    document = {"version": version, "generated_at": generated_at, "markets": payload.get("markets", [])}
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, output_path)


def current_series_events(events, now_ts, limit=None, horizon_seconds=3600):
    current = [
        event for event in events
        if event.get("active") and not event.get("closed")
        and (close_ts := parse_timestamp_seconds(event.get("endDate"))) is not None
        and now_ts < close_ts <= now_ts + horizon_seconds
    ]
    current.sort(key=lambda event: parse_timestamp_seconds(event.get("endDate")))
    return current[:limit] if limit is not None else current


def tradable_markets(markets):
    return [market for market in markets if market.get("enableOrderBook") and market.get("acceptingOrders")]


def filter_specs_with_orderbooks(specs, clob, diagnostics=None, examples=None):
    valid = []
    diagnostics = diagnostics if diagnostics is not None else {}
    examples = examples if examples is not None else []

    if hasattr(clob, "get_books"):
        try:
            books = clob.get_books([token for spec in specs for token in (spec.up_token_id, spec.down_token_id)])
        except Exception as exc:
            diagnostics["http_error"] = len(specs)
            examples.append(("", "", str(exc)[:240]))
            return [], len(specs)
        for spec in specs:
            up_book = books.get(spec.up_token_id)
            down_book = books.get(spec.down_token_id)
            if up_book is None or down_book is None:
                diagnostics["no_orderbook"] = diagnostics.get("no_orderbook", 0) + 1
                continue
            fee_rate = up_book.fee_rate or down_book.fee_rate or spec.fee_rate
            if fee_rate is None or fee_rate <= 0:
                diagnostics["fee_schedule_unavailable"] = diagnostics.get("fee_schedule_unavailable", 0) + 1
                continue
            if not up_book.asks or not down_book.asks:
                diagnostics["empty_asks"] = diagnostics.get("empty_asks", 0) + 1
                continue
            valid.append(replace(spec, fee_rate=fee_rate))
        return valid, len(specs) - len(valid)

    def validate(spec):
        try:
            info = clob.get_market_info(spec.market_id)
            token_map = {
                str(token.get("o", "")).lower(): str(token.get("t", ""))
                for token in info.get("t", [])
                if isinstance(token, dict)
            }
            up_token_id = token_map.get("up")
            down_token_id = token_map.get("down")
            if not up_token_id or not down_token_id:
                return None, "clob_outcome_mismatch", (spec.market_id, "", "CLOB market did not return Up and Down tokens")
            fee_data = info.get("fd") if isinstance(info.get("fd"), dict) else {}
            fee_value = fee_data.get("r", spec.fee_rate)
            try:
                fee_rate = float(fee_value)
            except (TypeError, ValueError):
                fee_rate = 0
            if fee_rate <= 0:
                return None, "fee_schedule_unavailable", (spec.market_id, "", "CLOB fee schedule unavailable")
            spec = replace(spec, up_token_id=up_token_id, down_token_id=down_token_id, fee_rate=fee_rate)
            up_book = clob.get_book(spec.up_token_id)
            down_book = clob.get_book(spec.down_token_id)
            if not up_book.asks or not down_book.asks:
                return None, "empty_asks", None
            elif up_book.ask_liquidity(1.0) <= 0 or down_book.ask_liquidity(1.0) <= 0:
                return None, "no_buy_depth", None
            return spec, None, None
        except RuntimeError as exc:
            message = str(exc)
            lower = message.lower()
            if "invalid token id" in lower or "http get 400" in lower:
                key = "invalid_token"
            elif "no orderbook" in lower or "http get 404" in lower:
                key = "no_orderbook"
            elif "network failed" in message:
                key = "network_error"
            else:
                key = "http_error"
            return None, key, (spec.market_id, spec.up_token_id, message[:240])
        except Exception:
            return None, "unexpected_error", (spec.market_id, spec.up_token_id, "unexpected error")

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(specs)))) as executor:
        results = executor.map(validate, specs)
        for spec, reason, example in results:
            if spec is not None:
                valid.append(spec)
            else:
                diagnostics[reason] = diagnostics.get(reason, 0) + 1
                if example:
                    examples.append(example)
    return valid, len(specs) - len(valid)


def inspect_gamma(gamma_base_url: str, limit: int) -> int:
    client = PolymarketDataClient(base_url=gamma_base_url)
    scanner = MarketScanner()
    events = client.events(limit=limit, active=True)
    candidates = scanner.candidate_markets(events)
    print(f"events_scanned={len(events)} crypto_updown_candidates={len(candidates)}")
    for index, market in enumerate(candidates[:10]):
        title = market.get("question") or market.get("title") or market.get("slug")
        interesting = {
            key: market.get(key)
            for key in sorted(market.keys())
            if key in {
                "conditionId",
                "question",
                "title",
                "slug",
                "outcomes",
                "clobTokenIds",
                "tokens",
                "priceToBeat",
                "openPrice",
                "startPrice",
                "targetPrice",
                "endDate",
                "endDateIso",
                "rules",
                "description",
                "resolutionSource",
            }
        }
        print(f"CANDIDATE {index} {title}")
        print(json.dumps(interesting, ensure_ascii=False)[:3000])
    return 0


def discover_crypto_markets(output_path: Path, gamma_base_url: str, limit: int) -> int:
    now = int(time.time())
    events = PolymarketDataClient(base_url=gamma_base_url).events_keyset(limit=limit)
    clob = PolymarketClobClient()
    markets = []
    rejected = 0
    for event in events:
        for market in event.get("markets") or []:
            if not is_crypto_market(market):
                continue
            close_ts = parse_timestamp_seconds(market.get("endDate") or market.get("endDateIso"))
            condition_id = str(market.get("conditionId") or "")
            if not condition_id or close_ts is None or close_ts <= now:
                rejected += 1
                continue
            try:
                info = clob.get_market_info(condition_id)
                tokens = info.get("t", [])
                token_ids = [str(token.get("t", "")) for token in tokens if isinstance(token, dict) and token.get("t")]
                if len(token_ids) != 2:
                    rejected += 1
                    continue
                books = [clob.get_book(token_id) for token_id in token_ids]
                if not all(book.bids and book.asks for book in books):
                    rejected += 1
                    continue
            except RuntimeError:
                rejected += 1
                continue
            markets.append({
                "market_id": condition_id,
                "title": market.get("question") or market.get("title") or market.get("slug"),
                "slug": market.get("slug"),
                "close_ts": close_ts,
                "outcomes": parse_jsonish(market.get("outcomes")) or [token.get("o") for token in tokens],
                "token_ids": token_ids,
                "min_order_size": info.get("mos"),
                "tick_size": info.get("mts"),
                "taker_base_fee_bps": info.get("tbf"),
                "fee_details": info.get("fd"),
            })
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps({"markets": markets}, indent=2), encoding="utf-8")
    print(f"CLOB_CRYPTO markets={len(markets)} rejected={rejected} events_scanned={len(events)}")
    print(f"WROTE {output_path} markets={len(markets)}")
    return 0


def is_crypto_market(market) -> bool:
    title = " ".join(str(market.get(key, "")) for key in ("question", "title")).lower()
    slug = str(market.get("slug", "")).lower()
    return any(term in title for term in ("bitcoin", "ethereum", "solana", "xrp", "dogecoin", "bnb", "crypto")) or slug.startswith(("btc-", "eth-", "sol-", "xrp-", "doge-", "bnb-"))


def print_binance_quote(symbol: str, binance_base_url: str) -> int:
    ticker = BinanceSource(base_url=binance_base_url).ticker(symbol)
    print(
        f"{ticker.symbol} price={ticker.price} bid={ticker.bid_price} bid_qty={ticker.bid_qty} "
        f"ask={ticker.ask_price} ask_qty={ticker.ask_qty} latency_ms={ticker.latency_ms}"
    )
    return 0


def print_clob_book(token_id: str, size: float) -> int:
    book = PolymarketClobClient().get_book(token_id)
    expected = book.expected_buy_price(size)
    print(
        f"token_id={token_id} best_bid={book.best_bid} best_ask={book.best_ask} "
        f"expected_buy_price_{size}={expected} ask_liquidity_0.99={book.ask_liquidity(0.99)} "
        f"latency_ms={book.latency_ms}"
    )
    return 0


def print_chainlink_price(rpc_url: str, feed_address: str) -> int:
    price = ChainlinkSource(rpc_url).latest_price(feed_address)
    print(
        f"feed={price.feed_address} price={price.price} decimals={price.decimals} "
        f"updated_at={price.updated_at} stale={price.stale}"
    )
    return 0


def run_simulation(
    snapshot: Path,
    mode: str,
    require_cpp: bool,
    live_enabled: bool,
    state_file: Path = None,
    log_file: Path = None,
) -> int:
    positions, signals = load_snapshot(snapshot)
    config = StrategyConfig(trading_mode=mode, live_enabled=live_enabled)
    position_manager = PositionManager(positions)
    strategy = UpDownStrategy(config)
    risk = RiskManager(config)
    state_store = JsonStateStore(state_file) if state_file else None
    event_logger = JsonlLogger(log_file) if log_file else None
    execution = ExecutionEngine(config, state_store=state_store)

    exe_name = "pnl_curve_engine.exe" if os.name == "nt" else "pnl_curve_engine"
    exe_path = Path("build") / exe_name
    curves = score_positions_cpp(positions.values(), exe_path, require_cpp=require_cpp)
    print("PNL_CURVES")
    for curve in curves:
        print(
            f"{curve.market_id} {curve.classification} "
            f"cost={curve.total_cost:.2f} up={curve.pnl_if_up:.2f} down={curve.pnl_if_down:.2f}"
        )

    print("ORDER_DECISIONS")
    accepted = 0
    for signal in strategy.candidates(signals):
        position = position_manager.get(signal.market_id, signal.title)
        order = strategy.build_order_intent(signal, position)
        decision = risk.check(signal, order, position, position_manager.total_exposure())
        if not decision.allowed:
            if event_logger:
                event_logger.write("risk_block", {"market_id": signal.market_id, "outcome": signal.outcome, "reason": decision.reason})
            print(f"BLOCK {signal.market_id} {signal.outcome} reason={decision.reason}")
            continue
        result = execution.submit(order)
        accepted += int(result.accepted)
        if event_logger:
            event_logger.write(
                "order_decision",
                {
                    "market_id": order.market_id,
                    "outcome": order.outcome,
                    "size": order.size,
                    "limit_price": order.limit_price,
                    "client_order_id": order.client_order_id,
                    "status": result.status,
                    "execution_reason": result.reason,
                    "strategy_reason": order.reason,
                },
            )
        print(
            f"{result.status.upper()} {order.market_id} {order.outcome} "
            f"size={order.size:.4f} price={order.limit_price:.4f} "
            f"execution_reason={result.reason}; strategy_reason={order.reason}"
        )

    print(f"SUMMARY accepted={accepted} mode={mode}")
    return 0


def run_live_loop(
    markets_path: Path,
    output_path: Path,
    mode: str,
    interval_seconds: float,
    iterations: int,
    require_cpp: bool,
    live_enabled: bool,
    binance_base_url: str,
    auto_scan: bool,
    gamma_base_url: str,
    scan_intervals: str,
    slug_window: str,
    state_file: Path,
    log_file: Path,
) -> int:
    count = 0
    while iterations <= 0 or count < iterations:
        loop_started = time.time()
        try:
            if auto_scan:
                scan_updown_markets(markets_path, gamma_base_url, scan_intervals, slug_window)
            write_live_snapshot(markets_path, output_path, binance_base_url)
            run_simulation(output_path, mode, require_cpp, live_enabled, state_file=state_file, log_file=log_file)
        except Exception as exc:
            if log_file:
                JsonlLogger(log_file).write("loop_error", {"error_type": type(exc).__name__, "error": str(exc)})
            print(f"LOOP_ERROR {type(exc).__name__}: {exc}")
        count += 1
        elapsed = time.time() - loop_started
        sleep_for = max(0.0, interval_seconds - elapsed)
        if iterations > 0 and count >= iterations:
            break
        time.sleep(sleep_for)
    print(f"LIVE_RUN_DONE iterations={count} mode={mode}")
    return 0


def run_shadow_ws(markets_path: Path, size: float, fee_rate: float, log_file: Path) -> int:
    import asyncio
    from .shadow_ws import ShadowMarketMonitor
    markets = load_live_markets(markets_path)
    selected = [market.__dict__ for market in markets if market.asset == "Bitcoin" and market.title.startswith("Bitcoin Up or Down")]
    if not selected:
        raise SystemExit("No BTC 5m/15m markets found; run scan-updown --intervals 5m,15m first")
    print(f"SHADOW_WS markets={len(selected)} tokens={len(selected) * 2} fee_rate={fee_rate}")
    asyncio.run(ShadowMarketMonitor(selected, size=size, fee_rate=fee_rate, log_file=log_file).run())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "simulate",
            "scan-markets",
            "scan-updown",
            "inspect-gamma",
            "discover-crypto",
            "live-snapshot",
            "live-run",
            "web-monitor",
            "shadow-ws",
            "binance-quote",
            "clob-book",
            "chainlink-price",
            "shadow-acceptance",
        ],
    )
    parser.add_argument("--snapshot", default="data/sample_live_snapshot.json")
    parser.add_argument("--markets", default="data/live_markets.example.json")
    parser.add_argument("--output", default="data/live_snapshot.json")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--binance-base-url", default="https://data-api.binance.vision")
    parser.add_argument("--gamma-base-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--intervals", default="5m,15m")
    parser.add_argument("--slug-window", choices=["current", "current,next", "previous,current", "previous,current,next"], default="current,next")
    parser.add_argument("--base-ts", type=int)
    parser.add_argument("--token-id")
    parser.add_argument("--size", type=float, default=25.0)
    parser.add_argument("--fee-rate", type=float, default=0.07)
    parser.add_argument("--rpc-url")
    parser.add_argument("--feed-address")
    parser.add_argument("--mode", choices=["dry_run", "simulation", "live"], default="dry_run")
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--iterations", type=int, default=0)
    parser.add_argument("--auto-scan", action="store_true")
    parser.add_argument("--state-file", default="")
    parser.add_argument("--log-file", default="logs/orders.jsonl")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--require-cpp", action="store_true")
    parser.add_argument("--live-enabled", action="store_true")
    args = parser.parse_args()
    if args.command == "web-monitor":
        serve(args.host, args.port, Path("web"), Path("data"), Path(args.log_file), Path(args.state_file or "state/orders.json"))
        return 0
    if args.command == "shadow-ws":
        return run_shadow_ws(Path(args.markets), args.size, args.fee_rate, Path(args.log_file))
    if args.command == "shadow-acceptance":
        from .shadow_acceptance import run as run_acceptance
        return run_acceptance(Path("data"), Path(args.log_file), Path(args.state_file or "state/orders.json"))

    if args.command == "simulate":
        return run_simulation(
            Path(args.snapshot),
            args.mode,
            args.require_cpp,
            args.live_enabled,
            state_file=Path(args.state_file) if args.state_file else None,
            log_file=Path(args.log_file),
        )
    if args.command == "live-snapshot":
        return write_live_snapshot(Path(args.markets), Path(args.output), args.binance_base_url)
    if args.command == "scan-markets":
        return scan_markets(Path(args.output), args.gamma_base_url, args.limit)
    if args.command == "scan-updown":
        return scan_updown_markets(Path(args.output), args.gamma_base_url, args.intervals, args.slug_window, args.base_ts)
    if args.command == "discover-crypto":
        return discover_crypto_markets(Path(args.output), args.gamma_base_url, args.limit)
    if args.command == "inspect-gamma":
        return inspect_gamma(args.gamma_base_url, args.limit)
    if args.command == "live-run":
        return run_live_loop(
            Path(args.markets),
            Path(args.output),
            args.mode,
            args.interval_seconds,
            args.iterations,
            args.require_cpp,
            args.live_enabled,
            args.binance_base_url,
            args.auto_scan,
            args.gamma_base_url,
            args.intervals,
            args.slug_window,
            Path(args.state_file or "state/orders.json"),
            Path(args.log_file),
        )
    if args.command == "binance-quote":
        return print_binance_quote(args.symbol, args.binance_base_url)
    if args.command == "clob-book":
        if not args.token_id:
            raise SystemExit("--token-id is required")
        return print_clob_book(args.token_id, args.size)
    if args.command == "chainlink-price":
        if not args.rpc_url or not args.feed_address:
            raise SystemExit("--rpc-url and --feed-address are required")
        return print_chainlink_price(args.rpc_url, args.feed_address)
    raise ValueError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
