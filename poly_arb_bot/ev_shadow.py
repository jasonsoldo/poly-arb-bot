import json
import hashlib
import math
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .ev_strategies import (
    DirectionalInput,
    decision_audit,
    evaluate_directional,
    evaluate_lottery,
)
from .reference_layer import ReferenceQuote, aggregate_reference


BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "XRP": "XRPUSDT", "BNB": "BNBUSDT", "DOGE": "DOGEUSDT",
}
PRICE_TO_BEAT_CAPTURE_MAX_DELAY_MS = 10_000
STRATEGY_CONFIG_VERSION = "shadow-buy-rules-v2"


def strategy_config():
    values = {
        "directional_min_net_ev": os.getenv("DIRECTIONAL_MIN_NET_EV", "0.015"),
        "directional_latency_buffer": os.getenv("DIRECTIONAL_LATENCY_BUFFER", "0.003"),
        "directional_settlement_buffer": os.getenv("DIRECTIONAL_SETTLEMENT_BUFFER", "0.002"),
        "lottery_min_price": os.getenv("LOTTERY_MIN_PRICE", "0.01"),
        "lottery_max_price": os.getenv("LOTTERY_MAX_PRICE", "0.05"),
        "lottery_min_net_ev": os.getenv("LOTTERY_MIN_NET_EV", "0.015"),
        "lottery_model_buffer": os.getenv("LOTTERY_MODEL_BUFFER", "0.01"),
        "lottery_execution_buffer": os.getenv("LOTTERY_EXECUTION_BUFFER", "0.005"),
        "minimum_liquidity": os.getenv("STRATEGY_MIN_LIQUIDITY", "20"),
        "maximum_slippage": os.getenv("STRATEGY_MAX_SLIPPAGE", "0.01"),
        "maximum_reference_age_ms": os.getenv("REFERENCE_MAX_AGE_MS", "3000"),
        "maximum_book_age_ms": os.getenv("CLOB_MAX_BOOK_AGE_MS", "750"),
        "maximum_clock_skew_ms": os.getenv("MAX_CLOCK_SKEW_MS", "250"),
        "momentum_z_per_bps": os.getenv("MODEL_MOMENTUM_Z_PER_BPS", "0.002"),
        "imbalance_z": os.getenv("MODEL_IMBALANCE_Z", "0.25"),
    }
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return values, hashlib.sha256(encoded).hexdigest()


def capture_opening_prices(markets, venue, existing, now_ms=None):
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    anchors = dict(existing)
    for market_id, market in markets.items():
        if market_id in anchors or market.get("open_price") is not None:
            continue
        start_ms = float(market.get("start_ts") or 0) * 1000
        if not start_ms or now_ms < start_ms:
            continue
        source = market.get("settlement_source")
        if source not in {"binance", "chainlink"}:
            continue
        samples = venue.get("assets", {}).get(market.get("asset"), {}).get(f"{source}_samples", [])
        eligible = [row for row in samples
                    if start_ms <= float(row.get("source_timestamp_ms", 0))
                    <= start_ms + PRICE_TO_BEAT_CAPTURE_MAX_DELAY_MS]
        if eligible:
            sample = min(eligible, key=lambda row: float(row["source_timestamp_ms"]))
            anchors[market_id] = {
                "price": float(sample["price"]),
                "source_timestamp_ms": float(sample["source_timestamp_ms"]),
                "captured_at_ms": now_ms,
                "source": source,
            }
    return anchors


def _historical_volatility(rows):
    closes = [float(row[4]) for row in rows if len(row) > 4 and float(row[4]) > 0]
    returns = [math.log(current / previous) for previous, current in zip(closes, closes[1:])]
    if len(returns) < 2:
        return None, len(returns)
    return statistics.pstdev(returns) / math.sqrt(60), len(returns)


def _load_historical_model(asset, symbol, timeout):
    query = urlencode({"symbol": symbol, "interval": "1m", "limit": 120})
    request = Request(
        f"https://data-api.binance.vision/api/v3/klines?{query}",
        headers={"User-Agent": "poly-arb-bot/1.0", "Accept": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        rows = json.load(response)
    volatility, samples = _historical_volatility(rows)
    if volatility is None or samples < 20:
        return asset, None
    return asset, {
        "volatility_per_sqrt_second": volatility,
        "model_sample_count": samples,
    }


def load_historical_models(timeout=10):
    models = {}
    with ThreadPoolExecutor(max_workers=len(BINANCE_SYMBOLS)) as pool:
        futures = [pool.submit(_load_historical_model, asset, symbol, timeout)
                   for asset, symbol in BINANCE_SYMBOLS.items()]
        for future in as_completed(futures):
            try:
                asset, model = future.result()
                if model:
                    models[asset] = model
            except (OSError, ValueError, TypeError):
                continue
    return models


def _reference_state(asset, settlement_source, maximum_age_ms, file_age_ms=0):
    sources = []
    for name, row in asset.get("sources", {}).items():
        age = row.get("message_age_ms")
        effective_age = None if age is None else max(0.0, float(age) + file_age_ms)
        status = row.get("status", "NOT_RECEIVED")
        if status == "FRESH" and (effective_age is None or effective_age > maximum_age_ms):
            status = "STALE"
        sources.append(ReferenceQuote(
            name, "", row.get("symbol", ""), row.get("market_type", ""),
            row.get("quote_currency", ""), row.get("price"), row.get("bid"), row.get("ask"),
            row.get("source_timestamp"), row.get("received_at"), effective_age, status,
        ))
    selected = next((row for row in sources if row.source == settlement_source), None)
    verified = selected is not None and selected.status == "FRESH" and selected.price is not None
    return aggregate_reference(sources, selected.price if selected else None, verified)


def _up_probability(asset, price_to_beat, seconds_to_close, book_imbalance=None):
    reference = asset.get("consensus_price")
    volatility = asset.get("volatility_per_sqrt_second")
    samples = int(asset.get("model_sample_count", 0))
    if not reference or not price_to_beat or not volatility or samples < 20 or seconds_to_close <= 0:
        return None
    scale = float(volatility) * math.sqrt(seconds_to_close)
    if scale <= 0:
        return None
    momentum = asset.get("momentum_bps_30s")
    if momentum is None or book_imbalance is None:
        return None
    z = math.log(float(reference) / float(price_to_beat)) / scale
    z += float(momentum) * float(os.getenv("MODEL_MOMENTUM_Z_PER_BPS", "0.002"))
    z += float(book_imbalance) * float(os.getenv("MODEL_IMBALANCE_Z", "0.25"))
    return min(.999, max(.001, .5 * (1 + math.erf(z / math.sqrt(2)))))


def evaluate_market_event(event, market, venue, now=None, historical_models=None,
                          opening_prices=None):
    now = time.time() if now is None else now
    asset = venue.get("assets", {}).get(market.get("asset"), {})
    settlement_source = market.get("settlement_source")
    maximum_reference_age_ms = float(os.getenv("REFERENCE_MAX_AGE_MS", "3000"))
    file_age_ms = max(0.0, now * 1000 - float(venue.get("updated_at_ms", now * 1000)))
    reference = _reference_state(
        asset, settlement_source, maximum_reference_age_ms, file_age_ms,
    )
    seconds_to_close = max(0, int(float(market.get("close_ts", 0)) - now))
    model_asset = asset
    model_source = "live_multi_source"
    if (not asset.get("volatility_per_sqrt_second") or
            int(asset.get("model_sample_count", 0)) < 20):
        historical = (historical_models or {}).get(market.get("asset"))
        if historical:
            model_asset = dict(asset, **historical)
            model_source = "binance_historical_1m"
    anchor = (opening_prices or {}).get(market.get("market_id"), {})
    price_to_beat = market.get("open_price")
    price_to_beat_source = "gamma" if price_to_beat is not None else None
    if price_to_beat is None and anchor.get("price") is not None:
        price_to_beat = float(anchor["price"])
        price_to_beat_source = f"{anchor.get('source')}_start_anchor"
    up_imbalance = event.get("up_book_imbalance")
    down_imbalance = event.get("down_book_imbalance")
    paired_imbalance = None
    if up_imbalance is not None and down_imbalance is not None:
        paired_imbalance = (float(up_imbalance) - float(down_imbalance)) / 2
    up_probability = _up_probability(model_asset, price_to_beat, seconds_to_close, paired_imbalance)
    probability_block_reason = None
    if up_probability is None:
        if price_to_beat is None:
            start_ts = float(market.get("start_ts") or 0)
            probability_block_reason = (
                "price_to_beat_capture_missed"
                if start_ts and now * 1000 > start_ts * 1000 + PRICE_TO_BEAT_CAPTURE_MAX_DELAY_MS
                else "price_to_beat_pending"
            )
        elif not model_asset.get("volatility_per_sqrt_second"):
            probability_block_reason = "volatility_unavailable"
        elif int(model_asset.get("model_sample_count", 0)) < 20:
            probability_block_reason = "insufficient_model_samples"
        elif model_asset.get("momentum_bps_30s") is None:
            probability_block_reason = "momentum_unavailable"
        elif paired_imbalance is None:
            probability_block_reason = "order_book_imbalance_unavailable"
        else:
            probability_block_reason = "probability_model_unavailable"
    size = max(float(event.get("size", 0)), 1e-9)
    settlement = asset.get("sources", {}).get(settlement_source, {})
    rows = []
    config_values, config_hash = strategy_config()
    for outcome, fill_key, ask_key, fee_key, depth_key, imbalance_key, probability in (
        ("Up", "up_vwap", "up_best_ask", "up_fee", "up_available_depth", "up_book_imbalance", up_probability),
        ("Down", "down_vwap", "down_best_ask", "down_fee", "down_available_depth", "down_book_imbalance", None if up_probability is None else 1 - up_probability),
    ):
        fill = float(event.get(fill_key, 1))
        best_ask = event.get(ask_key)
        slippage = max(0.0, fill - float(best_ask)) if best_ask is not None else float("inf")
        source_ages = [row.message_age_ms for row in reference.sources
                       if row.status == "FRESH" and row.message_age_ms is not None and
                       (row.market_type == "spot" or row.source == settlement_source)]
        reference_age_ms = max(source_ages) if source_ages else None
        samples = int(model_asset.get("model_sample_count", 0))
        divergence = reference.cross_source_divergence_bps
        confidence = None if up_probability is None else max(0.0, min(1.0,
            min(samples / 120, 1.0) * (1 - min(float(divergence or 0) / 100, 1.0))))
        common = dict(
            market_id=market.get("market_id", ""), condition_id=market.get("condition_id", market.get("market_id", "")),
            asset=market.get("asset", ""), timeframe=market.get("interval", ""), outcome=outcome,
            market_price=float(best_ask) if best_ask is not None else fill,
            expected_fill_price=fill, estimated_probability=probability,
            seconds_to_close=seconds_to_close, price_to_beat=price_to_beat,
            reference=reference, fee_per_share=float(event.get(fee_key, 0)) / size,
            slippage_per_share=slippage,
            latency_risk_buffer=float(os.getenv("DIRECTIONAL_LATENCY_BUFFER", "0.003")),
            settlement_risk_buffer=float(os.getenv("DIRECTIONAL_SETTLEMENT_BUFFER", "0.002")),
            model_uncertainty_buffer=float(os.getenv("LOTTERY_MODEL_BUFFER", "0.01")),
            execution_risk_buffer=float(os.getenv("LOTTERY_EXECUTION_BUFFER", "0.005")),
            liquidity=float(event.get(depth_key, event.get("up_fill" if outcome == "Up" else "down_fill", 0))),
            book_age_ms=float(event.get("source_age_ms", 1e9)),
            reference_age_ms=reference_age_ms,
            clock_skew_ms=asset.get("clock_skew_ms"),
            minimum_liquidity=float(os.getenv("STRATEGY_MIN_LIQUIDITY", "20")),
            maximum_slippage=float(os.getenv("STRATEGY_MAX_SLIPPAGE", "0.01")),
            maximum_reference_age_ms=maximum_reference_age_ms,
            maximum_book_age_ms=float(os.getenv("CLOB_MAX_BOOK_AGE_MS", "750")),
            maximum_clock_skew_ms=float(os.getenv("MAX_CLOCK_SKEW_MS", "250")),
            market_active=bool(market.get("active", True)) and float(market.get("close_ts", 0)) > now,
            market_tradable=bool(market.get("accepting_orders", True)),
            target_depth_ok=float(event.get("up_fill" if outcome == "Up" else "down_fill", 0)) >= size,
            momentum_bps_30s=model_asset.get("momentum_bps_30s"),
            order_book_imbalance=event.get(imbalance_key), confidence=confidence,
            settlement_source_verified=(settlement.get("status") == "FRESH" and
                                        settlement.get("price") is not None),
            probability_block_reason=probability_block_reason,
        )
        for strategy, evaluator in (
            ("late_window_directional_ev", evaluate_directional),
            ("low_price_lottery_ev", evaluate_lottery),
        ):
            input_row = DirectionalInput(strategy=strategy, **common)
            if strategy == "late_window_directional_ev":
                result = evaluator(input_row, float(os.getenv("DIRECTIONAL_MIN_NET_EV", "0.015")))
            else:
                result = evaluator(
                    input_row, float(os.getenv("LOTTERY_MIN_PRICE", "0.01")),
                    float(os.getenv("LOTTERY_MAX_PRICE", "0.05")),
                    float(os.getenv("LOTTERY_MIN_NET_EV", "0.015")),
                )
            event_id = f'{event.get("event_id", market.get("market_id"))}:{strategy}:{outcome}'
            audit = decision_audit(
                input_row, result, event_id,
                int(event.get("subscription_generation", event.get("generation", 0))),
                int(event.get("ws_session_id", event.get("session", 0))),
                int(event.get("evaluation_sequence", 0)), float(event.get("ts", now)),
            )
            audit["model_type"] = "configured_distributional_shadow"
            audit["model_sample_count"] = int(model_asset.get("model_sample_count", 0))
            audit["model_source"] = model_source if up_probability is not None else None
            audit["price_to_beat_source"] = price_to_beat_source
            audit["price_to_beat_source_timestamp_ms"] = anchor.get("source_timestamp_ms")
            audit["target_size"] = size
            audit["window"] = market.get("window", "current")
            audit["config_version"] = STRATEGY_CONFIG_VERSION
            audit["config_hash"] = config_hash
            audit["strategy_config"] = config_values
            rows.append(audit)
    return rows


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def process_once(audit_path, market_path, venue_path, output_path, state_path,
                 historical_models=None):
    state = _load(state_path, {"offset": 0, "processed": []})
    processed = set(state.get("processed", []))
    markets = {row.get("market_id"): row for row in _load(market_path, {"markets": []}).get("markets", [])}
    venue = _load(venue_path, {})
    opening_prices = capture_opening_prices(markets, venue, state.get("opening_prices", {}))
    audit_path, output_path, state_path = Path(audit_path), Path(output_path), Path(state_path)
    if not audit_path.exists():
        return 0
    if audit_path.stat().st_size < state.get("offset", 0):
        state["offset"] = 0
    emitted = 0
    with audit_path.open(encoding="utf-8") as source, output_path.open("a", encoding="utf-8") as target:
        source.seek(state.get("offset", 0))
        while line := source.readline():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("strategy") != "paired_lock" or event.get("event_type") != "shadow_eval":
                continue
            event_id = event.get("event_id")
            market = markets.get(event.get("market_id"))
            if not event_id or event_id in processed or not market:
                continue
            for row in evaluate_market_event(event, market, venue,
                                             historical_models=historical_models,
                                             opening_prices=opening_prices):
                target.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
                emitted += 1
            processed.add(event_id)
        state["offset"] = source.tell()
    state["processed"] = list(processed)[-20000:]
    state["opening_prices"] = opening_prices
    temporary = state_path.with_suffix(state_path.suffix + ".tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary, state_path)
    return emitted


def main():
    historical_models = load_historical_models()
    while True:
        process_once("logs/shadow-audit.jsonl", "data/live_markets.json", "data/venue-status.json",
                     "logs/strategy-audit.jsonl", "state/ev-shadow.json", historical_models)
        time.sleep(.5)


if __name__ == "__main__":
    main()
