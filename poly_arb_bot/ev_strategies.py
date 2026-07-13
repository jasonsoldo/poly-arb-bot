from dataclasses import dataclass
from typing import Optional

from .reference_layer import ReferenceState


DIRECTIONAL_WINDOWS = {"5m": (15, 90), "15m": (20, 180), "1h": (30, 300), "4h": (60, 600)}


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
    settlement_source_verified: bool


@dataclass(frozen=True)
class EvDecision:
    strategy: str
    gross_edge: Optional[float]
    net_ev: Optional[float]
    decision: str
    reason: str
    completed: bool = False
    real_order_submissions: int = 0


def _common_rejection(row):
    if row.estimated_probability is None:
        return "probability_model_unavailable"
    if row.price_to_beat is None:
        return "price_to_beat_missing"
    if not row.settlement_source_verified:
        return "settlement_reference_unverified"
    if not row.reference.reference_quorum_met:
        return row.reference.reference_block_reason or "insufficient_reference_sources"
    if row.book_age_ms > 750:
        return "clob_book_stale"
    if row.liquidity <= 0:
        return "insufficient_liquidity"
    return None


def evaluate_directional(row, min_net_ev=.015):
    reason = _common_rejection(row)
    window = DIRECTIONAL_WINDOWS.get(row.timeframe)
    if reason is None and (not window or not window[0] <= row.seconds_to_close <= window[1]):
        reason = "outside_time_window"
    gross = row.estimated_probability - row.expected_fill_price if row.estimated_probability is not None else None
    net = gross - row.fee_per_share - row.slippage_per_share - row.latency_risk_buffer - row.settlement_risk_buffer if gross is not None else None
    if reason is None and net is not None and net < min_net_ev:
        reason = "net_ev_below_threshold"
    return EvDecision("late_window_directional_ev", gross, net,
                      "REJECT" if reason else "ACCEPT", reason or "positive_net_ev")


def evaluate_lottery(row, min_price=.01, max_price=.05, min_net_ev=.015):
    reason = _common_rejection(row)
    if reason is None and not min_price <= row.expected_fill_price <= max_price:
        reason = "entry_price_above_limit" if row.expected_fill_price > max_price else "entry_price_below_limit"
    gross = row.estimated_probability - row.expected_fill_price if row.estimated_probability is not None else None
    net = gross - row.fee_per_share - row.slippage_per_share - row.model_uncertainty_buffer - row.execution_risk_buffer if gross is not None else None
    if reason is None and net is not None and net < min_net_ev:
        reason = "net_ev_below_threshold"
    return EvDecision("low_price_lottery_ev", gross, net,
                      "REJECT" if reason else "ACCEPT", reason or "positive_net_ev")


def decision_audit(row, result, event_id, generation, session, evaluation_sequence, timestamp):
    reference_price = row.reference.consensus_price
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
        "reference_price": reference_price, "price_to_beat": row.price_to_beat,
        "distance_to_price_to_beat": reference_price - row.price_to_beat if reference_price is not None and row.price_to_beat is not None else None,
        "seconds_to_close": row.seconds_to_close, "book_age_ms": row.book_age_ms,
        "decision": result.decision, "reason": result.reason,
        "real_order_submissions": 0, "real_orders": 0,
    }
