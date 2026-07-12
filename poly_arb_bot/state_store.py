import json
from pathlib import Path
from typing import Dict, Optional


class JsonStateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load()

    def seen_order(self, client_order_id: str) -> bool:
        return client_order_id in self._state.get("client_order_ids", {})

    def record_order(self, client_order_id: str, payload: Dict) -> None:
        self._state.setdefault("client_order_ids", {})[client_order_id] = payload
        self._save()

    def _load(self) -> Dict:
        if not self.path.exists():
            return {"client_order_ids": {}}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._state, indent=2, sort_keys=True), encoding="utf-8")
