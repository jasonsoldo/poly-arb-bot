import asyncio
import json
from pathlib import Path
from typing import Dict, Iterable

from .logger import JsonlLogger
from .shadow_opportunity import LocalOrderBook, evaluate_pair


WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class ShadowMarketMonitor:
    def __init__(self, markets: Iterable[Dict], size: float = 10.0, fee_rate: float = 0.07, log_file: Path = Path("logs/shadow.jsonl")):
        self.markets = {m["market_id"]: m for m in markets}
        self.tokens = {}
        self.books = {}
        for market in self.markets.values():
            for outcome in ("Up", "Down"):
                token = market[f"{outcome.lower()}_token_id"]
                self.tokens[token] = (market["market_id"], outcome)
                self.books[token] = LocalOrderBook(token)
        self.size, self.fee_rate, self.logger = size, fee_rate, JsonlLogger(log_file)
        self.active_since = {}

    async def run(self, ws_url=WS_URL):
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("websockets is required: pip install websockets") from exc
        async with websockets.connect(ws_url, ping_interval=10, ping_timeout=20) as socket:
            await socket.send(json.dumps({"assets_ids": list(self.tokens), "type": "market", "custom_feature_enabled": True}))
            async for raw in socket:
                if raw == "PONG":
                    continue
                self.handle(json.loads(raw))

    def handle(self, message):
        event = message if isinstance(message, dict) else {}
        event_type = event.get("event_type", "")
        token = event.get("asset_id") or event.get("market")
        if event_type == "book" and token in self.books:
            self.books[token].snapshot(event.get("bids", []), event.get("asks", []))
        elif event_type == "price_change":
            for change in event.get("price_changes", []):
                asset = change.get("asset_id", token)
                if asset in self.books:
                    self.books[asset].price_change([change])
        market_id = self.tokens.get(token, (None,))[0] if token else None
        market = self.markets.get(market_id)
        if not market:
            return
        up_token = market["up_token_id"]
        down_token = market["down_token_id"]
        opportunity = evaluate_pair(market_id, self.books[up_token].asks_for_vwap(), self.books[down_token].asks_for_vwap(), self.size, self.fee_rate)
        key = market_id
        if opportunity.profitable_after_fees:
            self.active_since.setdefault(key, opportunity.ts)
            payload = dict(opportunity.__dict__)
            payload.update({"event_type": "shadow_opportunity", "opportunity_duration_s": opportunity.ts - self.active_since[key]})
            self.logger.write("shadow_opportunity", payload)
        else:
            self.active_since.pop(key, None)
