import os
from dataclasses import dataclass, replace
from statistics import median
from typing import List, Optional


VALID_STATUSES = {"FRESH", "STALE", "DISCONNECTED", "NOT_RECEIVED", "UNSUPPORTED", "OUTLIER"}
COINBASE_REFERENCE_MAX_AGE_MS = 10_000
# Kraken 现货 ticker 为成交触发型，低流动性 USD 对推送间隔达数十秒
# （docs/diagnosis-reference-stale.md §1），与 C++ 引擎的 KRAKEN_REFERENCE_FRESHNESS_MS 保持一致。
KRAKEN_REFERENCE_MAX_AGE_MS = 60_000


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


def reference_source_maximum_age_ms(source, default_maximum_age_ms):
    if source == "coinbase":
        return float(os.getenv("COINBASE_REFERENCE_MAX_AGE_MS", str(COINBASE_REFERENCE_MAX_AGE_MS)))
    if source == "kraken":
        return float(os.getenv("KRAKEN_REFERENCE_MAX_AGE_MS", str(KRAKEN_REFERENCE_MAX_AGE_MS)))
    return float(default_maximum_age_ms)


def reference_state_for_asset(asset, settlement_source, maximum_age_ms, file_age_ms=0):
    sources = []
    for name, row in asset.get("sources", {}).items():
        age = row.get("message_age_ms")
        effective_age = None if age is None else max(0.0, float(age) + file_age_ms)
        status = row.get("status", "NOT_RECEIVED")
        # venue-status 中引擎输出的每源阈值优先；旧文件缺该字段时回退到本地阈值表。
        emitted_limit = row.get("freshness_limit_ms")
        source_maximum_age_ms = (
            float(emitted_limit) if emitted_limit
            else reference_source_maximum_age_ms(name, maximum_age_ms)
        )
        if status == "FRESH" and (effective_age is None or effective_age > source_maximum_age_ms):
            status = "STALE"
        sources.append(ReferenceQuote(
            name, "", row.get("symbol", ""), row.get("market_type", ""),
            row.get("quote_currency", ""), row.get("price"), row.get("bid"), row.get("ask"),
            row.get("source_timestamp"), row.get("received_at"), effective_age, status,
        ))
    selected = next((row for row in sources if row.source == settlement_source), None)
    verified = selected is not None and selected.status == "FRESH" and selected.price is not None
    return aggregate_reference(sources, selected.price if selected else None, verified)
