"""Temporary probe: verify reference-source WebSockets actually stream data.

Usage: python scripts/probe_reference_ws.py
Each source gets up to `timeout` seconds to deliver one usable message.
"""
import asyncio
import json
import ssl
import sys
import time

import websockets

TIMEOUT = 15


async def probe(name, url, sub=None, timeout=TIMEOUT, ping=None):
    started = time.monotonic()
    ssl_ctx = ssl.create_default_context()
    try:
        async with websockets.connect(url, ssl=ssl_ctx, ping_interval=None,
                                      open_timeout=10, close_timeout=2) as ws:
            if sub:
                await ws.send(sub)
            async def keepalive():
                while True:
                    await asyncio.sleep(5)
                    await ws.send(ping or "PING")
            ka = asyncio.create_task(keepalive()) if ping else None
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                elapsed = (time.monotonic() - started) * 1000
                snippet = raw[:200].replace("\n", " ")
                print(f"{name}: DATA {elapsed:.0f}ms | {snippet}")
                return True
            except asyncio.TimeoutError:
                print(f"{name}: TIMEOUT after {timeout}s (connected, no data)")
                return False
            finally:
                if ka:
                    ka.cancel()
    except Exception as exc:
        elapsed = (time.monotonic() - started) * 1000
        print(f"{name}: FAIL {elapsed:.0f}ms | {type(exc).__name__}: {exc}")
        return False


async def main():
    results = await asyncio.gather(
        probe("binance_ws ", "wss://data-stream.binance.vision/stream?streams=btcusdt@bookTicker"),
        probe("coinbase_ws", "wss://ws-feed.exchange.coinbase.com/",
              json.dumps({"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]})),
        probe("kraken_ws ", "wss://ws.kraken.com/v2",
              json.dumps({"method": "subscribe", "params": {"channel": "ticker", "symbol": ["BTC/USD"]}})),
        probe("bybit_ws  ", "wss://stream.bybit.com/v5/public/spot",
              json.dumps({"op": "subscribe", "args": ["tickers.BTCUSDT"]})),
        probe("okx_ws    ", "wss://ws.okx.com:8443/ws/v5/public",
              json.dumps({"op": "subscribe", "args": [{"channel": "tickers", "instId": "BTC-USDT"}]})),
        probe("rtds_clink", "wss://ws-live-data.polymarket.com/",
              json.dumps({"action": "subscribe", "subscriptions": [
                  {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]}),
              ping="PING"),
    )
    ok = sum(1 for r in results if r)
    print(f"\nsummary: {ok}/6 sources streaming")
    return 0 if ok >= 4 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
