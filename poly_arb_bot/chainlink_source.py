import time
from dataclasses import dataclass
from .http_utils import HttpClient


LATEST_ROUND_DATA_SELECTOR = "0xfeaf968c"
DECIMALS_SELECTOR = "0x313ce567"


@dataclass(frozen=True)
class ChainlinkPrice:
    feed_address: str
    price: float
    updated_at: int
    decimals: int
    latency_ms: int
    stale: bool


class ChainlinkSource:
    def __init__(self, rpc_url: str, http: HttpClient = None, max_staleness_seconds: int = 180):
        self.rpc_url = rpc_url
        self.http = http or HttpClient(timeout=2.0)
        self.max_staleness_seconds = max_staleness_seconds

    def latest_price(self, feed_address: str) -> ChainlinkPrice:
        decimals_raw = self._eth_call(feed_address, DECIMALS_SELECTOR)
        latest_raw = self._eth_call(feed_address, LATEST_ROUND_DATA_SELECTOR)
        decimals = int(decimals_raw, 16)
        words = self._decode_words(latest_raw)
        answer = self._signed_int(words[1])
        updated_at = int(words[3], 16)
        price = answer / (10 ** decimals)
        stale = (int(time.time()) - updated_at) > self.max_staleness_seconds
        return ChainlinkPrice(
            feed_address=feed_address,
            price=price,
            updated_at=updated_at,
            decimals=decimals,
            latency_ms=0,
            stale=stale,
        )

    def _eth_call(self, to_address: str, data: str) -> str:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": to_address, "data": data}, "latest"],
        }
        started = time.monotonic()
        response = self.http.post_json(self.rpc_url, "", payload)
        result = response.data.get("result")
        if not result:
            raise RuntimeError(f"Chainlink eth_call failed: {response.data}")
        return result

    @staticmethod
    def _decode_words(hex_data: str):
        clean = hex_data[2:] if hex_data.startswith("0x") else hex_data
        return [clean[index:index + 64] for index in range(0, len(clean), 64)]

    @staticmethod
    def _signed_int(word: str) -> int:
        value = int(word, 16)
        if value >= 2 ** 255:
            value -= 2 ** 256
        return value
