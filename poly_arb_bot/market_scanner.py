import re
import time
from dataclasses import asdict
from typing import Any, Dict, Iterable, List, Optional

from .live_signals import LiveMarketSpec
from .polymarket_data import first_present, parse_jsonish, parse_timestamp_seconds


ASSET_SYMBOLS = {
    "Bitcoin": "BTCUSDT",
    "Ethereum": "ETHUSDT",
    "Solana": "SOLUSDT",
    "XRP": "XRPUSDT",
    "Dogecoin": "DOGEUSDT",
    "BNB": "BNBUSDT",
    "Hyperliquid": "HYPEUSDT",
}

ASSET_SLUGS = {
    "Bitcoin": "btc",
    "Ethereum": "eth",
    "Solana": "sol",
    "XRP": "xrp",
    "Dogecoin": "doge",
    "BNB": "bnb",
    "Hyperliquid": "hype",
}

ASSET_CODES = {
    "Bitcoin": "BTC", "Ethereum": "ETH", "Solana": "SOL", "XRP": "XRP",
    "Dogecoin": "DOGE", "BNB": "BNB", "Hyperliquid": "HYPE",
}

INTERVAL_SECONDS = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
}


class MarketScanner:
    def __init__(self, assets: Dict[str, str] = None):
        self.assets = assets or ASSET_SYMBOLS

    def specs_from_events(self, events: Iterable[Dict[str, Any]]) -> List[LiveMarketSpec]:
        specs = []
        for event in events:
            candidates = event.get("markets") or [event]
            for market in candidates:
                spec = self.spec_from_market(
                    market, event, interval=event.get("_interval"), series_id=event.get("_series_id")
                )
                if spec is not None:
                    specs.append(spec)
        return specs

    def candidate_markets(self, events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        candidates = []
        for event in events:
            for market in event.get("markets") or [event]:
                title = str(first_present(market, ("question", "title", "slug")) or first_present(event, ("title", "slug")) or "")
                if self._asset_from_title(title) is not None and "Up or Down" in title:
                    candidates.append(market)
        return candidates

    def spec_from_market(
        self, market: Dict[str, Any], event: Dict[str, Any] = None,
        interval: str = None, series_id: str = None,
    ) -> Optional[LiveMarketSpec]:
        event = event or {}
        title = str(first_present(market, ("question", "title", "slug")) or first_present(event, ("title", "slug")) or "")
        asset = self._asset_from_title(title)
        if asset is None or "Up or Down" not in title:
            return None

        outcomes = parse_jsonish(first_present(market, ("outcomes", "shortOutcomes")))
        token_ids = self._token_ids(market)
        outcome_tokens = self._token_map(outcomes, token_ids)
        up_token_id = next((value for key, value in outcome_tokens.items() if key.lower() == "up"), None)
        down_token_id = next((value for key, value in outcome_tokens.items() if key.lower() == "down"), None)
        if (not up_token_id or not down_token_id) and len(token_ids) == 2:
            up_token_id, down_token_id = self._binary_token_pair(token_ids)
        if not up_token_id or not down_token_id:
            return None

        condition_id = str(first_present(market, ("conditionId", "condition_id", "id")) or "")
        open_price = self._open_price(market, event)
        start_ts = parse_timestamp_seconds(
            first_present(event, ("startTime", "eventStartTime")) or
            first_present(market, ("eventStartTime", "startTime"))
        )
        if start_ts is None:
            slug = str(first_present(market, ("slug",)) or first_present(event, ("slug",)) or "")
            match = re.search(r"-updown-(?:5m|15m|1h|4h)-(\d+)$", slug)
            start_ts = int(match.group(1)) if match else None
        close_ts = parse_timestamp_seconds(first_present(market, ("endDate", "endDateIso", "end_date", "closeTime")))
        if not condition_id or close_ts is None:
            return None

        return LiveMarketSpec(
            market_id=condition_id,
            title=title,
            asset=ASSET_CODES[asset],
            symbol=self.assets[asset],
            open_price=open_price,
            close_ts=close_ts,
            up_token_id=up_token_id,
            down_token_id=down_token_id,
            start_ts=start_ts,
            interval=interval,
            series_id=series_id,
            fee_rate=self._fee_rate(market),
        )

    @staticmethod
    def _fee_rate(market: Dict[str, Any]) -> Optional[float]:
        schedule = parse_jsonish(market.get("feeSchedule")) or {}
        if isinstance(schedule, dict):
            try:
                value = first_present(schedule, ("rate", "r"))
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                pass
        return None

    def to_payload(self, specs: Iterable[LiveMarketSpec]) -> Dict[str, Any]:
        return {"markets": [asdict(spec) for spec in specs]}

    def _asset_from_title(self, title: str) -> Optional[str]:
        for asset in self.assets:
            if title.startswith(f"{asset} Up or Down"):
                return asset
        return None

    def _token_map(self, outcomes: Any, token_ids: Any) -> Dict[str, str]:
        if not isinstance(outcomes, list) or not isinstance(token_ids, list):
            return {}
        tokens = []
        for token in token_ids:
            if isinstance(token, dict):
                value = first_present(token, ("token_id", "tokenId", "id"))
            else:
                value = token
            normalized = str(value or "").strip().strip('"')
            tokens.append(normalized if normalized.isdigit() else "")
        return {str(outcome): token for outcome, token in zip(outcomes, tokens) if token}

    @staticmethod
    def _binary_token_pair(token_ids: Any):
        values = []
        for token in token_ids:
            if isinstance(token, dict):
                value = first_present(token, ("token_id", "tokenId", "id"))
            else:
                value = token
            normalized = str(value or "").strip().strip('"')
            if normalized.isdigit():
                values.append(normalized)
        return (values[0], values[1]) if len(values) == 2 else (None, None)

    @staticmethod
    def _token_ids(market: Dict[str, Any]):
        for name in ("clobTokenIds", "clobTokenIDs", "tokens"):
            value = parse_jsonish(market.get(name))
            if isinstance(value, list) and len(value) == 2:
                return value
        return []

    def updown_slugs(
        self,
        intervals: Iterable[str],
        now_ts: Optional[int] = None,
        include_previous: bool = False,
        include_next: bool = True,
    ) -> List[str]:
        now_ts = int(now_ts or time.time())
        slugs = []
        for asset, prefix in ASSET_SLUGS.items():
            if asset not in self.assets:
                continue
            for interval in intervals:
                seconds = INTERVAL_SECONDS[interval]
                current_start = now_ts - (now_ts % seconds)
                starts = [current_start]
                if include_previous:
                    starts.append(current_start - seconds)
                if include_next:
                    starts.append(current_start + seconds)
                for start in sorted(set(starts)):
                    slugs.append(f"{prefix}-updown-{interval}-{start}")
        return slugs

    def updown_series_slugs(self, intervals: Iterable[str]):
        rows = []
        for asset, prefix in ASSET_SLUGS.items():
            if asset not in self.assets:
                continue
            for interval in intervals:
                if interval not in INTERVAL_SECONDS:
                    raise ValueError(f"unsupported interval: {interval}")
                rows.append((ASSET_CODES[asset], interval, f"{prefix}-up-or-down-{interval}"))
        return rows

    def _open_price(self, market: Dict[str, Any], event: Dict[str, Any]) -> Optional[float]:
        direct = first_present(
            market,
            (
                "priceToBeat",
                "price_to_beat",
                "openPrice",
                "open_price",
                "startPrice",
                "start_price",
                "targetPrice",
            ),
        )
        if direct is not None:
            try:
                return float(direct)
            except (TypeError, ValueError):
                pass

        metadata = event.get("eventMetadata") or {}
        if isinstance(metadata, dict):
            for key in ("priceToBeat", "openPrice", "startPrice"):
                value = metadata.get(key)
                if value not in (None, ""):
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        pass

        rules = str(first_present(market, ("rules", "description", "resolutionSource")) or first_present(event, ("description", "resolutionSource")) or "")
        match = re.search(r"(?:price to beat|open price|start price)[^0-9$-]*\$?([0-9][0-9,]*(?:\.[0-9]+)?)", rules, re.I)
        if match:
            return float(match.group(1).replace(",", ""))
        return None
