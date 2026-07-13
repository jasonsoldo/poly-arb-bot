import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class HttpResponse:
    data: Any
    elapsed_ms: int
    url: str


class HttpClient:
    def __init__(self, timeout: float = 2.0, user_agent: str = "poly-arb-bot/0.1"):
        self.timeout = timeout
        self.user_agent = user_agent

    def get_json(self, base_url: str, path: str, params: Optional[Dict[str, Any]] = None) -> HttpResponse:
        query = urlencode(params or {}, doseq=True)
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{query}"
        return self._json_request("GET", url)

    def post_json(self, base_url: str, path: str, payload: Any) -> HttpResponse:
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        body = json.dumps(payload).encode("utf-8")
        return self._json_request("POST", url, body)

    def _json_request(self, method: str, url: str, body: Optional[bytes] = None) -> HttpResponse:
        started = time.monotonic()
        headers = {
            "Accept": "application/json", "User-Agent": self.user_agent,
            "Accept-Encoding": "identity", "Connection": "close",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                detail = ""
            suffix = f" body={detail}" if detail else ""
            raise RuntimeError(f"HTTP {method} {exc.code} failed for {url}:{suffix}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"HTTP {method} network failed for {url}: {exc}") from exc
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return HttpResponse(json.loads(raw), elapsed_ms, url)
