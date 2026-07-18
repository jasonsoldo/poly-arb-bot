import os
from dataclasses import dataclass
from typing import Optional

from .reference_layer import ReferenceState


DEFAULT_DIRECTIONAL_WINDOWS = {
    "5m": (5, 15), "15m": (5, 20), "1h": (8, 30), "4h": (10, 45),
}


def directional_windows():
    return {
        timeframe: (
            int(os.getenv(f"DIRECTIONAL_WINDOW_{timeframe.upper()}_MIN", minimum)),
            int(os.getenv(f"DIRECTIONAL_WINDOW_{timeframe.upper()}_MAX", maximum)),
        )
        for timeframe, (minimum, maximum) in DEFAULT_DIRECTIONAL_WINDOWS.items()
    }


@dataclass(frozen=True)
class DirectionalInput:
    strategy: str
    market_id: str
    condition_id: str
    asset: str
    timeframe: str
    outcome: str
    market_price: float
    expected_fill_price: float
    estimated_probability: Optional[float]
    seconds_to_close: int
    price_to_beat: Optional[float]
    reference: ReferenceState
    fee_per_share: float
    slippage_per_share: float
    latency_risk_buffer: float
    settlement_risk_buffer: float
    model_uncertainty_buffer: float
    execution_risk_buffer: float
    liquidity: float
    book_age_ms: float
    reference_age_ms: Optional[float]
    clock_skew_ms: Optional[float]
    minimum_liquidity: float
    maximum_slippage: float
    maximum_reference_age_ms: float
    maximum_book_age_ms: float
    maximum_clock_skew_ms: float
    market_active: bool
    market_tradable: bool
    target_depth_ok: bool
    momentum_bps_30s: Optional[float]
    order_book_imbalance: Optional[float]
    confidence: Optional[float]
    settlement_source_verified: bool
    probability_block_reason: Optional[str] = None
    settlement_source: str = ""


@dataclass(frozen=True)
class EvDecision:
    strategy: str
    gross_edge: Optional[float]
    net_ev: Optional[float]
    decision: str
    reason: str
    completed: bool = False
    real_order_submissions: int = 0
    blocking_reasons: tuple[str, ...] = ()


def _append_reason(reasons, reason):
    if reason and reason not in reasons:
        reasons.append(reason)


def _common_rejections(row):
    reasons = []
    if not row.market_active or not row.market_tradable:
        _append_reason(reasons, "market_not_tradable")
    if row.book_age_ms > row.maximum_book_age_ms:
        _append_reason(reasons, "clob_book_stale")
    if row.clock_skew_ms is None:
        _append_reason(reasons, "clock_skew_unavailable")
    elif abs(row.clock_skew_ms) > row.maximum_clock_skew_ms:
        _append_reason(reasons, "clock_skew_exceeded")
    if not row.reference.reference_quorum_met:
        _append_reason(
            reasons,
            row.reference.reference_block_reason or "insufficient_reference_sources",
        )
    if not row.settlement_source_verified:
        _append_reason(reasons, "settlement_reference_unverified")
    if row.reference_age_ms is None or row.reference_age_ms > row.maximum_reference_age_ms:
        _append_reason(reasons, "reference_data_stale")
    if row.price_to_beat is None:
        _append_reason(reasons, row.probability_block_reason or "price_to_beat_missing")
    elif row.estimated_probability is None:
        _append_reason(reasons, row.probability_block_reason or "probability_model_unavailable")
    if row.liquidity < row.minimum_liquidity:
        _append_reason(reasons, "insufficient_liquidity")
    if not row.target_depth_ok:
        _append_reason(reasons, "target_depth_insufficient")
    if row.slippage_per_share > row.maximum_slippage:
        _append_reason(reasons, "slippage_exceeded")
    if row.momentum_bps_30s is None:
        _append_reason(reasons, "momentum_unavailable")
    if row.order_book_imbalance is None:
        _append_reason(reasons, "order_book_imbalance_unavailable")
    return reasons


def evaluate_directional(row, min_net_ev=.015, min_probability=.90, windows=None,
                         enforce_time_window=True):
    reasons = _common_rejections(row)
    window = (windows or directional_windows()).get(row.timeframe)
    if enforce_time_window and (
        not window or not window[0] <= row.seconds_to_close <= window[1]
    ):
        _append_reason(reasons, "outside_time_window")
    if row.estimated_probability is not None and row.estimated_probability < min_probability:
        _append_reason(reasons, "model_confidence_below_threshold")
    gross = row.estimated_probability - row.expected_fill_price if row.estimated_probability is not None else None
    net = gross - row.fee_per_share - row.slippage_per_share - row.latency_risk_buffer - row.settlement_risk_buffer if gross is not None else None
    if net is not None and net < min_net_ev:
        _append_reason(reasons, "net_ev_below_threshold")
    return EvDecision(
        "late_window_directional_ev",
        gross,
        net,
        "REJECT" if reasons else "ACCEPT",
        reasons[0] if reasons else "positive_net_ev",
        blocking_reasons=tuple(reasons),
    )


def evaluate_lottery(row, min_price=.01, max_price=.05, min_net_ev=.015):
    reasons = _common_rejections(row)
    if not min_price <= row.expected_fill_price <= max_price:
        _append_reason(
            reasons,
            "entry_price_above_limit" if row.expected_fill_price > max_price else "entry_price_below_limit",
        )
    gross = row.estimated_probability - row.expected_fill_price if row.estimated_probability is not None else None
    net = gross - row.fee_per_share - row.slippage_per_share - row.model_uncertainty_buffer - row.execution_risk_buffer if gross is not None else None
    if net is not None and net < min_net_ev:
        _append_reason(reasons, "net_ev_below_threshold")
    return EvDecision(
        "low_price_lottery_ev",
        gross,
        net,
        "REJECT" if reasons else "ACCEPT",
        reasons[0] if reasons else "positive_net_ev",
        blocking_reasons=tuple(reasons),
    )


def _reference_source_statuses(row):
    statuses = []
    valid = []
    rejected = []
    for quote in row.reference.sources:
        is_settlement = quote.source == row.settlement_source
        accepted_for_quorum = (
            quote.status == "FRESH"
            and quote.market_type == "spot"
            and quote.price is not None
        )
        valid_settlement = is_settlement and quote.status == "FRESH" and quote.price is not None
        if accepted_for_quorum:
            role = "exchange_quorum"
            rejection_reason = None
        elif is_settlement:
            role = "settlement_reference"
            if valid_settlement:
                rejection_reason = None
            elif quote.status != "FRESH":
                rejection_reason = quote.status.lower()
            elif quote.price is None:
                rejection_reason = "missing_price"
            else:
                rejection_reason = "unverified"
        else:
            role = "excluded"
            if quote.status != "FRESH":
                rejection_reason = quote.status.lower()
            elif quote.price is None:
                rejection_reason = "missing_price"
            elif quote.market_type != "spot":
                rejection_reason = "wrong_market_type"
            else:
                rejection_reason = "excluded"
        if accepted_for_quorum or valid_settlement:
            valid.append(quote.source)
        elif rejection_reason is not None:
            rejected.append(quote.source)
        statuses.append({
            "source": quote.source,
            "symbol": quote.symbol,
            "market_type": quote.market_type,
            "quote_currency": quote.quote_currency,
            "price": quote.price,
            "effective_age_ms": quote.message_age_ms,
            "status": quote.status,
            "role": role,
            "accepted_for_quorum": accepted_for_quorum,
            "rejection_reason": rejection_reason,
        })
    return statuses, valid, rejected


def decision_audit(row, result, event_id, generation, session, evaluation_sequence, timestamp):
    reference_price = row.reference.settlement_reference
    statuses, valid_sources, rejected_sources = _reference_source_statuses(row)
    return {
        "ts": timestamp, "event_id": event_id, "event_type": "shadow_eval", "strategy": result.strategy,
        "market_id": row.market_id, "condition_id": row.condition_id, "asset": row.asset,
        "timeframe": row.timeframe, "window": "current", "generation": generation,
        "session": session, "evaluation_sequence": evaluation_sequence, "timestamp": timestamp,
        "outcome": row.outcome, "market_price": row.market_price,
        "expected_fill_price": row.expected_fill_price,
        "estimated_probability": row.estimated_probability,
        "market_implied_probability": row.market_price, "gross_edge": result.gross_edge,
        "fees": row.fee_per_share, "slippage": row.slippage_per_share,
        "latency_risk_buffer": row.latency_risk_buffer,
        "settlement_risk_buffer": row.settlement_risk_buffer,
        "model_uncertainty_buffer": row.model_uncertainty_buffer,
        "execution_risk_buffer": row.execution_risk_buffer, "net_ev": result.net_ev,
        "fast_price": row.reference.fast_price, "consensus_price": row.reference.consensus_price,
        "settlement_reference": row.reference.settlement_reference,
        "fresh_exchange_source_count": row.reference.fresh_exchange_source_count,
        "fresh_usd_spot_source_count": row.reference.fresh_usd_spot_source_count,
        "cross_source_divergence_bps": row.reference.cross_source_divergence_bps,
        "reference_quorum_met": row.reference.reference_quorum_met,
        "reference_state": row.reference.reference_state,
        "settlement_source": row.settlement_source,
        "settlement_source_verified": row.settlement_source_verified,
        "reference_source_statuses": statuses,
        "valid_reference_sources": valid_sources,
        "rejected_reference_sources": rejected_sources,
        "reference_price": reference_price,
        "probability_reference_source": "settlement_reference",
        "probability_reference_price": reference_price,
        "price_to_beat": row.price_to_beat,
        "distance_to_price_to_beat": reference_price - row.price_to_beat if reference_price is not None and row.price_to_beat is not None else None,
        "seconds_to_close": row.seconds_to_close, "book_age_ms": row.book_age_ms,
        "reference_age_ms": row.reference_age_ms, "clock_skew_ms": row.clock_skew_ms,
        "liquidity": row.liquidity, "minimum_liquidity": row.minimum_liquidity,
        "target_depth_ok": row.target_depth_ok,
        "maximum_slippage": row.maximum_slippage,
        "maximum_reference_age_ms": row.maximum_reference_age_ms,
        "maximum_book_age_ms": row.maximum_book_age_ms,
        "maximum_clock_skew_ms": row.maximum_clock_skew_ms,
        "momentum_bps_30s": row.momentum_bps_30s,
        "order_book_imbalance": row.order_book_imbalance,
        "confidence": row.confidence,
        "decision": result.decision, "reason": result.reason,
        "blocking_reasons": list(result.blocking_reasons),
        "real_order_submissions": 0, "real_orders": 0,
    }
