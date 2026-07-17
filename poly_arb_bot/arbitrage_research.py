import json
import math
import os
from pathlib import Path


ARBITRAGE_STRATEGIES = (
    "paired_lock",
    "split_sell_lock",
    "maker_complete_set_arb",
)
MAX_SEEN_EVENTS = 50_000
MAX_PATTERN_VALUES = 4_096


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * fraction)]


def _wilson_interval(successes, trials, z=1.96):
    if trials <= 0:
        return (None, None)
    probability = successes / trials
    denominator = 1 + z * z / trials
    center = (probability + z * z / (2 * trials)) / denominator
    margin = z * math.sqrt(
        probability * (1 - probability) / trials + z * z / (4 * trials * trials)
    ) / denominator
    return (max(0.0, center - margin), min(1.0, center + margin))


def _mean_confidence_interval(values, z=1.96):
    if not values:
        return (None, None, None)
    mean = sum(values) / len(values)
    if len(values) == 1:
        return (mean, None, None)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    margin = z * math.sqrt(variance / len(values))
    return (mean, mean - margin, mean + margin)


def _pattern_statistics(attempts, outcomes):
    attempts = int(attempts or 0)
    book_executable = sum(
        row.get("state") == "BOOK_EXECUTABLE" for row in outcomes
    )
    orphaned = sum(row.get("state") == "ORPHANED" for row in outcomes)
    invalidated = sum(row.get("state") == "INVALIDATED" for row in outcomes)
    pnl_values = [float(row.get("pnl", 0)) for row in outcomes]
    positive_total = sum(max(0.0, value) for value in pnl_values)
    positive_by_market = {}
    for row, pnl in zip(outcomes, pnl_values):
        market_id = row.get("market_id") or "UNKNOWN"
        positive_by_market[market_id] = (
            positive_by_market.get(market_id, 0.0) + max(0.0, pnl)
        )
    wilson = _wilson_interval(book_executable, attempts)
    pnl_interval = _mean_confidence_interval(pnl_values)
    return {
        "attempts": attempts,
        "book_executable": book_executable,
        "orphaned": orphaned,
        "invalidated": invalidated,
        "book_executable_rate": (
            book_executable / attempts if attempts else None
        ),
        "book_executable_wilson_95": {
            "lower": wilson[0], "upper": wilson[1],
        },
        "orphan_rate": orphaned / attempts if attempts else None,
        "invalidated_rate": invalidated / attempts if attempts else None,
        "conservative_total_pnl": sum(pnl_values),
        "max_market_pnl_contribution": (
            round(max(positive_by_market.values(), default=0) /
                  positive_total, 12)
            if positive_total > 0 else None
        ),
        "conservative_pnl_95": {
            "mean": pnl_interval[0],
            "lower": pnl_interval[1],
            "upper": pnl_interval[2],
        },
    }


def _candidate_qualified(stats, distinct_close_windows):
    return (
        stats["attempts"] >= 20
        and distinct_close_windows >= 10
        and stats["book_executable_wilson_95"]["lower"] is not None
        and stats["book_executable_wilson_95"]["lower"] >= .8
        and stats["orphan_rate"] is not None
        and stats["orphan_rate"] <= .05
        and stats["conservative_total_pnl"] > 0
        and stats["conservative_pnl_95"]["lower"] is not None
        and stats["conservative_pnl_95"]["lower"] > 0
    )


def _classification(pattern, stats, validation=None):
    windows = int(pattern.get("distinct_close_windows", 0))
    if not pattern.get("independent_episodes") and not stats["attempts"]:
        return "NO_EVIDENCE"
    if _candidate_qualified(stats, windows):
        if validation and _candidate_qualified(
                validation, validation.get("distinct_close_windows", 0)) and (
                validation.get("max_market_pnl_contribution") is not None
                and validation["max_market_pnl_contribution"] <= .2):
            classification = "OUT_OF_SAMPLE_VALIDATED"
        else:
            classification = "RESEARCH_CANDIDATE"
        if pattern.get("strategy") == "maker_complete_set_arb":
            return "MAKER_RESEARCH_CANDIDATE"
        return classification
    if (
        stats["attempts"] >= 5
        and windows >= 3
        and stats["conservative_total_pnl"] > 0
    ):
        return "PROVISIONAL"
    return "OBSERVED"


def _empty_funnel():
    return {
        "evaluations": 0,
        "depth_passed": 0,
        "fee_passed": 0,
        "latency_survived": 0,
        "independent_episodes": 0,
        "shadow_attempts": 0,
        "leg_1_book_executable": 0,
        "both_legs_book_executable": 0,
        "orphaned": 0,
        "invalidated": 0,
        "completed": 0,
        "positive_completed": 0,
    }


def _empty_state():
    return {
        "version": 2,
        "audit": {"identity": None, "offset": 0},
        "execution": {"identity": None, "offset": 0},
        "seen": [],
        "funnels": {name: _empty_funnel() for name in ARBITRAGE_STRATEGIES},
        "active": {},
        "patterns": {},
        "counterfactual_active": {},
        "counterfactual_patterns": {},
        "episode_starts": {},
    }


class IncrementalArbitrageResearch:
    def __init__(self, audit_path, execution_path=None, state_path=None):
        self.audit_path = Path(audit_path)
        self.execution_path = Path(execution_path) if execution_path else None
        self.state_path = Path(state_path) if state_path else None
        self.state = self._load()
        self.state.setdefault("active", {})
        self.state.setdefault("patterns", {})
        self.state.setdefault("counterfactual_active", {})
        self.state.setdefault("counterfactual_patterns", {})
        self.state.setdefault("episode_starts", {})
        self.state["version"] = 2
        funnels = self.state.setdefault("funnels", {})
        for strategy in ARBITRAGE_STRATEGIES:
            funnel = funnels.setdefault(strategy, _empty_funnel())
            for field, value in _empty_funnel().items():
                funnel.setdefault(field, value)
            funnel.pop("both_legs_filled", None)
        self._migrate_legacy_patterns()
        self._seen_order = list(self.state.get("seen", []))[-MAX_SEEN_EVENTS:]
        self._seen = set(self._seen_order)

    def _migrate_legacy_patterns(self):
        patterns = self.state["patterns"]
        for typed_key, typed in list(patterns.items()):
            if not typed.get("asset") or not typed.get("timeframe"):
                continue
            if typed.get("independent_episodes", 0) or not typed.get("completed", 0):
                continue
            candidates = [
                (key, pattern) for key, pattern in patterns.items()
                if key != typed_key
                and pattern.get("strategy") == typed.get("strategy")
                and not pattern.get("asset")
                and not pattern.get("timeframe")
                and pattern.get("independent_episodes", 0)
                and not pattern.get("completed", 0)
                and pattern.get("target_size") == typed.get("target_size")
            ]
            if len(candidates) != 1:
                continue
            legacy_key, legacy = candidates[0]
            legacy["asset"] = typed["asset"]
            legacy["timeframe"] = typed["timeframe"]
            legacy["completed"] = typed.get("completed", 0)
            legacy["positive_completed"] = typed.get("positive_completed", 0)
            legacy["simulated_pnl"] = typed.get("simulated_pnl", 0.0)
            canonical_key = self._pattern_key(legacy)
            patterns.pop(legacy_key)
            patterns.pop(typed_key)
            canonical = patterns.get(canonical_key)
            if canonical:
                for field in (
                    "independent_episodes", "latency_survived", "completed",
                    "positive_completed", "simulated_pnl",
                ):
                    canonical[field] = canonical.get(field, 0) + legacy.get(field, 0)
                for field in ("close_windows", "market_ids"):
                    canonical[field] = list(dict.fromkeys(
                        canonical.get(field, []) + legacy.get(field, [])
                    ))
                for field in ("durations", "profits"):
                    canonical[field] = (
                        canonical.get(field, []) + legacy.get(field, [])
                    )[-MAX_PATTERN_VALUES:]
            else:
                patterns[canonical_key] = legacy

    def _load(self):
        if not self.state_path:
            return _empty_state()
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _empty_state()
        return state if state.get("version") in (1, 2) else _empty_state()

    def _save(self):
        if not self.state_path:
            return
        self.state["seen"] = self._seen_order[-MAX_SEEN_EVENTS:]
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(self.state, separators=(",", ":")), encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    @staticmethod
    def _identity(stat):
        return f"{stat.st_dev}:{stat.st_ino}"

    def _consume_file(self, path, bucket_name, consumer):
        if not path or not path.exists():
            return False
        bucket = self.state[bucket_name]
        stat = path.stat()
        identity = self._identity(stat)
        if bucket.get("identity") != identity or stat.st_size < bucket.get("offset", 0):
            bucket.update(identity=identity, offset=0)
        changed = False
        with path.open("rb") as handle:
            handle.seek(bucket.get("offset", 0))
            while True:
                start = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    handle.seek(start)
                    break
                try:
                    row = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    changed = True
                    continue
                event_id = row.get("event_id")
                if event_id and event_id in self._seen:
                    changed = True
                    continue
                if event_id:
                    self._seen.add(event_id)
                    self._seen_order.append(event_id)
                    if len(self._seen_order) > MAX_SEEN_EVENTS:
                        self._seen.discard(self._seen_order.pop(0))
                consumer(row)
                changed = True
            bucket["offset"] = handle.tell()
        return changed

    @staticmethod
    def _evaluation_kind(row):
        strategy = row.get("strategy")
        event_type = row.get("event_type")
        if strategy == "paired_lock" and event_type == "shadow_eval":
            return strategy
        if strategy == "split_sell_lock" and event_type == "shadow_split_sell_eval":
            return strategy
        if strategy == "maker_complete_set_arb" and event_type == "shadow_maker_quote_eval":
            return strategy
        return None

    @staticmethod
    def _stage_passes(strategy, row):
        if strategy == "paired_lock":
            return (
                bool(row.get("fok")),
                float(row.get("locked_profit", 0)) > 0,
                float(row.get("expected_execution_value", 0)) > 0,
            )
        if strategy == "split_sell_lock":
            size = float(row.get("target_size", 0))
            depth = (
                size > 0
                and float(row.get("up_sell_fill", 0)) >= size
                and float(row.get("down_sell_fill", 0)) >= size
            )
            return (
                depth,
                float(row.get("locked_profit", 0)) > 0,
                float(row.get("expected_execution_value", 0)) > 0,
            )
        return (
            bool(row.get("quote_geometry_qualified")),
            float(row.get("locked_edge_if_both_fill", 0)) > 0,
            float(row.get("expected_value", 0)) > 0,
        )

    @staticmethod
    def _pattern_key(row):
        size = row.get("target_size", row.get("size"))
        delay_us = row.get("time_between_legs_us")
        delay_ms = row.get("delay_ms")
        if delay_us is not None:
            delay_ms = round(float(delay_us) / 1000, 3)
        return "|".join((
            str(row.get("strategy") or ""),
            str(row.get("asset") or "UNKNOWN"),
            str(row.get("timeframe") or "UNKNOWN"),
            str(size if size is not None else "N/A"),
            str(delay_ms if delay_ms is not None else "N/A"),
            str(row.get("leg_order") or "N/A"),
            str(row.get("config_hash") or "LEGACY"),
        ))

    def _pattern(self, row):
        key = self._pattern_key(row)
        pattern = self.state["patterns"].setdefault(key, {
            "strategy": row.get("strategy"),
            "asset": row.get("asset"),
            "timeframe": row.get("timeframe"),
            "target_size": row.get("target_size", row.get("size")),
            "delay_ms": (
                round(float(row["time_between_legs_us"]) / 1000, 3)
                if row.get("time_between_legs_us") is not None
                else row.get("delay_ms")
            ),
            "leg_order": row.get("leg_order"),
            "config_hash": row.get("config_hash"),
            "independent_episodes": 0,
            "close_windows": [],
            "durations": [],
            "profits": [],
            "latency_survived": 0,
            "completed": 0,
            "positive_completed": 0,
            "simulated_pnl": 0.0,
            "market_ids": [],
            "attempts": 0,
            "attempt_ids": [],
            "lifetime_attempts": 0,
            "leg_1_book_executable": 0,
            "both_legs_book_executable": 0,
            "orphaned": 0,
            "invalidated": 0,
            "outcome_attempt_ids": [],
            "outcomes": [],
        })
        pattern.setdefault("attempt_ids", list(
            pattern.get("outcome_attempt_ids", [])
        )[-MAX_PATTERN_VALUES:])
        pattern.setdefault("lifetime_attempts", int(pattern.get("attempts", 0)))
        return pattern

    def _consume_observed_arb(self, row):
        event_type = row.get("event_type")
        strategy = row.get("strategy")
        if strategy not in ARBITRAGE_STRATEGIES:
            return False
        observed_types = {
            "arb_episode_started", "arb_episode_ended", "arb_shadow_attempt",
            "arb_shadow_leg_result", "arb_shadow_book_executable",
            "arb_shadow_orphaned", "arb_shadow_invalidated",
            "arb_research_summary",
        }
        if event_type not in observed_types:
            return False
        if event_type == "arb_research_summary":
            return True
        funnel = self.state["funnels"][strategy]
        pattern = self._pattern(row)
        if event_type == "arb_episode_started":
            funnel["independent_episodes"] += 1
            pattern["independent_episodes"] += 1
            close_window = str(row.get("close_ts") or row.get("market_id"))
            if close_window not in pattern["close_windows"]:
                pattern["close_windows"].append(close_window)
            market_id = row.get("market_id")
            if market_id and market_id not in pattern["market_ids"]:
                pattern["market_ids"].append(market_id)
            self.state["episode_starts"][row.get("event_id")] = row.get("ts")
            return True
        if event_type == "arb_episode_ended":
            return True
        if event_type == "arb_shadow_attempt":
            funnel["shadow_attempts"] += 1
            pattern["lifetime_attempts"] += 1
            attempt_ids = pattern["attempt_ids"]
            attempt_id = row.get("attempt_id") or row.get("event_id")
            if attempt_id not in attempt_ids:
                attempt_ids.append(attempt_id)
            pattern["attempt_ids"] = attempt_ids[-MAX_PATTERN_VALUES:]
            pattern["attempts"] = len(pattern["attempt_ids"])
            return True
        if event_type == "arb_shadow_leg_result":
            if int(row.get("leg_index", 0)) == 1 and row.get(
                    "first_leg_book_executable") is True:
                funnel["leg_1_book_executable"] += 1
                pattern["leg_1_book_executable"] += 1
            return True

        attempt_id = row.get("attempt_id") or row.get("event_id")
        outcome_ids = pattern["outcome_attempt_ids"]
        if attempt_id in outcome_ids:
            return True
        outcome_ids.append(attempt_id)
        pattern["outcome_attempt_ids"] = outcome_ids[-MAX_PATTERN_VALUES:]
        if event_type == "arb_shadow_book_executable":
            funnel["both_legs_book_executable"] += 1
            pattern["both_legs_book_executable"] += 1
            pnl = float(row.get("delayed_locked_profit", 0))
            state = "BOOK_EXECUTABLE"
        elif event_type == "arb_shadow_orphaned":
            funnel["orphaned"] += 1
            pattern["orphaned"] += 1
            pnl = float(row.get("orphan_pnl", 0))
            state = "ORPHANED"
        else:
            funnel["invalidated"] += 1
            pattern["invalidated"] += 1
            pnl = float(row.get("orphan_pnl", 0))
            state = "INVALIDATED"
        pattern["outcomes"].append({
            "attempt_id": attempt_id,
            "close_window": str(row.get("close_ts") or row.get("market_id")),
            "market_id": row.get("market_id"),
            "state": state,
            "pnl": pnl,
        })
        pattern["outcomes"] = pattern["outcomes"][-MAX_PATTERN_VALUES:]
        return True

    def _consume_audit(self, row):
        if row.get("event_type") == "shadow_arb_counterfactual":
            self._consume_counterfactual(row)
            return
        if self._consume_observed_arb(row):
            return
        strategy = self._evaluation_kind(row)
        if not strategy:
            return
        funnel = self.state["funnels"][strategy]
        funnel["evaluations"] += 1
        depth, fee, latency = self._stage_passes(strategy, row)
        funnel["depth_passed"] += int(depth)
        funnel["fee_passed"] += int(fee)
        funnel["latency_survived"] += int(latency)

        active_key = f"{strategy}|{row.get('market_id')}"
        decision = row.get("decision")
        if decision != "ACCEPT":
            self.state["active"].pop(active_key, None)
            return
        identity = f"{row.get('generation')}|{row.get('session')}"
        if self.state["active"].get(active_key) == identity:
            return
        self.state["active"][active_key] = identity
        funnel["independent_episodes"] += 1
        pattern = self._pattern(row)
        pattern["independent_episodes"] += 1
        market_id = row.get("market_id")
        market_ids = pattern.setdefault("market_ids", [])
        if market_id and market_id not in market_ids:
            market_ids.append(market_id)
        window = str(row.get("close_ts") or row.get("market_id"))
        if window not in pattern["close_windows"]:
            pattern["close_windows"].append(window)
        if row.get("duration_ms") is not None:
            pattern["durations"].append(float(row["duration_ms"]))
            pattern["durations"] = pattern["durations"][-MAX_PATTERN_VALUES:]
        if row.get("locked_profit") is not None:
            pattern["profits"].append(float(row["locked_profit"]))
            pattern["profits"] = pattern["profits"][-MAX_PATTERN_VALUES:]
        pattern["latency_survived"] += int(latency)

    def _consume_counterfactual(self, row):
        for observation in row.get("observations", []):
            strategy = observation.get("method")
            if strategy not in ("paired_lock", "split_sell_lock"):
                continue
            for stress in observation.get("latency_stress", []):
                key = "|".join((
                    strategy,
                    str(row.get("asset") or "UNKNOWN"),
                    str(row.get("timeframe") or "UNKNOWN"),
                    str(observation.get("target_size")),
                    str(stress.get("delay_ms")),
                ))
                active_key = f"{key}|{row.get('market_id')}"
                pattern = self.state["counterfactual_patterns"].setdefault(key, {
                    "strategy": strategy,
                    "asset": row.get("asset"),
                    "timeframe": row.get("timeframe"),
                    "target_size": observation.get("target_size"),
                    "delay_ms": stress.get("delay_ms"),
                    "observations": 0,
                    "qualified_observations": 0,
                    "independent_episodes": 0,
                    "close_windows": [],
                    "profits": [],
                    "expected_values": [],
                })
                pattern["observations"] += 1
                qualified = (
                    observation.get("depth_ok") is True
                    and float(observation.get("post_cost_profit", 0)) > 0
                    and float(stress.get("expected_execution_value", 0)) > 0
                )
                if not qualified:
                    self.state["counterfactual_active"].pop(active_key, None)
                    continue
                pattern["qualified_observations"] += 1
                identity = f"{row.get('generation')}|{row.get('session')}"
                if self.state["counterfactual_active"].get(active_key) == identity:
                    continue
                self.state["counterfactual_active"][active_key] = identity
                pattern["independent_episodes"] += 1
                window = str(row.get("close_ts") or row.get("market_id"))
                if window not in pattern["close_windows"]:
                    pattern["close_windows"].append(window)
                pattern["profits"].append(float(observation["post_cost_profit"]))
                pattern["expected_values"].append(
                    float(stress["expected_execution_value"])
                )
                pattern["profits"] = pattern["profits"][-MAX_PATTERN_VALUES:]
                pattern["expected_values"] = pattern["expected_values"][-MAX_PATTERN_VALUES:]

    def _consume_execution(self, row):
        strategy = row.get("strategy")
        if strategy not in ARBITRAGE_STRATEGIES or row.get("event_type") != "shadow_complete":
            return
        funnel = self.state["funnels"][strategy]
        funnel["completed"] += 1
        pnl = float(row.get("realized_simulated_pnl", 0))
        funnel["positive_completed"] += int(pnl > 0)
        candidates = [
            pattern for pattern in self.state["patterns"].values()
            if pattern.get("strategy") == strategy
            and row.get("market_id") in pattern.get("market_ids", [])
        ]
        if not candidates:
            candidates = [
                pattern for pattern in self.state["patterns"].values()
                if pattern.get("strategy") == strategy
                and pattern.get("asset") == row.get("asset")
                and pattern.get("timeframe") == row.get("timeframe")
            ]
        if not candidates:
            candidates = [self._pattern(row)]
        pattern = candidates[0]
        if not pattern.get("asset") and row.get("asset"):
            pattern["asset"] = row["asset"]
        if not pattern.get("timeframe") and row.get("timeframe"):
            pattern["timeframe"] = row["timeframe"]
        pattern["completed"] += 1
        pattern["positive_completed"] += int(pnl > 0)
        pattern["simulated_pnl"] += pnl

    def refresh(self):
        changed = self._consume_file(
            self.audit_path, "audit", self._consume_audit,
        )
        changed = self._consume_file(
            self.execution_path, "execution", self._consume_execution,
        ) or changed
        if changed:
            self._save()
        return self.report()

    def report(self):
        patterns = []
        for raw in self.state["patterns"].values():
            durations = raw.get("durations", [])
            profits = raw.get("profits", [])
            windows = len(raw.get("close_windows", []))
            episodes = int(raw.get("independent_episodes", 0))
            outcomes = raw.get("outcomes", [])
            stats = _pattern_statistics(raw.get("attempts", 0), outcomes)
            ordered_windows = sorted(set(raw.get("close_windows", [])))
            split_at = math.ceil(len(ordered_windows) * .6)
            discovery_windows = set(ordered_windows[:split_at])
            validation_windows = set(ordered_windows[split_at:])
            discovery_outcomes = [
                outcome for outcome in outcomes
                if outcome.get("close_window") in discovery_windows
            ]
            validation_outcomes = [
                outcome for outcome in outcomes
                if outcome.get("close_window") in validation_windows
            ]
            discovery = _pattern_statistics(
                len(discovery_outcomes), discovery_outcomes,
            )
            discovery["distinct_close_windows"] = len(discovery_windows)
            validation = _pattern_statistics(
                len(validation_outcomes), validation_outcomes,
            )
            validation["distinct_close_windows"] = len(validation_windows)
            row = {
                key: raw.get(key) for key in (
                    "strategy", "asset", "timeframe", "target_size", "delay_ms",
                    "independent_episodes", "completed", "positive_completed",
                    "simulated_pnl", "leg_order", "config_hash",
                    "lifetime_attempts",
                )
            }
            row.update({
                "distinct_close_windows": windows,
                "duration_ms": {
                    "p50": _percentile(durations, 0.5),
                    "p95": _percentile(durations, 0.95),
                },
                "median_post_cost_profit": _percentile(profits, 0.5),
                "latency_survival_rate": (
                    raw.get("latency_survived", 0) / episodes if episodes else None
                ),
                "cohorts": {
                    "discovery": discovery,
                    "validation": validation,
                },
            })
            row.update(stats)
            row["classification"] = _classification(row, stats, validation)
            row["profitable_capacity"] = (
                row.get("target_size")
                if row["classification"] in (
                    "OUT_OF_SAMPLE_VALIDATED", "RESEARCH_CANDIDATE",
                    "MAKER_RESEARCH_CANDIDATE",
                )
                else None
            )
            patterns.append(row)
        patterns.sort(key=lambda row: (
            row["classification"] not in (
                "OUT_OF_SAMPLE_VALIDATED", "RESEARCH_CANDIDATE",
                "MAKER_RESEARCH_CANDIDATE",
            ),
            -row["distinct_close_windows"],
            -row["independent_episodes"],
        ))
        counterfactual_patterns = []
        for raw in self.state.get("counterfactual_patterns", {}).values():
            windows = len(raw.get("close_windows", []))
            counterfactual_patterns.append({
                key: raw.get(key) for key in (
                    "strategy", "asset", "timeframe", "target_size", "delay_ms",
                    "observations", "qualified_observations", "independent_episodes",
                )
            } | {
                "distinct_close_windows": windows,
                "classification": "COUNTERFACTUAL_ONLY",
                "median_post_cost_profit": _percentile(raw.get("profits", []), 0.5),
                "median_expected_execution_value": _percentile(
                    raw.get("expected_values", []), 0.5,
                ),
            })
        counterfactual_patterns.sort(key=lambda row: (
            row["classification"] != "RESEARCH_CANDIDATE",
            -row["distinct_close_windows"], -row["independent_episodes"],
            row.get("delay_ms") or 0,
        ))
        repeatable = any(row["classification"] in (
            "OUT_OF_SAMPLE_VALIDATED", "RESEARCH_CANDIDATE",
            "MAKER_RESEARCH_CANDIDATE",
        ) for row in patterns)
        return {
            "funnels": self.state["funnels"],
            "repeatable_patterns": patterns,
            "counterfactual_patterns": counterfactual_patterns,
            "semantics": "RESEARCH_ONLY_NOT_ORDERS_OR_PNL",
            "no_repeatable_arbitrage": not repeatable,
            "conclusion": (
                "REPEATABLE ARBITRAGE CANDIDATE FOUND"
                if repeatable else "NO REPEATABLE ARBITRAGE FOUND"
            ),
        }
