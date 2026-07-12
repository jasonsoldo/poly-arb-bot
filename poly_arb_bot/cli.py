import argparse
import json
import os
import time
from pathlib import Path

from .binance_source import BinanceSource
from .chainlink_source import ChainlinkSource
from .clob_client import PolymarketClobClient
from .cpp_bridge import score_positions_cpp
from .execution_engine import ExecutionEngine
from .live_signals import LiveMarketSpec, LiveSignalBuilder
from .logger import JsonlLogger
from .market_scanner import MarketScanner
from .models import MarketSignal, PositionCurve
from .polymarket_data import PolymarketDataClient
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
    include_previous = slug_window in ("previous,current", "previous,current,next")
    include_next = slug_window in ("current,next", "previous,current,next")
    slugs = scanner.updown_slugs(interval_list, now_ts=base_ts, include_previous=include_previous, include_next=include_next)
    events = []
    for slug in slugs:
        events.extend(client.events_by_slug(slug))
    specs = scanner.specs_from_events(events)
    unique = {spec.market_id: spec for spec in specs}
    if not unique:
        fallback = client.markets(limit=1000, active=True)
        fallback = [
            row for row in fallback
            if str(row.get("slug", "")).startswith("btc-updown-5m-")
            or str(row.get("slug", "")).startswith("btc-updown-15m-")
        ]
        specs = scanner.specs_from_events(fallback)
        unique = {spec.market_id: spec for spec in specs}
    valid, rejected = filter_specs_with_orderbooks(list(unique.values()), PolymarketClobClient())
    unique = {spec.market_id: spec for spec in valid}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(scanner.to_payload(unique.values()), indent=2), encoding="utf-8")
    print(f"CLOB_VALID markets={len(unique)} no_orderbook={rejected}")
    print(f"WROTE {output_path} markets={len(unique)} slugs_checked={len(slugs)}")
    return 0


def filter_specs_with_orderbooks(specs, clob):
    valid = []
    rejected = 0
    for spec in specs:
        try:
            up_book = clob.get_book(spec.up_token_id)
            down_book = clob.get_book(spec.down_token_id)
            if up_book.asks and down_book.asks:
                valid.append(spec)
            else:
                rejected += 1
        except Exception:
            rejected += 1
    return valid, rejected


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
            "live-snapshot",
            "live-run",
            "web-monitor",
            "shadow-ws",
            "binance-quote",
            "clob-book",
            "chainlink-price",
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
