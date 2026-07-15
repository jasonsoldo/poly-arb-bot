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
INTERVAL_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14_400}
STRATEGY_CONFIG_VERSION = "shadow-buy-rules-v3"


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
        "minimum_model_sample_span_seconds": os.getenv("MODEL_MIN_SAMPLE_SPAN_SECONDS", "60"),
    }
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return values, hashlib.sha256(encoded).hexdigest()


def _market_start_ts(market):
    explicit_start = float(market.get("start_ts") or 0)
    close_ts = float(market.get("close_ts") or 0)
    duration = INTERVAL_SECONDS.get(market.get("interval"))
    derived_start = close_ts - duration if close_ts > 0 and duration else 0.0
    if derived_start > 0 and (explicit_start <= 0 or abs(explicit_start - derived_start) > 5):
        return derived_start
    return explicit_start if explicit_start > 0 else derived_start


def _anchor_identity(market, start_ts=None):
    return {
        "market_id": market.get("market_id"),
        "condition_id": market.get("condition_id", market.get("market_id")),
        "asset": market.get("asset"),
        "interval": market.get("interval"),
        "start_ts": float(_market_start_ts(market) if start_ts is None else start_ts),
        "settlement_source": market.get("settlement_source"),
    }


def _anchor_matches_market(anchor, market):
    if not anchor or anchor.get("price") is None:
        return False
    identity = _anchor_identity(market)
    # Legacy anchors were keyed by market_id only. Preserve them when they do
    # not carry conflicting identity fields, then upgrade them on persistence.
    for field, expected in identity.items():
        actual = anchor.get(field)
        if actual is not None and actual != expected:
            return False
    return True


def _build_anchor(market, price, source, source_timestamp_ms, captured_at_ms, capture_mode):
    anchor = _anchor_identity(market)
    anchor.update({
        "price": float(price),
        "source": source,
        "source_timestamp_ms": float(source_timestamp_ms),
        "captured_at_ms": float(captured_at_ms),
        "capture_mode": capture_mode,
    })
    return anchor


def capture_opening_prices(markets, venue, existing, now_ms=None):
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    anchors = {}
    for market_id, market in markets.items():
        start_ts = _market_start_ts(market)
        start_ms = start_ts * 1000
        open_price = market.get("open_price")
        if open_price is not None:
            anchors[market_id] = _build_anchor(
                market,
                open_price,
                "gamma",
                market.get("open_price_source_timestamp_ms") or start_ms or now_ms,
                now_ms,
                "gamma",
            )
            continue
        source = market.get("settlement_source")
        asset_venue = venue.get("assets", {}).get(market.get("asset"), {})
        source_state = asset_venue.get("sources", {}).get(source, {})
        source_supported = (not source_state) or (source_state.get("supported", True) and source_state.get("status") != "UNSUPPORTED")
        previous = existing.get(market_id)
        if source_supported and _anchor_matches_market(previous, market):
            upgraded = _anchor_identity(market)
            upgraded.update(previous)
            upgraded.setdefault("capture_mode", "legacy_persisted")
            anchors[market_id] = upgraded
            continue
        if not start_ms or now_ms < start_ms:
            continue
        if source not in {"binance", "chainlink"} or not source_supported:
            continue
        samples = list(asset_venue.get(f"{source}_samples", []))
        samples.extend(asset_venue.get(f"{source}_settlement_samples", []))
        interval = market.get("interval")
        eligible = [
            row for row in samples
            if start_ms <= float(row.get("source_timestamp_ms", 0))
            <= start_ms + PRICE_TO_BEAT_CAPTURE_MAX_DELAY_MS
            and row.get("price") is not None
            and (not row.get("timeframe") or row.get("timeframe") == interval)
        ]
        if eligible:
            sample = min(eligible, key=lambda row: float(row["source_timestamp_ms"]))
            capture_mode = (
                "live_boundary"
                if now_ms <= start_ms + PRICE_TO_BEAT_CAPTURE_MAX_DELAY_MS
                else "restart_backfill"
            )
            anchors[market_id] = _build_anchor(
                market,
                sample["price"],
                source,
                sample["source_timestamp_ms"],
                now_ms,
                capture_mode,
            )
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
        "model_sample_span_seconds": samples * 60,
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


def _up_probability_model(asset, price_to_beat, seconds_to_close, book_imbalance=None):
    reference = asset.get("consensus_price")
    volatility = asset.get("volatility_per_sqrt_second")
    samples = int(asset.get("model_sample_count", 0))
    sample_span = float(asset.get("model_sample_span_seconds") or 0)
    minimum_span = float(os.getenv("MODEL_MIN_SAMPLE_SPAN_SECONDS", "60"))
    diagnostics = {
        "model_sample_span_seconds": sample_span,
        "minimum_model_sample_span_seconds": minimum_span,
    }
    if (not reference or not price_to_beat or not volatility or samples < 20 or
            sample_span < minimum_span or seconds_to_close <= 0):
        return None, diagnostics
    scale = float(volatility) * math.sqrt(seconds_to_close)
    if scale <= 0:
        return None, diagnostics
    momentum = asset.get("momentum_bps_30s")
    if momentum is None or book_imbalance is None:
        return None, diagnostics
    log_distance = math.log(float(reference) / float(price_to_beat))
    standardized_distance = log_distance / scale
    momentum_z = float(momentum) * float(os.getenv("MODEL_MOMENTUM_Z_PER_BPS", "0.002"))
    imbalance_z = float(book_imbalance) * float(os.getenv("MODEL_IMBALANCE_Z", "0.25"))
    final_z = standardized_distance + momentum_z + imbalance_z
    probability = min(.999, max(.001, .5 * (1 + math.erf(final_z / math.sqrt(2)))))
    return probability, {
        **diagnostics,
        "volatility_per_sqrt_second": float(volatility),
        "expected_move_log_std": scale,
        "reference_log_distance": log_distance,
        "up_standardized_distance": standardized_distance,
        "up_momentum_z": momentum_z,
        "up_imbalance_z": imbalance_z,
        "up_final_model_z": final_z,
        "paired_book_imbalance": float(book_imbalance),
    }


def _up_probability(asset, price_to_beat, seconds_to_close, book_imbalance=None):
    return _up_probability_model(asset, price_to_beat, seconds_to_close, book_imbalance)[0]


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
    minimum_model_span = float(os.getenv("MODEL_MIN_SAMPLE_SPAN_SECONDS", "60"))
    if (not asset.get("volatility_per_sqrt_second") or
            int(asset.get("model_sample_count", 0)) < 20 or
            float(asset.get("model_sample_span_seconds") or 0) < minimum_model_span):
        historical = (historical_models or {}).get(market.get("asset"))
        if historical:
            model_asset = dict(asset, **historical)
            model_source = "binance_historical_1m"
    model_asset = dict(
        model_asset,
        consensus_price=reference.consensus_price,
        fast_price=reference.fast_price,
        settlement_reference=reference.settlement_reference,
    )
    raw_anchor = (opening_prices or {}).get(market.get("market_id"), {})
    anchor = raw_anchor if _anchor_matches_market(raw_anchor, market) else {}
    price_to_beat = market.get("open_price")
    price_to_beat_source = "gamma" if price_to_beat is not None else None
    price_to_beat_capture_mode = "gamma" if price_to_beat is not None else None
    price_to_beat_source_timestamp_ms = (
        market.get("open_price_source_timestamp_ms")
        or (_market_start_ts(market) * 1000 if price_to_beat is not None else None)
    )
    if price_to_beat is None and anchor.get("price") is not None:
        price_to_beat = float(anchor["price"])
        price_to_beat_source = f"{anchor.get('source')}_start_anchor"
        price_to_beat_capture_mode = anchor.get("capture_mode")
        price_to_beat_source_timestamp_ms = anchor.get("source_timestamp_ms")
    up_imbalance = event.get("up_book_imbalance")
    down_imbalance = event.get("down_book_imbalance")
    paired_imbalance = None
    if up_imbalance is not None and down_imbalance is not None:
        paired_imbalance = (float(up_imbalance) - float(down_imbalance)) / 2
    up_probability, model_diagnostics = _up_probability_model(
        model_asset, price_to_beat, seconds_to_close, paired_imbalance,
    )
    probability_block_reason = None
    if up_probability is None:
        if price_to_beat is None:
            start_ts = _market_start_ts(market)
            if not start_ts:
                probability_block_reason = "price_to_beat_start_time_unavailable"
            else:
                probability_block_reason = (
                    "price_to_beat_capture_missed"
                    if now * 1000 > start_ts * 1000 + PRICE_TO_BEAT_CAPTURE_MAX_DELAY_MS
                    else "price_to_beat_pending"
                )
        elif reference.consensus_price is None:
            probability_block_reason = "consensus_price_unavailable"
        elif not model_asset.get("volatility_per_sqrt_second"):
            probability_block_reason = "volatility_unavailable"
        elif int(model_asset.get("model_sample_count", 0)) < 20:
            probability_block_reason = "insufficient_model_samples"
        elif float(model_asset.get("model_sample_span_seconds") or 0) < minimum_model_span:
            probability_block_reason = "model_sample_span_insufficient"
        elif model_asset.get("momentum_bps_30s") is None:
            probability_block_reason = "momentum_unavailable"
        elif paired_imbalance is None:
            probability_block_reason = "order_book_imbalance_unavailable"
        else:
            probability_block_reason = "probability_model_unavailable"
    size = max(float(event.get("size", 0)), 1e-9)
    rows = []
    _, config_hash = strategy_config()
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
            book_age_ms=float(event.get("book_age_ms", event.get("source_age_ms", 1e9))),
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
            settlement_source_verified=any(
                quote.source == settlement_source
                and quote.status == "FRESH"
                and quote.price is not None
                for quote in reference.sources
            ),
            probability_block_reason=probability_block_reason,
            settlement_source=settlement_source or "",
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
            audit.update(model_diagnostics)
            audit["input_quality_score"] = confidence
            audit["confidence_type"] = "input_quality_not_historical_accuracy"
            audit["price_to_beat_source"] = price_to_beat_source
            audit["price_to_beat_capture_mode"] = price_to_beat_capture_mode
            audit["price_to_beat_source_timestamp_ms"] = price_to_beat_source_timestamp_ms
            audit["target_size"] = size
            audit["window"] = market.get("window", "current")
            audit["config_version"] = STRATEGY_CONFIG_VERSION
            audit["config_hash"] = config_hash
            rows.append(audit)
    return rows


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _should_emit_audit(row, emission_state):
    key = "|".join(str(row.get(field, "")) for field in (
        "market_id", "strategy", "outcome"
    ))
    fingerprint = "|".join(str(row.get(field, "")) for field in (
        "decision", "reason", "config_hash"
    ))
    timestamp = float(row.get("ts", time.time()))
    previous = emission_state.get(key, {})
    heartbeat = float(os.getenv(
        "STRATEGY_ACCEPT_AUDIT_HEARTBEAT_SECONDS" if row.get("decision") == "ACCEPT"
        else "STRATEGY_REJECT_AUDIT_HEARTBEAT_SECONDS",
        "5" if row.get("decision") == "ACCEPT" else "60",
    ))
    if previous.get("fingerprint") == fingerprint and timestamp - float(
        previous.get("last_emitted_ts", 0)
    ) < heartbeat:
        return False
    emission_state[key] = {"fingerprint": fingerprint, "last_emitted_ts": timestamp}
    return True


def process_once(audit_path, market_path, venue_path, output_path, state_path,
                 historical_models=None):
    state = _load(state_path, {"offset": 0, "processed": []})
    processed = set(state.get("processed", []))
    emission_state = state.get("emission_state", {})
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
                if not _should_emit_audit(row, emission_state):
                    continue
                target.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
                emitted += 1
            processed.add(event_id)
        state["offset"] = source.tell()
    state["processed"] = list(processed)[-20000:]
    state["emission_state"] = dict(sorted(
        emission_state.items(), key=lambda item: float(item[1].get("last_emitted_ts", 0))
    )[-5000:])
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
