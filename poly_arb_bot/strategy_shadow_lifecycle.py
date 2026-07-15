import json
import os
import time
import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path

from .ev_shadow import strategy_config
from .jsonl_history import history_paths, open_history
from .logger import JsonlLogger


SETTLEMENT_MAX_DELAY_MS = 10_000
DEFAULT_SETTLEMENT_ORPHAN_AFTER_SECONDS = 900


@dataclass(frozen=True)
class PortfolioLimits:
    combined_max_per_close_window: int = 1
    directional_max_order_size: float = 250.0
    directional_max_open_positions: int = 8
    directional_max_per_close_window: int = 4
    directional_max_open_notional: float = 20.0
    directional_max_daily_loss: float = 5.0
    directional_max_consecutive_losses: int = 5
    lottery_max_open_positions: int = 4
    lottery_max_order_size: float = 25.0
    lottery_max_per_close_window: int = 2
    lottery_max_open_notional: float = 5.0
    lottery_max_daily_loss: float = 5.0
    lottery_max_consecutive_losses: int = 5

    @classmethod
    def from_env(cls):
        return cls(
            combined_max_per_close_window=int(os.getenv("COMBINED_MAX_PER_CLOSE_WINDOW", "1")),
            directional_max_order_size=float(os.getenv("DIRECTIONAL_MAX_ORDER_SIZE", "250")),
            directional_max_open_positions=int(os.getenv("DIRECTIONAL_MAX_OPEN_POSITIONS", "8")),
            directional_max_per_close_window=int(os.getenv("DIRECTIONAL_MAX_PER_CLOSE_WINDOW", "4")),
            directional_max_open_notional=float(os.getenv("DIRECTIONAL_MAX_OPEN_NOTIONAL", "20")),
            directional_max_daily_loss=float(os.getenv("DIRECTIONAL_MAX_DAILY_LOSS", "5")),
            directional_max_consecutive_losses=int(os.getenv("DIRECTIONAL_MAX_CONSECUTIVE_LOSSES", "5")),
            lottery_max_open_positions=int(os.getenv("LOTTERY_MAX_OPEN_POSITIONS", "4")),
            lottery_max_order_size=float(os.getenv("LOTTERY_MAX_ORDER_SIZE", "25")),
            lottery_max_per_close_window=int(os.getenv("LOTTERY_MAX_PER_CLOSE_WINDOW", "2")),
            lottery_max_open_notional=float(os.getenv("LOTTERY_MAX_OPEN_NOTIONAL", "5")),
            lottery_max_daily_loss=float(os.getenv("LOTTERY_MAX_DAILY_LOSS", "5")),
            lottery_max_consecutive_losses=int(os.getenv("LOTTERY_MAX_CONSECUTIVE_LOSSES", "5")),
        )


class StrategyShadowLifecycle:
    def __init__(self, state_path, log_path, limits=None, orphan_after_seconds=None,
                 checkpoint_interval_seconds=5):
        self.state_path = Path(state_path)
        self.logger = JsonlLogger(Path(log_path))
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint_interval_seconds = float(checkpoint_interval_seconds)
        self._dirty = False
        self._last_checkpoint = time.monotonic()
        self.limits = limits or PortfolioLimits.from_env()
        self.orphan_after_seconds = float(
            orphan_after_seconds
            if orphan_after_seconds is not None
            else os.getenv(
                "SHADOW_SETTLEMENT_ORPHAN_AFTER_SECONDS",
                str(DEFAULT_SETTLEMENT_ORPHAN_AFTER_SECONDS),
            )
        )
        self.config_version = "shadow-portfolio-v3"
        self.strategy_config_hash = strategy_config()[1]
        self.config_hash = hashlib.sha256(
            json.dumps(asdict(self.limits), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.data = self._load()
        for field in ("real_order_submissions", "real_orders", "real_fills"):
            self.data.setdefault(field, 0)
        self.data.setdefault("completed_trades", [])
        self.data.setdefault("portfolio_rejections", {})
        self.data.setdefault("orphaned_positions", [])
        self.data["portfolio_limits"] = asdict(self.limits)
        self.data["config_version"] = self.config_version
        self.data["config_hash"] = self.config_hash
        self._mark_dirty()
        self._backfill_completed_trades()
        self._save(force=True)

    def _load(self):
        if not self.state_path.exists():
            return {"positions": {}, "completed": [], "audit_offset": 0, "paired_audit_offset": 0,
                    "real_order_submissions": 0, "real_orders": 0, "real_fills": 0}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _write_state(self):
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.state_path)

    def _mark_dirty(self):
        self._dirty = True

    def _save(self, force=False):
        if not self._dirty:
            return False
        if not force and time.monotonic() - self._last_checkpoint < self.checkpoint_interval_seconds:
            return False
        self._write_state()
        self._dirty = False
        self._last_checkpoint = time.monotonic()
        return True

    def flush(self):
        return self._save(force=True)

    def _backfill_completed_trades(self):
        if not self.logger.path.exists():
            return False
        known = {row.get("event_id"): row for row in self.data["completed_trades"]}
        changed = False
        for log_path in [self.logger.path]:
            if not log_path.exists():
                continue
            with open_history(log_path) as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    event_id = row.get("event_id")
                    if row.get("event_type") != "shadow_complete" or not event_id:
                        continue
                    if event_id in known:
                        trade = known[event_id]
                        if not trade.get("strategy_config_hash") and row.get("strategy_config_hash"):
                            trade["strategy_config_hash"] = row["strategy_config_hash"]
                            changed = True
                        continue
                    self.data["completed_trades"].append({
                        "event_id": event_id, "strategy": row.get("strategy"),
                        "market_id": row.get("market_id"), "ts": float(row.get("ts", 0)),
                        "pnl": float(row.get("realized_simulated_pnl", 0)),
                        "strategy_config_hash": row.get("strategy_config_hash"),
                    })
                    known[event_id] = self.data["completed_trades"][-1]
                    changed = True
        missing_hashes = {event_id for event_id, trade in known.items()
                          if event_id and not trade.get("strategy_config_hash")}
        for log_path in history_paths(self.logger.path)[:-1]:
            if not missing_hashes:
                break
            with open_history(log_path) as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    event_id = row.get("event_id")
                    if event_id not in missing_hashes or row.get("event_type") != "shadow_complete":
                        continue
                    if row.get("strategy_config_hash"):
                        known[event_id]["strategy_config_hash"] = row["strategy_config_hash"]
                        missing_hashes.remove(event_id)
                        changed = True
        self.data["completed_trades"] = self.data["completed_trades"][-20000:]
        return changed

    def _loss_block_reason(self, strategy, daily_limit, consecutive_limit, prefix):
        today = int(time.time() // 86400)
        completed = [trade for trade in self.data["completed_trades"]
                     if trade.get("strategy") == strategy and
                     trade.get("strategy_config_hash") == self.strategy_config_hash and
                     int(trade.get("ts", 0) // 86400) == today]
        if -sum(min(0.0, float(trade.get("pnl", 0))) for trade in completed) >= daily_limit:
            return f"{prefix}_daily_loss_limit"
        consecutive = 0
        for trade in reversed(self.data["completed_trades"]):
            if trade.get("strategy") != strategy:
                continue
            if trade.get("strategy_config_hash") != self.strategy_config_hash:
                continue
            if float(trade.get("pnl", 0)) >= 0:
                break
            consecutive += 1
        if consecutive >= consecutive_limit:
            return f"{prefix}_consecutive_loss_limit"
        return None

    def _reject(self, row, reason):
        key = self._key(row)
        if self.data["portfolio_rejections"].get(key) != reason:
            self.logger.write("shadow_position_reject", {
                "event_id": f'{row["event_id"]}:portfolio-reject',
                "entry_event_id": row["event_id"], "strategy": row["strategy"],
                "market_id": row["market_id"], "asset": row.get("asset"),
                "timeframe": row.get("timeframe"), "outcome": row.get("outcome"),
                "decision": "REJECT", "reason": reason,
                "config_version": self.config_version, "config_hash": self.config_hash,
                "real_order_submissions": 0, "real_orders": 0,
            })
        self.data["portfolio_rejections"][key] = reason
        self._mark_dirty()
        self._save()
        return False

    def _portfolio_block_reason(self, row, market, entry_cost):
        strategy = row["strategy"]
        if strategy == "paired_lock":
            return None
        positions = list(self.data["positions"].values())
        if any(position["market_id"] == row["market_id"] and
               position.get("outcome") == row.get("outcome") for position in positions):
            return "correlated_market_outcome_exposure"
        strategy_positions = [position for position in positions if position["strategy"] == strategy]
        close_ts = market.get("close_ts")
        combined_close = [position for position in positions
                          if position["strategy"] != "paired_lock" and
                          position.get("close_ts") == close_ts]
        if len(combined_close) >= self.limits.combined_max_per_close_window:
            return "combined_close_window_limit"
        same_close = [position for position in strategy_positions if position.get("close_ts") == close_ts]
        if strategy == "late_window_directional_ev":
            if float(row.get("target_size", 0)) > self.limits.directional_max_order_size:
                return "directional_order_size_limit"
            if len(strategy_positions) >= self.limits.directional_max_open_positions:
                return "directional_open_position_limit"
            if len(same_close) >= self.limits.directional_max_per_close_window:
                return "directional_close_window_limit"
            if sum(position["entry_cost"] for position in strategy_positions) + entry_cost > self.limits.directional_max_open_notional:
                return "directional_open_notional_limit"
            return self._loss_block_reason(
                strategy, self.limits.directional_max_daily_loss,
                self.limits.directional_max_consecutive_losses, "directional",
            )
        if float(row.get("target_size", 0)) > self.limits.lottery_max_order_size:
            return "lottery_order_size_limit"
        if len(strategy_positions) >= self.limits.lottery_max_open_positions:
            return "lottery_open_position_limit"
        if len(same_close) >= self.limits.lottery_max_per_close_window:
            return "lottery_close_window_limit"
        if sum(position["entry_cost"] for position in strategy_positions) + entry_cost > self.limits.lottery_max_open_notional:
            return "lottery_open_notional_limit"
        return self._loss_block_reason(
            strategy, self.limits.lottery_max_daily_loss,
            self.limits.lottery_max_consecutive_losses, "lottery",
        )

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
        block_reason = self._portfolio_block_reason(row, market, entry_cost)
        if block_reason:
            return self._reject(row, block_reason)
        self.data["portfolio_rejections"].pop(key, None)
        self.data["positions"][key] = {
            "event_id": row["event_id"], "strategy": strategy,
            "lifecycle_state": "ACTIVE",
            "market_id": row["market_id"], "asset": row.get("asset", market.get("asset")),
            "timeframe": market.get("interval", row.get("timeframe")),
            "outcome": row.get("outcome", "Both"),
            "entry_ts": row.get("ts"), "expected_fill_price": fill,
            "fees_per_share": fees, "target_size": size,
            "entry_cost": round(entry_cost, 12),
            "price_to_beat": row.get("price_to_beat"),
            "condition_id": row.get("condition_id"), "window": row.get("window"),
            "generation": row.get("generation"), "session": row.get("session"),
            "evaluation_sequence": row.get("evaluation_sequence"),
            "estimated_probability": row.get("estimated_probability"),
            "market_implied_probability": row.get("market_implied_probability"),
            "gross_edge": row.get("gross_edge"), "net_ev": row.get("net_ev"),
            "consensus_price": row.get("consensus_price"),
            "fast_price": row.get("fast_price"),
            "reference_state": row.get("reference_state"),
            "reference_quorum_met": row.get("reference_quorum_met"),
            "cross_source_divergence_bps": row.get("cross_source_divergence_bps"),
            "seconds_to_close": row.get("seconds_to_close"),
            "model_source": row.get("model_source"),
            "model_sample_count": row.get("model_sample_count"),
            "model_sample_span_seconds": row.get("model_sample_span_seconds"),
            "minimum_model_sample_span_seconds": row.get("minimum_model_sample_span_seconds"),
            "volatility_per_sqrt_second": row.get("volatility_per_sqrt_second"),
            "expected_move_log_std": row.get("expected_move_log_std"),
            "reference_log_distance": row.get("reference_log_distance"),
            "up_standardized_distance": row.get("up_standardized_distance"),
            "up_momentum_z": row.get("up_momentum_z"),
            "up_imbalance_z": row.get("up_imbalance_z"),
            "up_final_model_z": row.get("up_final_model_z"),
            "paired_book_imbalance": row.get("paired_book_imbalance"),
            "input_quality_score": row.get("input_quality_score"),
            "confidence_type": row.get("confidence_type"),
            "close_ts": market.get("close_ts"),
            "settlement_source": market.get("settlement_source"),
            "strategy_config_version": row.get("config_version"),
            "strategy_config_hash": row.get("config_hash"),
            "config_version": self.config_version, "config_hash": self.config_hash,
            "real_order_submissions": 0, "real_orders": 0,
        }
        self._mark_dirty()
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
        changed = False
        durable_transition = False

        for key, position in list(self.data["positions"].items()):
            close_ts = float(position.get("close_ts") or 0)
            if now < close_ts:
                continue

            sample = self._settlement_sample(position, venue)
            start_price = position.get("price_to_beat")
            if start_price is None:
                start_price = markets.get(position["market_id"], {}).get("open_price")

            paired = position["strategy"] == "paired_lock"
            settlement_ready = sample is not None and (paired or start_price is not None)

            if not settlement_ready:
                if position.get("lifecycle_state") != "SETTLEMENT_PENDING":
                    position["lifecycle_state"] = "SETTLEMENT_PENDING"
                    position["settlement_pending_since"] = now
                    changed = True

                if now - close_ts < self.orphan_after_seconds:
                    continue

                orphan_id = f'{position["event_id"]}:orphaned'
                orphan = {
                    **position,
                    "event_id": orphan_id,
                    "entry_event_id": position["event_id"],
                    "event_type": "shadow_orphaned",
                    "lifecycle_state": "ORPHANED",
                    "orphaned_at": now,
                    "orphan_reason": (
                        "settlement_sample_unavailable"
                        if sample is None
                        else "opening_anchor_unavailable"
                    ),
                    "real_order_submissions": 0,
                    "real_orders": 0,
                }
                self.logger.write("shadow_orphaned", orphan)
                self.data["orphaned_positions"] = (
                    self.data["orphaned_positions"] + [orphan]
                )[-20000:]
                del self.data["positions"][key]
                changed = True
                durable_transition = True
                continue

            winning_outcome = None if paired else (
                "Up" if float(sample["price"]) >= float(start_price) else "Down"
            )
            if paired:
                payout = position["target_size"]
            else:
                payout = (
                    position["target_size"]
                    if position["outcome"] == winning_outcome
                    else 0.0
                )

            pnl = round(payout - position["entry_cost"], 12)
            complete_id = f'{position["event_id"]}:complete'

            self.logger.write("shadow_complete", {
                **position,
                "event_id": complete_id,
                "entry_event_id": position["event_id"],
                "lifecycle_state": "COMPLETE",
                "settlement_price": float(sample["price"]),
                "settlement_timestamp_ms": float(sample["source_timestamp_ms"]),
                "winning_outcome": winning_outcome,
                "payout": payout,
                "realized_simulated_pnl": pnl,
                "real_order_submissions": 0,
                "real_orders": 0,
            })

            self.data["completed"] = (
                self.data["completed"] + [complete_id]
            )[-20000:]
            self.data["completed_trades"] = (
                self.data["completed_trades"] + [{
                    "event_id": complete_id,
                    "strategy": position["strategy"],
                    "market_id": position["market_id"],
                    "ts": now,
                    "pnl": pnl,
                    "strategy_config_hash": position.get("strategy_config_hash"),
                }]
            )[-20000:]

            del self.data["positions"][key]
            completed += 1
            changed = True
            durable_transition = True

        if changed:
            self._mark_dirty()
            self._save(force=durable_transition)

        return completed


def process_audit_once(audit_path, lifecycle, markets, offset_key="audit_offset"):
    audit_path = Path(audit_path)
    if not audit_path.exists():
        return 0
    stat = audit_path.stat()
    identity_key = f"{offset_key}_file_identity"
    identity = f"{stat.st_dev}:{stat.st_ino}"
    previous_identity = lifecycle.data.get(identity_key)
    if (previous_identity and previous_identity != identity) or stat.st_size < lifecycle.data.get(offset_key, 0):
        lifecycle.data[offset_key] = 0
        lifecycle._mark_dirty()
    if previous_identity != identity:
        lifecycle.data[identity_key] = identity
        lifecycle._mark_dirty()
    opened = 0
    with audit_path.open(encoding="utf-8") as handle:
        handle.seek(lifecycle.data.get(offset_key, 0))
        while line := handle.readline():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            opened += lifecycle.consume(row, markets)
        offset = handle.tell()
        if offset != lifecycle.data.get(offset_key):
            lifecycle.data[offset_key] = offset
            lifecycle._mark_dirty()
    lifecycle._save(force=bool(opened))
    return opened
