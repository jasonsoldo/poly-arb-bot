import json
import os
from pathlib import Path

from .logger import JsonlLogger


SETTLEMENT_MAX_DELAY_MS = 10_000


class StrategyShadowLifecycle:
    def __init__(self, state_path, log_path):
        self.state_path = Path(state_path)
        self.logger = JsonlLogger(Path(log_path))
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self):
        if not self.state_path.exists():
            return {"positions": {}, "completed": [], "audit_offset": 0, "paired_audit_offset": 0}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save(self):
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.state_path)

    @staticmethod
    def _key(row):
        return ":".join((row["strategy"], row["market_id"], row.get("outcome", "Both")))

    def consume(self, row, markets):
        strategy = row.get("strategy")
        if strategy not in {"late_window_directional_ev", "low_price_lottery_ev", "paired_lock"}:
            return False
        accepted = row.get("decision") == "ACCEPT" or (
            strategy == "paired_lock" and row.get("event_type") == "shadow_opportunity"
        )
        if not accepted or row.get("market_id") not in markets:
            return False
        key = self._key(row)
        if key in self.data["positions"]:
            return False
        size = float(row.get("target_size", 10))
        market = markets[row["market_id"]]
        paired = strategy == "paired_lock"
        fill = None if paired else float(row["expected_fill_price"])
        fees = 0.0 if paired else float(row.get("fees", 0))
        entry_cost = float(row["net_cost"]) if paired else size * (fill + fees)
        self.data["positions"][key] = {
            "event_id": row["event_id"], "strategy": strategy,
            "market_id": row["market_id"], "asset": row.get("asset", market.get("asset")),
            "timeframe": market.get("interval", row.get("timeframe")),
            "outcome": row.get("outcome", "Both"),
            "entry_ts": row.get("ts"), "expected_fill_price": fill,
            "fees_per_share": fees, "target_size": size,
            "entry_cost": round(entry_cost, 12),
            "price_to_beat": row.get("price_to_beat"),
            "close_ts": market.get("close_ts"),
            "settlement_source": market.get("settlement_source"),
            "real_order_submissions": 0, "real_orders": 0,
        }
        self._save()
        return True

    @staticmethod
    def _settlement_sample(position, venue):
        source = position.get("settlement_source")
        if source not in {"binance", "chainlink"}:
            return None
        rows = venue.get("assets", {}).get(position.get("asset"), {}).get(
            f"{source}_settlement_samples", []
        )
        close_ms = float(position.get("close_ts") or 0) * 1000
        eligible = []
        for row in rows:
            timestamp = float(row.get("source_timestamp_ms", 0))
            if not close_ms <= timestamp <= close_ms + SETTLEMENT_MAX_DELAY_MS:
                continue
            if source == "binance" and row.get("timeframe") != position.get("timeframe"):
                continue
            eligible.append(row)
        return min(eligible, key=lambda row: float(row["source_timestamp_ms"])) if eligible else None

    def settle(self, markets, venue, now):
        completed = 0
        for key, position in list(self.data["positions"].items()):
            if now < float(position.get("close_ts") or 0):
                continue
            sample = self._settlement_sample(position, venue)
            start_price = position.get("price_to_beat")
            if start_price is None:
                start_price = markets.get(position["market_id"], {}).get("open_price")
            paired = position["strategy"] == "paired_lock"
            if not sample or (not paired and start_price is None):
                continue
            winning_outcome = None if paired else (
                "Up" if float(sample["price"]) >= float(start_price) else "Down"
            )
            if paired:
                payout = position["target_size"]
            else:
                payout = position["target_size"] if position["outcome"] == winning_outcome else 0.0
            pnl = round(payout - position["entry_cost"], 12)
            complete_id = f'{position["event_id"]}:complete'
            self.logger.write("shadow_complete", {
                **position, "event_id": complete_id, "entry_event_id": position["event_id"],
                "settlement_price": float(sample["price"]),
                "settlement_timestamp_ms": float(sample["source_timestamp_ms"]),
                "winning_outcome": winning_outcome, "payout": payout,
                "realized_simulated_pnl": pnl, "real_order_submissions": 0, "real_orders": 0,
            })
            self.data["completed"] = (self.data["completed"] + [complete_id])[-20000:]
            del self.data["positions"][key]
            completed += 1
        if completed:
            self._save()
        return completed


def process_audit_once(audit_path, lifecycle, markets, offset_key="audit_offset"):
    audit_path = Path(audit_path)
    if not audit_path.exists():
        return 0
    if audit_path.stat().st_size < lifecycle.data.get(offset_key, 0):
        lifecycle.data[offset_key] = 0
    opened = 0
    with audit_path.open(encoding="utf-8") as handle:
        handle.seek(lifecycle.data.get(offset_key, 0))
        while line := handle.readline():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            opened += lifecycle.consume(row, markets)
        lifecycle.data[offset_key] = handle.tell()
    lifecycle._save()
    return opened
