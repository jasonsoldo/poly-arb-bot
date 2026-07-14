import json
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
from .reference_layer import ReferenceQuote, ReferenceState


BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "XRP": "XRPUSDT", "BNB": "BNBUSDT", "DOGE": "DOGEUSDT",
}
PRICE_TO_BEAT_CAPTURE_MAX_DELAY_MS = 10_000


def capture_opening_prices(markets, venue, existing, now_ms=None):
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    anchors = dict(existing)
    for market_id, market in markets.items():
        if market_id in anchors or market.get("open_price") is not None:
            continue
        start_ms = float(market.get("start_ts") or 0) * 1000
        if not start_ms or now_ms < start_ms:
            continue
        samples = venue.get("assets", {}).get(market.get("asset"), {}).get("chainlink_samples", [])
        eligible = [row for row in samples
                    if start_ms <= float(row.get("source_timestamp_ms", 0))
                    <= start_ms + PRICE_TO_BEAT_CAPTURE_MAX_DELAY_MS]
        if eligible:
            sample = min(eligible, key=lambda row: float(row["source_timestamp_ms"]))
            anchors[market_id] = {
                "price": float(sample["price"]),
                "source_timestamp_ms": float(sample["source_timestamp_ms"]),
                "captured_at_ms": now_ms,
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


def _reference_state(asset):
    sources = []
    for name, row in asset.get("sources", {}).items():
        sources.append(ReferenceQuote(
            name, "", row.get("symbol", ""), row.get("market_type", ""),
            row.get("quote_currency", ""), row.get("price"), row.get("bid"), row.get("ask"),
            row.get("source_timestamp"), row.get("received_at"), row.get("message_age_ms"),
            row.get("status", "NOT_RECEIVED"),
        ))
    return ReferenceState(
        sources, asset.get("fast_price"), asset.get("consensus_price"),
        asset.get("settlement_reference"), int(asset.get("fresh_exchange_source_count", 0)),
        int(asset.get("fresh_usd_spot_source_count", 0)), asset.get("cross_source_divergence_bps"),
        bool(asset.get("reference_quorum_met")), asset.get("reference_state", "REFERENCE_BLOCKED"),
        None if asset.get("reference_quorum_met") else asset.get("reference_block_reason", "insufficient_reference_sources"),
    )


def _up_probability(asset, price_to_beat, seconds_to_close):
    reference = asset.get("consensus_price")
    volatility = asset.get("volatility_per_sqrt_second")
    samples = int(asset.get("model_sample_count", 0))
    if not reference or not price_to_beat or not volatility or samples < 20 or seconds_to_close <= 0:
        return None
    scale = float(volatility) * math.sqrt(seconds_to_close)
    if scale <= 0:
        return None
    z = math.log(float(reference) / float(price_to_beat)) / scale
    return min(.999, max(.001, .5 * (1 + math.erf(z / math.sqrt(2)))))


def evaluate_market_event(event, market, venue, now=None, historical_models=None,
                          opening_prices=None):
    now = time.time() if now is None else now
    asset = venue.get("assets", {}).get(market.get("asset"), {})
    reference = _reference_state(asset)
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
        price_to_beat_source = "chainlink_rtds_start_anchor"
    up_probability = _up_probability(model_asset, price_to_beat, seconds_to_close)
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
        else:
            probability_block_reason = "probability_model_unavailable"
    size = max(float(event.get("size", 0)), 1e-9)
    chainlink = asset.get("sources", {}).get("chainlink", {})
    rows = []
    for outcome, fill_key, fee_key, depth_key, probability in (
        ("Up", "up_vwap", "up_fee", "up_fill", up_probability),
        ("Down", "down_vwap", "down_fee", "down_fill", None if up_probability is None else 1 - up_probability),
    ):
        fill = float(event.get(fill_key, 1))
        common = dict(
            market_id=market.get("market_id", ""), condition_id=market.get("market_id", ""),
            asset=market.get("asset", ""), timeframe=market.get("interval", ""), outcome=outcome,
            market_price=fill, expected_fill_price=fill, estimated_probability=probability,
            seconds_to_close=seconds_to_close, price_to_beat=price_to_beat,
            reference=reference, fee_per_share=float(event.get(fee_key, 0)) / size,
            slippage_per_share=0.0,
            latency_risk_buffer=float(os.getenv("DIRECTIONAL_LATENCY_BUFFER", "0.003")),
            settlement_risk_buffer=float(os.getenv("DIRECTIONAL_SETTLEMENT_BUFFER", "0.002")),
            model_uncertainty_buffer=float(os.getenv("LOTTERY_MODEL_BUFFER", "0.01")),
            execution_risk_buffer=float(os.getenv("LOTTERY_EXECUTION_BUFFER", "0.005")),
            liquidity=float(event.get(depth_key, 0)), book_age_ms=float(event.get("source_age_ms", 1e9)),
            settlement_source_verified=chainlink.get("status") == "FRESH",
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
            audit["window"] = market.get("window", "current")
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
