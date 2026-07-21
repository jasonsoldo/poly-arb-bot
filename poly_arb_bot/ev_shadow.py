import json
import hashlib
import math
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .ev_strategies import (
    DirectionalInput,
    decision_audit,
    directional_windows,
    evaluate_directional,
    evaluate_lottery,
)
from .reference_layer import ReferenceState, reference_source_maximum_age_ms, reference_state_for_asset


BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "XRP": "XRPUSDT", "BNB": "BNBUSDT", "DOGE": "DOGEUSDT",
}
PRICE_TO_BEAT_CAPTURE_MAX_DELAY_MS = 10_000
INTERVAL_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14_400}
STRATEGY_CONFIG_VERSION = "shadow-buy-rules-v9"


def strategy_env_enabled(name, default="0"):
    """Return True when a strategy enable env flag is truthy.

    Directional/lottery probability strategies default to disabled so the
    runtime surface stays focused on risk-free paired-lock arbitrage.
    """
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def directional_ev_enabled():
    return strategy_env_enabled("DIRECTIONAL_EV_ENABLE")


def lottery_ev_enabled():
    return strategy_env_enabled("LOTTERY_EV_ENABLE")


def strategy_config(strategy=None):
    values = {
        "directional_min_net_ev": os.getenv("DIRECTIONAL_MIN_NET_EV", "0.015"),
        "directional_min_probability": os.getenv("DIRECTIONAL_MIN_PROBABILITY", "0.90"),
        "directional_enforce_time_window": os.getenv("DIRECTIONAL_ENFORCE_TIME_WINDOW", "0"),
        "directional_window_5m_min": os.getenv("DIRECTIONAL_WINDOW_5M_MIN", "5"),
        "directional_window_5m_max": os.getenv("DIRECTIONAL_WINDOW_5M_MAX", "15"),
        "directional_window_15m_min": os.getenv("DIRECTIONAL_WINDOW_15M_MIN", "5"),
        "directional_window_15m_max": os.getenv("DIRECTIONAL_WINDOW_15M_MAX", "20"),
        "directional_window_1h_min": os.getenv("DIRECTIONAL_WINDOW_1H_MIN", "8"),
        "directional_window_1h_max": os.getenv("DIRECTIONAL_WINDOW_1H_MAX", "30"),
        "directional_window_4h_min": os.getenv("DIRECTIONAL_WINDOW_4H_MIN", "10"),
        "directional_window_4h_max": os.getenv("DIRECTIONAL_WINDOW_4H_MAX", "45"),
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
        "coinbase_reference_max_age_ms": os.getenv("COINBASE_REFERENCE_MAX_AGE_MS", "10000"),
        "maximum_book_age_ms": os.getenv("CLOB_MAX_BOOK_AGE_MS", "750"),
        "maximum_clock_skew_ms": os.getenv("MAX_CLOCK_SKEW_MS", "250"),
        "momentum_z_per_bps": os.getenv("MODEL_MOMENTUM_Z_PER_BPS", "0.002"),
        "imbalance_z": os.getenv("MODEL_IMBALANCE_Z", "0.25"),
        "lottery_distance_weight": os.getenv("LOTTERY_DISTANCE_WEIGHT", "1.0"),
        "lottery_momentum_z_per_bps": os.getenv("LOTTERY_MOMENTUM_Z_PER_BPS", "0.001"),
        "lottery_imbalance_z": os.getenv("LOTTERY_IMBALANCE_Z", "0.10"),
        "lottery_market_blend": os.getenv("LOTTERY_MARKET_BLEND", "0.50"),
        "minimum_model_sample_span_seconds": os.getenv("MODEL_MIN_SAMPLE_SPAN_SECONDS", "60"),
        "shadow_profit_exit_buffer_per_share": os.getenv(
            "SHADOW_PROFIT_EXIT_BUFFER_PER_SHARE", "0.001"
        ),
        "terminal_hedge_max_reversal_loss": os.getenv("TERMINAL_HEDGE_MAX_REVERSAL_LOSS", "1.0"),
        "terminal_hedge_min_expected_pnl": os.getenv("TERMINAL_HEDGE_MIN_EXPECTED_PNL", "0.05"),
        "terminal_hedge_max_size_ratio": os.getenv("TERMINAL_HEDGE_MAX_SIZE_RATIO", "1.0"),
        "probability_reference": "settlement_reference",
    }
    if strategy:
        common_keys = {
            "coinbase_reference_max_age_ms", "minimum_liquidity", "maximum_slippage",
            "maximum_reference_age_ms", "maximum_book_age_ms", "maximum_clock_skew_ms",
            "minimum_model_sample_span_seconds", "probability_reference",
            "shadow_profit_exit_buffer_per_share",
            "terminal_hedge_max_reversal_loss", "terminal_hedge_min_expected_pnl",
            "terminal_hedge_max_size_ratio",
        }
        strategy_keys = {
            "late_window_directional_ev": {
                "directional_min_net_ev", "directional_latency_buffer",
                "directional_settlement_buffer", "directional_min_probability",
                "directional_enforce_time_window",
                "directional_window_5m_min", "directional_window_5m_max",
                "directional_window_15m_min", "directional_window_15m_max",
                "directional_window_1h_min", "directional_window_1h_max",
                "directional_window_4h_min", "directional_window_4h_max",
                "momentum_z_per_bps", "imbalance_z",
            },
            "low_price_lottery_ev": {
                "lottery_min_price", "lottery_max_price", "lottery_min_net_ev",
                "lottery_model_buffer", "lottery_execution_buffer",
                "lottery_distance_weight", "lottery_momentum_z_per_bps",
                "lottery_imbalance_z", "lottery_market_blend",
            },
        }
        allowed = common_keys | strategy_keys.get(strategy, set())
        values = {key: value for key, value in values.items() if key in allowed}
        values["strategy"] = strategy
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
    return reference_state_for_asset(asset, settlement_source, maximum_age_ms, file_age_ms)


def _up_probability_model(asset, price_to_beat, seconds_to_close, book_imbalance=None):
    reference = asset.get("settlement_reference")
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


def _lottery_up_probability_model(asset, price_to_beat, seconds_to_close, book_imbalance=None):
    reference = asset.get("settlement_reference")
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
    momentum_z = float(momentum) * float(os.getenv("LOTTERY_MOMENTUM_Z_PER_BPS", "0.001"))
    imbalance_z = float(book_imbalance) * float(os.getenv("LOTTERY_IMBALANCE_Z", "0.10"))
    final_z = (
        standardized_distance * float(os.getenv("LOTTERY_DISTANCE_WEIGHT", "1.0"))
        + momentum_z + imbalance_z
    )
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


def _lottery_market_blend_probability(raw_probability, market_implied_probability):
    if raw_probability is None or market_implied_probability is None:
        return None
    weight = min(1.0, max(0.0, float(os.getenv("LOTTERY_MARKET_BLEND", "0.50"))))
    return min(.999, max(.001, float(market_implied_probability) +
                         weight * (float(raw_probability) - float(market_implied_probability))))


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
    price_to_beat_source = market.get("open_price_source") if price_to_beat is not None else None
    price_to_beat_capture_mode = market.get("open_price_capture_mode") if price_to_beat is not None else None
    if price_to_beat is not None:
        price_to_beat_source = price_to_beat_source or "gamma"
        price_to_beat_capture_mode = price_to_beat_capture_mode or "gamma_metadata"
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
    directional_up_probability, directional_diagnostics = _up_probability_model(
        model_asset, price_to_beat, seconds_to_close, paired_imbalance,
    )
    lottery_up_probability, lottery_diagnostics = _lottery_up_probability_model(
        model_asset, price_to_beat, seconds_to_close, paired_imbalance,
    )
    probability_block_reason = None
    if directional_up_probability is None or lottery_up_probability is None:
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
        elif reference.settlement_reference is None:
            probability_block_reason = "settlement_reference_unavailable"
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
    for outcome, fill_key, ask_key, fee_key, depth_key, imbalance_key, directional_raw, lottery_raw in (
        ("Up", "up_vwap", "up_best_ask", "up_fee", "up_available_depth", "up_book_imbalance",
         directional_up_probability, lottery_up_probability),
        ("Down", "down_vwap", "down_best_ask", "down_fee", "down_available_depth", "down_book_imbalance",
         None if directional_up_probability is None else 1 - directional_up_probability,
         None if lottery_up_probability is None else 1 - lottery_up_probability),
    ):
        fill = float(event.get(fill_key, 1))
        best_ask = event.get(ask_key)
        slippage = max(0.0, fill - float(best_ask)) if best_ask is not None else float("inf")
        fresh_reference_sources = [
            row for row in reference.sources
            if row.status == "FRESH" and row.message_age_ms is not None and
            (row.market_type == "spot" or row.source == settlement_source)
        ]
        source_ages = [row.message_age_ms for row in fresh_reference_sources]
        reference_age_ms = max(source_ages) if source_ages else None
        reference_age_limit_ms = max(
            (reference_source_maximum_age_ms(row.source, maximum_reference_age_ms)
             for row in fresh_reference_sources),
            default=maximum_reference_age_ms,
        )
        samples = int(model_asset.get("model_sample_count", 0))
        divergence = reference.cross_source_divergence_bps
        confidence = None if directional_up_probability is None else max(0.0, min(1.0,
            min(samples / 120, 1.0) * (1 - min(float(divergence or 0) / 100, 1.0))))
        common = dict(
            market_id=market.get("market_id", ""), condition_id=market.get("condition_id", market.get("market_id", "")),
            asset=market.get("asset", ""), timeframe=market.get("interval", ""), outcome=outcome,
            market_price=float(best_ask) if best_ask is not None else fill,
            expected_fill_price=fill,
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
            maximum_reference_age_ms=reference_age_limit_ms,
            maximum_book_age_ms=float(os.getenv("CLOB_MAX_BOOK_AGE_MS", "750")),
            maximum_clock_skew_ms=float(os.getenv("MAX_CLOCK_SKEW_MS", "250")),
            market_active=bool(market.get("active", True)) and float(market.get("close_ts", 0)) > now,
            market_tradable=bool(market.get("accepting_orders", True)),
            target_depth_ok=float(event.get("up_fill" if outcome == "Up" else "down_fill", 0)) >= size,
            momentum_bps_30s=model_asset.get("momentum_bps_30s"),
            order_book_imbalance=event.get(imbalance_key), confidence=confidence,
            settlement_source_verified=bool(market.get("settlement_verified", True)) and any(
                quote.source == settlement_source
                and quote.status == "FRESH"
                and quote.price is not None
                for quote in reference.sources
            ),
            probability_block_reason=probability_block_reason,
            settlement_source=settlement_source or "",
        )
        strategy_evaluators = []
        if directional_ev_enabled():
            strategy_evaluators.append(("late_window_directional_ev", evaluate_directional))
        if lottery_ev_enabled():
            strategy_evaluators.append(("low_price_lottery_ev", evaluate_lottery))
        for strategy, evaluator in strategy_evaluators:
            is_lottery = strategy == "low_price_lottery_ev"
            raw_probability = lottery_raw if is_lottery else directional_raw
            probability = (
                _lottery_market_blend_probability(raw_probability, common["market_price"])
                if is_lottery else raw_probability
            )
            diagnostics = lottery_diagnostics if is_lottery else directional_diagnostics
            input_row = DirectionalInput(
                strategy=strategy, estimated_probability=probability, **common,
            )
            if strategy == "late_window_directional_ev":
                result = evaluator(
                    input_row,
                    float(os.getenv("DIRECTIONAL_MIN_NET_EV", "0.015")),
                    float(os.getenv("DIRECTIONAL_MIN_PROBABILITY", "0.90")),
                    enforce_time_window=(
                        os.getenv("DIRECTIONAL_ENFORCE_TIME_WINDOW", "0").strip().lower()
                        not in {"0", "false", "no", "off"}
                    ),
                )
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
            audit["probability_model_id"] = (
                "lottery_market_blend_v1" if is_lottery else "directional_normal_cdf_v1"
            )
            audit["raw_estimated_probability"] = raw_probability
            audit["model_type"] = (
                "configured_lottery_market_blend_shadow"
                if is_lottery else "configured_distributional_shadow"
            )
            audit["model_sample_count"] = int(model_asset.get("model_sample_count", 0))
            audit["model_source"] = model_source if probability is not None else None
            audit.update(diagnostics)
            audit["input_quality_score"] = confidence
            audit["confidence_type"] = "input_quality_not_historical_accuracy"
            audit["price_to_beat_source"] = price_to_beat_source
            audit["price_to_beat_capture_mode"] = price_to_beat_capture_mode
            audit["price_to_beat_source_timestamp_ms"] = price_to_beat_source_timestamp_ms
            audit["target_size"] = size
            audit["window"] = market.get("window", "current")
            audit["config_version"] = STRATEGY_CONFIG_VERSION
            audit["config_hash"] = strategy_config(strategy)[1]
            rows.append(audit)
    combined = _terminal_hedge_audit(rows, event, market, size)
    if combined:
        rows.append(combined)
    return rows


def _terminal_hedge_audit(rows, event, market, size):
    directional = [row for row in rows if row["strategy"] == "late_window_directional_ev"]
    if not directional:
        return None
    seconds_to_close = int(directional[0]["seconds_to_close"])
    window = directional_windows().get(market.get("interval"))
    if not window or not window[0] <= seconds_to_close <= window[1]:
        return None
    accepted_directional = [row for row in directional if row["decision"] == "ACCEPT"]
    main = max(
        accepted_directional or directional,
        key=lambda row: (
            row.get("net_ev") if row.get("net_ev") is not None else float("-inf"),
            row.get("estimated_probability") or float("-inf"),
        ),
    )
    hedge_outcome = "Down" if main["outcome"] == "Up" else "Up"
    hedge = next(
        (row for row in rows
         if row["strategy"] == "low_price_lottery_ev" and row["outcome"] == hedge_outcome),
        None,
    )
    audit = {
        "ts": event.get("ts"), "timestamp": event.get("ts"),
        "event_id": f'{event.get("event_id", market.get("market_id"))}:terminal_hedge',
        "event_type": "shadow_hedge_eval",
        "strategy": "late_window_directional_ev",
        "hedge_strategy": "low_price_lottery_ev",
        "execution_mode": "terminal_hedged",
        "market_id": market.get("market_id"), "condition_id": market.get("condition_id"),
        "asset": market.get("asset"), "timeframe": market.get("interval"),
        "window": market.get("window", "current"),
        "generation": event.get("subscription_generation", event.get("generation")),
        "session": event.get("ws_session_id", event.get("session")),
        "evaluation_sequence": event.get("evaluation_sequence"),
        "main_outcome": main["outcome"], "hedge_outcome": hedge_outcome,
        "main_size": size, "hedge_size": None,
        "main_expected_fill_price": main.get("expected_fill_price"),
        "hedge_expected_fill_price": hedge.get("expected_fill_price") if hedge else None,
        "main_cost": None, "hedge_cost": None, "total_cost": None,
        "main_win_pnl": None, "reversal_pnl": None,
        "expected_portfolio_pnl": None, "worst_case_pnl": None,
        "estimated_probability": main.get("estimated_probability"),
        "volatility_per_sqrt_second": main.get("volatility_per_sqrt_second"),
        "model_sample_count": main.get("model_sample_count"),
        "model_sample_span_seconds": main.get("model_sample_span_seconds"),
        "settlement_reference": main.get("settlement_reference"),
        "price_to_beat": main.get("price_to_beat"),
        "reference_quorum_met": main.get("reference_quorum_met"),
        "seconds_to_close": seconds_to_close,
        "decision": "REJECT", "reason": main["reason"],
        "target_size": size, "config_version": "terminal-hedge-v1",
        "config_hash": strategy_config()[1],
        "real_order_submissions": 0, "real_orders": 0, "real_fills": 0,
    }
    if not accepted_directional:
        return audit
    if not hedge:
        audit["reason"] = "hedge_quote_unavailable"
        return audit
    allowed_hedge_rejections = {"net_ev_below_threshold"}
    blockers = set(hedge.get("blocking_reasons", ())) - allowed_hedge_rejections
    if blockers:
        audit["reason"] = next(iter(blockers))
        return audit
    if hedge["expected_fill_price"] > float(os.getenv("LOTTERY_MAX_PRICE", "0.05")):
        audit["reason"] = "hedge_price_above_limit"
        return audit
    main_unit_cost = (
        main["expected_fill_price"] + main["fees"] + main["slippage"]
        + main["latency_risk_buffer"] + main["settlement_risk_buffer"]
    )
    hedge_unit_cost = (
        hedge["expected_fill_price"] + hedge["fees"] + hedge["slippage"]
        + hedge["model_uncertainty_buffer"] + hedge["execution_risk_buffer"]
    )
    if hedge_unit_cost >= 1:
        audit["reason"] = "hedge_unit_cost_invalid"
        return audit
    main_cost = size * main_unit_cost
    max_reversal_loss = float(os.getenv("TERMINAL_HEDGE_MAX_REVERSAL_LOSS", "1.0"))
    hedge_size = max(0.0, (main_cost - max_reversal_loss) / (1 - hedge_unit_cost))
    if hedge_size <= 0:
        audit.update(main_cost=main_cost, reason="hedge_not_required")
        return audit
    if hedge_size > size * float(os.getenv("TERMINAL_HEDGE_MAX_SIZE_RATIO", "1.0")):
        audit.update(main_cost=main_cost, hedge_size=hedge_size, reason="hedge_size_above_limit")
        return audit
    hedge_cost = hedge_size * hedge_unit_cost
    total_cost = main_cost + hedge_cost
    main_win_pnl = size - total_cost
    reversal_pnl = hedge_size - total_cost
    probability = float(main["estimated_probability"])
    expected_pnl = probability * main_win_pnl + (1 - probability) * reversal_pnl
    audit.update(
        hedge_size=hedge_size, main_cost=main_cost, hedge_cost=hedge_cost,
        total_cost=total_cost, main_win_pnl=main_win_pnl,
        reversal_pnl=reversal_pnl, expected_portfolio_pnl=expected_pnl,
        worst_case_pnl=min(main_win_pnl, reversal_pnl),
    )
    if main_win_pnl <= 0:
        audit["reason"] = "main_win_pnl_not_positive"
    elif reversal_pnl < -max_reversal_loss - 1e-9:
        audit["reason"] = "reversal_loss_above_limit"
    elif expected_pnl < float(os.getenv("TERMINAL_HEDGE_MIN_EXPECTED_PNL", "0.05")):
        audit["reason"] = "portfolio_ev_below_threshold"
    else:
        audit.update(
            event_type="shadow_hedged_opportunity",
            decision="ACCEPT",
            reason="terminal_hedged_opportunity",
        )
    return audit


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_state(path, state):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary, path)


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
    stat = audit_path.stat()
    identity = f"{stat.st_dev}:{stat.st_ino}"
    previous_identity = state.get("file_identity")
    changed = opening_prices != state.get("opening_prices", {})
    if (previous_identity not in {None, identity} or
            stat.st_size < state.get("offset", 0)):
        state["offset"] = 0
        changed = True
    if previous_identity != identity and (previous_identity is not None or stat.st_size > 0):
        state["file_identity"] = identity
        changed = True
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
        offset = source.tell()
        if offset != state.get("offset"):
            state["offset"] = offset
            changed = True
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


def _num(row, key, default):
    value = row.get(key)
    return float(default) if value is None else float(value)


def _verify_cpp_strategy_row(row):
    reference = ReferenceState(
        [], row.get("fast_price"), row.get("consensus_price"),
        row.get("settlement_reference"),
        int(_num(row, "fresh_exchange_source_count", 0)),
        int(_num(row, "fresh_usd_spot_source_count", 0)),
        row.get("cross_source_divergence_bps"),
        bool(row.get("reference_quorum_met")),
        row.get("reference_state", "REFERENCE_BLOCKED"),
        row.get("reference_block_reason"),
    )
    value = DirectionalInput(
        strategy=row["strategy"], market_id=row.get("market_id", ""),
        condition_id=row.get("condition_id", row.get("market_id", "")),
        asset=row.get("asset", ""), timeframe=row.get("timeframe", ""),
        outcome=row.get("outcome", ""), market_price=_num(row, "market_price", 0),
        expected_fill_price=_num(row, "expected_fill_price", 0),
        estimated_probability=row.get("estimated_probability"),
        seconds_to_close=int(_num(row, "seconds_to_close", 0)),
        price_to_beat=row.get("price_to_beat"), reference=reference,
        fee_per_share=_num(row, "fees", 0),
        slippage_per_share=_num(row, "slippage", 0),
        latency_risk_buffer=_num(row, "latency_risk_buffer", 0),
        settlement_risk_buffer=_num(row, "settlement_risk_buffer", 0),
        model_uncertainty_buffer=_num(row, "model_uncertainty_buffer", 0),
        execution_risk_buffer=_num(row, "execution_risk_buffer", 0),
        liquidity=_num(row, "liquidity", 0),
        book_age_ms=_num(row, "book_age_ms", 1e9),
        reference_age_ms=row.get("reference_age_ms"), clock_skew_ms=row.get("clock_skew_ms"),
        minimum_liquidity=_num(row, "minimum_liquidity", 20),
        maximum_slippage=_num(row, "maximum_slippage", .01),
        maximum_reference_age_ms=_num(row, "maximum_reference_age_ms", 3000),
        maximum_book_age_ms=_num(row, "maximum_book_age_ms", 750),
        maximum_clock_skew_ms=_num(row, "maximum_clock_skew_ms", 250),
        market_active=bool(row.get("market_active")),
        market_tradable=bool(row.get("market_tradable")),
        target_depth_ok=bool(row.get("target_depth_ok")),
        momentum_bps_30s=row.get("momentum_bps_30s"),
        order_book_imbalance=row.get("order_book_imbalance"),
        confidence=row.get("confidence"),
        settlement_source_verified=bool(row.get("settlement_source_verified")),
        probability_block_reason=row.get("probability_block_reason"),
        settlement_source=row.get("settlement_source", ""),
    )
    if row["strategy"] == "late_window_directional_ev":
        decision = evaluate_directional(
            value,
            float(os.getenv("DIRECTIONAL_MIN_NET_EV", "0.015")),
            float(os.getenv("DIRECTIONAL_MIN_PROBABILITY", "0.90")),
            enforce_time_window=(
                os.getenv("DIRECTIONAL_ENFORCE_TIME_WINDOW", "0").strip().lower()
                not in {"0", "false", "no", "off"}
            ),
        )
    else:
        decision = evaluate_lottery(
            value, float(os.getenv("LOTTERY_MIN_PRICE", "0.01")),
            float(os.getenv("LOTTERY_MAX_PRICE", "0.05")),
            float(os.getenv("LOTTERY_MIN_NET_EV", "0.015")),
        )
    model_asset = {
        "settlement_reference": row.get("settlement_reference"),
        "volatility_per_sqrt_second": row.get("volatility_per_sqrt_second"),
        "model_sample_count": row.get("model_sample_count", 0),
        "model_sample_span_seconds": row.get("model_sample_span_seconds", 0),
        "momentum_bps_30s": row.get("momentum_bps_30s"),
    }
    is_lottery = row["strategy"] == "low_price_lottery_ev"
    model = _lottery_up_probability_model if is_lottery else _up_probability_model
    up_probability, _ = model(
        model_asset, row.get("price_to_beat"), row.get("seconds_to_close", 0),
        row.get("paired_book_imbalance"),
    )
    raw_probability = up_probability if row.get("outcome") == "Up" else (
        None if up_probability is None else 1 - up_probability
    )
    expected_probability = (
        _lottery_market_blend_probability(raw_probability, row.get("market_implied_probability"))
        if is_lottery else raw_probability
    )
    independent_model_audit = (
        row.get("config_version") == STRATEGY_CONFIG_VERSION
        or row.get("probability_model_id") is not None
    )
    _, expected_hash = strategy_config(row["strategy"] if independent_model_audit else None)
    expected = {
        "decision": decision.decision, "reason": decision.reason,
        "gross_edge": decision.gross_edge, "net_ev": decision.net_ev,
        "config_hash": expected_hash,
    }
    if row.get("probability_model_id") is not None:
        expected["probability_model_id"] = (
            "lottery_market_blend_v1" if is_lottery else "directional_normal_cdf_v1"
        )
    if all(row.get(key) is not None for key in (
        "settlement_reference", "volatility_per_sqrt_second", "model_sample_count",
        "model_sample_span_seconds", "momentum_bps_30s", "paired_book_imbalance",
    )):
        expected["estimated_probability"] = expected_probability
        if "raw_estimated_probability" in row:
            expected["raw_estimated_probability"] = raw_probability
    mismatches = {}
    for key, expected_value in expected.items():
        actual = row.get(key)
        if isinstance(expected_value, float):
            if actual is None or not math.isclose(float(actual), expected_value, rel_tol=0, abs_tol=1e-12):
                mismatches[key] = {"cpp": actual, "python": expected_value}
        elif actual != expected_value:
            mismatches[key] = {"cpp": actual, "python": expected_value}
    return mismatches


def process_verification_once(source_path, output_path, state_path):
    source_path, output_path, state_path = map(Path, (source_path, output_path, state_path))
    state = _load(state_path, {"offset": 0})
    if not source_path.exists():
        return 0
    stat = source_path.stat()
    identity = f"{stat.st_dev}:{stat.st_ino}"
    previous_identity = state.get("file_identity")
    changed = False
    if (previous_identity not in {None, identity} or
            stat.st_size < int(state.get("offset", 0))):
        state["offset"] = 0
        changed = True
    if previous_identity != identity and (previous_identity is not None or stat.st_size > 0):
        state["file_identity"] = identity
        changed = True
    mismatches = []
    with source_path.open(encoding="utf-8") as source:
        source.seek(int(state.get("offset", 0)))
        while line := source.readline():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("event_type") != "shadow_eval" or row.get("strategy") not in {
                "late_window_directional_ev", "low_price_lottery_ev",
            }:
                continue
            differences = _verify_cpp_strategy_row(row)
            if differences:
                mismatches.append({
                    "ts": time.time(), "event_type": "strategy_parity_mismatch",
                    "source_event_id": row.get("event_id"),
                    "strategy": row.get("strategy"), "market_id": row.get("market_id"),
                    "outcome": row.get("outcome"), "mismatches": differences,
                })
        offset = source.tell()
        if offset != state.get("offset"):
            state["offset"] = offset
            changed = True
    if mismatches:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("a", encoding="utf-8") as output:
            for row in mismatches:
                output.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
    if changed:
        _write_state(state_path, state)
    return len(mismatches)


def main():
    if os.getenv("EV_SHADOW_MODE") == "verify":
        source = sys.argv[1] if len(sys.argv) > 1 else "logs/strategy-audit.jsonl"
        output = sys.argv[2] if len(sys.argv) > 2 else "logs/strategy-parity.jsonl"
        state = sys.argv[3] if len(sys.argv) > 3 else "state/ev-shadow-verify.json"
        while True:
            process_verification_once(source, output, state)
            time.sleep(.5)
    historical_models = load_historical_models()
    while True:
        process_once("logs/shadow-audit.jsonl", "data/live_markets.json", "data/venue-status.json",
                     "logs/strategy-audit.jsonl", "state/ev-shadow.json", historical_models)
        time.sleep(.5)


if __name__ == "__main__":
    main()
