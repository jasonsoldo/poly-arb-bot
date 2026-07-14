from dataclasses import dataclass, replace
from statistics import median
from typing import List, Optional


VALID_STATUSES = {"FRESH", "STALE", "DISCONNECTED", "NOT_RECEIVED", "UNSUPPORTED", "OUTLIER"}


@dataclass(frozen=True)
class ReferenceQuote:
    source: str
    asset: str
    symbol: str
    market_type: str
    quote_currency: str
    price: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    source_timestamp: Optional[int]
    received_at: Optional[int]
    message_age_ms: Optional[float]
    status: str


@dataclass(frozen=True)
class ReferenceState:
    sources: List[ReferenceQuote]
    fast_price: Optional[float]
    consensus_price: Optional[float]
    settlement_reference: Optional[float]
    fresh_exchange_source_count: int
    fresh_usd_spot_source_count: int
    cross_source_divergence_bps: Optional[float]
    reference_quorum_met: bool
    reference_state: str
    reference_block_reason: Optional[str]


def aggregate_reference(quotes, settlement_reference, settlement_verified,
                        min_sources=2, max_divergence_bps=100):
    rows = list(quotes)
    for row in rows:
        if row.status not in VALID_STATUSES:
            raise ValueError("invalid reference status")
    fresh_spot = [row for row in rows if row.status == "FRESH" and row.market_type == "spot" and row.price is not None]
    usd = [row for row in fresh_spot if row.quote_currency == "USD"]
    usd_median = median([row.price for row in usd]) if usd else None
    normalized = []
    for row in rows:
        if row in usd and usd_median and abs(row.price - usd_median) / usd_median * 10000 > max_divergence_bps:
            normalized.append(replace(row, status="OUTLIER"))
        else:
            normalized.append(row)
    valid = [row for row in normalized if row.status == "FRESH" and row.market_type == "spot" and row.price is not None]
    valid_usd = [row for row in valid if row.quote_currency == "USD"]
    consensus = median([row.price for row in valid_usd]) if valid_usd else None
    fast_candidates = [row for row in valid if row.source in {"binance", "bybit", "okx"}]
    fast = fast_candidates[0].price if fast_candidates else (valid[0].price if valid else None)
    divergence = None
    if valid:
        prices = [row.price for row in valid]
        center = median(prices)
        divergence = (max(prices) - min(prices)) / center * 10000 if center else None
    source_count = len({row.source for row in valid})
    usd_count = len({row.source for row in valid_usd})
    reason = None
    if source_count < min_sources:
        reason = "insufficient_reference_sources"
    elif usd_count < 1:
        reason = "required_usd_spot_source_unavailable"
    elif not settlement_verified or settlement_reference is None:
        reason = "settlement_reference_unavailable"
    elif divergence is not None and divergence > max_divergence_bps:
        reason = "cross_source_divergence_exceeded"
    ready = reason is None
    return ReferenceState(normalized, fast, consensus, settlement_reference, source_count, usd_count,
                          divergence, ready, "REFERENCE_READY" if ready else "REFERENCE_BLOCKED", reason)
