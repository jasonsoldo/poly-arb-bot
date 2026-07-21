"""Shadow state machine for the maker-side paired arbitrage strategy.

Strategy: ``maker_paired_accumulate`` (4th independent strategy, see
``docs/plans/2026-07-20-maker-paired-arb-design.md`` and
``docs/phase0-maker-parameter-research.md``).

Pure library module: no I/O, no runtime wiring, no real orders. Every code
path keeps the invariants ``real_order_submissions = 0``, ``real_orders = 0``,
``real_fills = 0``. Integration (cli.py / web_monitor.py / C++ bridge) is owned
by a separate phase.
"""
import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

STRATEGY = "maker_paired_accumulate"
CONFIG_VERSION = "maker-accumulate-v1"

# ---------------------------------------------------------------------------
# States (design §4.1)
# ---------------------------------------------------------------------------
IDLE = "IDLE"
LEG1_WORKING = "LEG1_WORKING"
LEG1_FILLED = "LEG1_FILLED"
LEG2_WORKING = "LEG2_WORKING"
COMPLETE = "COMPLETE"
LEG1_CANCELLED = "LEG1_CANCELLED"
HEDGING_DIRECTIONAL_EXIT = "HEDGING_DIRECTIONAL_EXIT"
EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"
CLOSED_WITH_LOSS = "CLOSED_WITH_LOSS"

TERMINAL_STATES = frozenset({COMPLETE, LEG1_CANCELLED, CLOSED_WITH_LOSS})
HOLDING_STATES = frozenset({LEG1_FILLED, LEG2_WORKING, HEDGING_DIRECTIONAL_EXIT})
ACTIVE_STATES = frozenset({LEG1_WORKING, LEG1_FILLED, LEG2_WORKING,
                           HEDGING_DIRECTIONAL_EXIT, EMERGENCY_FLATTEN})

# Exit paths (design §9.6 ``exit_path``)
EXIT_MAKER_COMPLETE = "maker_complete"
EXIT_TAKER_HEDGE = "taker_hedge"
EXIT_DIRECTIONAL = "directional_exit"
EXIT_EMERGENCY = "emergency_flatten"

# Fill simulation modes (design §7.2)
FILL_STRICT = "strict"
FILL_QUEUE = "queue"  # configured_queue_model, NOT a historical fill rate

# ---------------------------------------------------------------------------
# REJECT / terminal reason enums (design §2.5, §3.4)
# ---------------------------------------------------------------------------
LEG1_REJECT_REASONS = frozenset({
    "books_not_ready",
    "waiting_up_snapshot",
    "waiting_down_snapshot",
    "up_book_stale",
    "down_book_stale",
    "books_not_synced",
    "fee_schedule_unavailable",
    "market_not_tradable",
    "market_expired",
    "outside_time_window",
    "episode_already_active",
    "leg1_no_improve_room",
    "leg1_queue_too_deep",
    "leg1_extremity_exceeded",
    "expected_margin_below_threshold",
    "hedge_exit_margin_below_threshold",
    "orphan_loss_estimate_exceeded",
    "book_depth_insufficient",
    "portfolio_exposure_exceeded",
    "daily_loss_limit_reached",
    "orphan_circuit_breaker_open",
    "clock_skew_exceeded",
    "clock_skew_unavailable",
    "max_episodes_per_market_reached",
})

LEG2_TERMINAL_REASONS = frozenset({
    "leg2_max_price_below_bid",
    "leg2_improves_exhausted",
    "leg2_timeout",
    "hedge_margin_below_threshold",
    "directional_exit_timeout",
    "emergency_flatten_window",
    "market_expired_mid_episode",
    "books_lost_mid_episode",
    "fee_schedule_lost_mid_episode",
    "leg1_timeout",
    "orphan_seconds_exceeded",
    "orphan_loss_limit_exceeded",
})

# Internal guards (not part of the design enums; used when dropping late
# messages from an old generation/session, see design §4.4).
GUARD_REASONS = frozenset({"stale_message_dropped"})

# ---------------------------------------------------------------------------
# Phase 0 measured defaults (docs/phase0-maker-parameter-research.md §6)
# ---------------------------------------------------------------------------
DEFAULT_LEG1_TIMEOUT_SECONDS = {"5m": 60.0, "15m": 120.0, "1h": 300.0, "4h": 300.0}
DEFAULT_LEG2_IMPROVE_INTERVAL_MS = {"5m": 1500.0, "15m": 2000.0, "1h": 4000.0, "4h": 4000.0}
DEFAULT_LEG2_TIMEOUT_SECONDS = {"5m": 45.0, "15m": 180.0, "1h": 300.0, "4h": 300.0}
DEFAULT_LEG2_IMPROVE_STEP_TICKS = {"5m": 2, "15m": 1, "1h": 1, "4h": 1}
DEFAULT_MAX_ORPHAN_SECONDS = {"5m": 90.0, "15m": 240.0, "1h": 360.0, "4h": 600.0}
DEFAULT_FORCE_FLATTEN_SECONDS = {"5m": 240.0, "15m": 600.0, "1h": 900.0, "4h": 1500.0}
DEFAULT_WINDOW_MIN_SECONDS = {"5m": 20.0, "15m": 20.0, "1h": 20.0, "4h": 20.0}
DEFAULT_WINDOW_MAX_SECONDS = {"5m": 300.0, "15m": 900.0, "1h": 3600.0, "4h": 7200.0}
# Per-timeframe book-age limits, calibrated from measured book update intervals
# (Phase 0 collection + 6h VPS validation): 5m p50=133ms / 15m p50=1.07s /
# 1h p50=2.6s / 4h p50=35s. A single global threshold is structurally too
# tight for long timeframes (46-47% of evaluations rejected as *_book_stale).
DEFAULT_MAX_BOOK_AGE_MS = {"5m": 2000.0, "15m": 5000.0, "1h": 10000.0, "4h": 60000.0}

_EPS = 1e-9


def _round_price(price):
    """Prices are tick-quantized; strip float dust from quote arithmetic."""
    return round(price, 5)


def _env_tf_dict(prefix, defaults, cast):
    values = {}
    for timeframe, default in defaults.items():
        raw = os.getenv(f"{prefix}_{timeframe.upper()}")
        values[timeframe] = cast(raw) if raw is not None else default
    return values


def _tf(values, timeframe):
    return values.get(timeframe, values.get("4h"))


def taker_fee_total(price, size, fee_rate):
    """Official taker fee with C++ parity rounding (round half up at 1e-5)."""
    if price is None or size <= 0 or fee_rate <= 0:
        return 0.0
    raw = size * fee_rate * price * (1.0 - price)
    return math.floor(raw * 1e5 + 0.5) / 1e5


def taker_fee_per_share(price, fee_rate):
    if price is None or fee_rate <= 0:
        return 0.0
    return fee_rate * price * (1.0 - price)


# ---------------------------------------------------------------------------
# Configuration (design §6, AGENTS.md §26: every threshold explicit)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MakerAccumulateConfig:
    min_tick: float = 0.01
    buffer_per_share: float = 0.005
    gas_cost_per_share: float = 0.0001
    min_expected_locked_margin: float = 0.005
    min_hedge_exit_margin: float = -0.01
    min_realized_margin: float = 0.005
    default_taker_fee_rate: float = 0.07
    rebate_share_ratio: float = 0.20
    estimated_liquidity_reward_per_share: float = 0.0
    shadow_fill_mode: str = FILL_STRICT
    # leg1 quoting (design §2.2-§2.3)
    leg1_quote_mode: str = "improve"
    max_spread_to_join: float = 0.01
    leg1_max_improve_cost_per_share: float = 0.01
    leg1_min_fill_probability: float = 0.20
    leg1_max_extremity: float = 0.45
    leg1_side_min_score_gap: float = 0.05
    side_weight_extremity: float = 1.0
    side_weight_imbalance: float = 0.5
    side_weight_rebate: float = 0.1
    assumed_sell_flow_shares_per_minute: float = 50.0  # configured_fill_model prior
    # per-timeframe dicts
    leg1_timeout_seconds: dict = field(default_factory=lambda: dict(DEFAULT_LEG1_TIMEOUT_SECONDS))
    leg2_improve_interval_ms: dict = field(default_factory=lambda: dict(DEFAULT_LEG2_IMPROVE_INTERVAL_MS))
    leg2_timeout_seconds: dict = field(default_factory=lambda: dict(DEFAULT_LEG2_TIMEOUT_SECONDS))
    leg2_improve_step_ticks: dict = field(default_factory=lambda: dict(DEFAULT_LEG2_IMPROVE_STEP_TICKS))
    max_orphan_seconds: dict = field(default_factory=lambda: dict(DEFAULT_MAX_ORPHAN_SECONDS))
    force_flatten_seconds: dict = field(default_factory=lambda: dict(DEFAULT_FORCE_FLATTEN_SECONDS))
    window_min_seconds: dict = field(default_factory=lambda: dict(DEFAULT_WINDOW_MIN_SECONDS))
    window_max_seconds: dict = field(default_factory=lambda: dict(DEFAULT_WINDOW_MAX_SECONDS))
    leg2_max_improves: int = 5
    leg1_min_fill_ratio: float = 0.5
    # orphan / exit risk (design §4.3)
    max_orphan_loss_usd: float = 1.0
    max_orphan_giveback_per_share: float = 0.03
    directional_exit_timeout_seconds: float = 60.0
    # book / clock health (max_book_age_ms is per-timeframe, see DEFAULT_MAX_BOOK_AGE_MS)
    max_book_age_ms: dict = field(default_factory=lambda: dict(DEFAULT_MAX_BOOK_AGE_MS))
    max_book_skew_ms: float = 250.0
    max_clock_skew_ms: float = 250.0
    # portfolio risk (design §6, independent maker_accumulate_* block)
    max_order_size: float = 25.0
    min_book_depth: float = 10.0
    max_notional_per_market: float = 25.0
    max_total_exposure: float = 100.0
    max_at_risk_exposure: float = 50.0
    max_daily_loss: float = 5.0
    max_consecutive_orphans: int = 3
    circuit_cooldown_seconds: float = 3600.0
    max_episodes_per_market_window: int = 3

    def __post_init__(self):
        if self.shadow_fill_mode not in {FILL_STRICT, FILL_QUEUE}:
            raise ValueError(f"invalid shadow_fill_mode: {self.shadow_fill_mode}")
        if self.leg1_quote_mode not in {"improve", "join"}:
            raise ValueError(f"invalid leg1_quote_mode: {self.leg1_quote_mode}")
        for timeframe in DEFAULT_FORCE_FLATTEN_SECONDS:
            flatten = _tf(self.force_flatten_seconds, timeframe)
            orphan = _tf(self.max_orphan_seconds, timeframe)
            leg2 = _tf(self.leg2_timeout_seconds, timeframe)
            if flatten <= orphan + leg2:
                raise ValueError(
                    f"force_flatten_seconds[{timeframe}]={flatten} must exceed "
                    f"max_orphan_seconds + leg2_timeout_seconds ({orphan + leg2})"
                )

    @classmethod
    def from_env(cls):
        # Global MAKER_ACCUMULATE_MAX_BOOK_AGE_MS is a fallback base for every
        # timeframe; per-timeframe MAKER_ACCUMULATE_MAX_BOOK_AGE_MS_{5M,15M,1H,4H}
        # overrides win (same pattern as the other per-timeframe dicts).
        book_age_defaults = dict(DEFAULT_MAX_BOOK_AGE_MS)
        global_book_age = os.getenv("MAKER_ACCUMULATE_MAX_BOOK_AGE_MS")
        if global_book_age is not None:
            book_age_defaults = {tf: float(global_book_age) for tf in book_age_defaults}
        return cls(
            min_tick=float(os.getenv("MAKER_ACCUMULATE_MIN_TICK", "0.01")),
            buffer_per_share=float(os.getenv("MAKER_ACCUMULATE_BUFFER_PER_SHARE", "0.005")),
            gas_cost_per_share=float(os.getenv("MAKER_ACCUMULATE_GAS_COST_PER_SHARE", "0.0001")),
            min_expected_locked_margin=float(os.getenv("MAKER_ACCUMULATE_MIN_EXPECTED_LOCKED_MARGIN", "0.005")),
            min_hedge_exit_margin=float(os.getenv("MAKER_ACCUMULATE_MIN_HEDGE_EXIT_MARGIN", "-0.01")),
            min_realized_margin=float(os.getenv("MAKER_ACCUMULATE_MIN_REALIZED_MARGIN", "0.005")),
            default_taker_fee_rate=float(os.getenv("MAKER_ACCUMULATE_DEFAULT_TAKER_FEE_RATE", "0.07")),
            rebate_share_ratio=float(os.getenv("MAKER_ACCUMULATE_REBATE_SHARE_RATIO", "0.20")),
            estimated_liquidity_reward_per_share=float(os.getenv("MAKER_ACCUMULATE_ESTIMATED_LIQUIDITY_REWARD_PER_SHARE", "0")),
            shadow_fill_mode=os.getenv("MAKER_ACCUMULATE_SHADOW_FILL_MODE", FILL_STRICT),
            leg1_quote_mode=os.getenv("MAKER_ACCUMULATE_LEG1_QUOTE_MODE", "improve"),
            max_spread_to_join=float(os.getenv("MAKER_ACCUMULATE_MAX_SPREAD_TO_JOIN", "0.01")),
            leg1_max_improve_cost_per_share=float(os.getenv("MAKER_ACCUMULATE_LEG1_MAX_IMPROVE_COST_PER_SHARE", "0.01")),
            leg1_min_fill_probability=float(os.getenv("MAKER_ACCUMULATE_LEG1_MIN_FILL_PROBABILITY", "0.20")),
            leg1_max_extremity=float(os.getenv("MAKER_ACCUMULATE_LEG1_MAX_EXTREMITY", "0.45")),
            leg1_side_min_score_gap=float(os.getenv("MAKER_ACCUMULATE_LEG1_SIDE_MIN_SCORE_GAP", "0.05")),
            side_weight_extremity=float(os.getenv("MAKER_ACCUMULATE_SIDE_WEIGHT_EXTREMITY", "1.0")),
            side_weight_imbalance=float(os.getenv("MAKER_ACCUMULATE_SIDE_WEIGHT_IMBALANCE", "0.5")),
            side_weight_rebate=float(os.getenv("MAKER_ACCUMULATE_SIDE_WEIGHT_REBATE", "0.1")),
            assumed_sell_flow_shares_per_minute=float(os.getenv("MAKER_ACCUMULATE_ASSUMED_SELL_FLOW_PER_MINUTE", "50")),
            leg1_timeout_seconds=_env_tf_dict("MAKER_ACCUMULATE_LEG1_TIMEOUT", DEFAULT_LEG1_TIMEOUT_SECONDS, float),
            leg2_improve_interval_ms=_env_tf_dict("MAKER_ACCUMULATE_LEG2_IMPROVE_INTERVAL_MS", DEFAULT_LEG2_IMPROVE_INTERVAL_MS, float),
            leg2_timeout_seconds=_env_tf_dict("MAKER_ACCUMULATE_LEG2_TIMEOUT", DEFAULT_LEG2_TIMEOUT_SECONDS, float),
            leg2_improve_step_ticks=_env_tf_dict("MAKER_ACCUMULATE_LEG2_IMPROVE_STEP_TICKS", DEFAULT_LEG2_IMPROVE_STEP_TICKS, int),
            max_orphan_seconds=_env_tf_dict("MAKER_ACCUMULATE_MAX_ORPHAN", DEFAULT_MAX_ORPHAN_SECONDS, float),
            force_flatten_seconds=_env_tf_dict("MAKER_ACCUMULATE_FORCE_FLATTEN", DEFAULT_FORCE_FLATTEN_SECONDS, float),
            window_min_seconds=_env_tf_dict("MAKER_ACCUMULATE_WINDOW_MIN", DEFAULT_WINDOW_MIN_SECONDS, float),
            window_max_seconds=_env_tf_dict("MAKER_ACCUMULATE_WINDOW_MAX", DEFAULT_WINDOW_MAX_SECONDS, float),
            leg2_max_improves=int(os.getenv("MAKER_ACCUMULATE_LEG2_MAX_IMPROVES", "5")),
            leg1_min_fill_ratio=float(os.getenv("MAKER_ACCUMULATE_LEG1_MIN_FILL_RATIO", "0.5")),
            max_orphan_loss_usd=float(os.getenv("MAKER_ACCUMULATE_MAX_ORPHAN_LOSS_USD", "1.0")),
            max_orphan_giveback_per_share=float(os.getenv("MAKER_ACCUMULATE_MAX_ORPHAN_GIVEBACK_PER_SHARE", "0.03")),
            directional_exit_timeout_seconds=float(os.getenv("MAKER_ACCUMULATE_DIRECTIONAL_EXIT_TIMEOUT", "60")),
            max_book_age_ms=_env_tf_dict("MAKER_ACCUMULATE_MAX_BOOK_AGE_MS", book_age_defaults, float),
            max_book_skew_ms=float(os.getenv("MAKER_ACCUMULATE_MAX_BOOK_SKEW_MS", "250")),
            max_clock_skew_ms=float(os.getenv("MAKER_ACCUMULATE_MAX_CLOCK_SKEW_MS", "250")),
            max_order_size=float(os.getenv("MAKER_ACCUMULATE_MAX_ORDER_SIZE", "25")),
            min_book_depth=float(os.getenv("MAKER_ACCUMULATE_MIN_BOOK_DEPTH", "10")),
            max_notional_per_market=float(os.getenv("MAKER_ACCUMULATE_MAX_NOTIONAL_PER_MARKET", "25")),
            max_total_exposure=float(os.getenv("MAKER_ACCUMULATE_MAX_TOTAL_EXPOSURE", "100")),
            max_at_risk_exposure=float(os.getenv("MAKER_ACCUMULATE_MAX_AT_RISK_EXPOSURE", "50")),
            max_daily_loss=float(os.getenv("MAKER_ACCUMULATE_MAX_DAILY_LOSS", "5")),
            max_consecutive_orphans=int(os.getenv("MAKER_ACCUMULATE_MAX_CONSECUTIVE_ORPHANS", "3")),
            circuit_cooldown_seconds=float(os.getenv("MAKER_ACCUMULATE_CIRCUIT_COOLDOWN_SECONDS", "3600")),
            max_episodes_per_market_window=int(os.getenv("MAKER_ACCUMULATE_MAX_EPISODES_PER_MARKET_WINDOW", "3")),
        )

    def config_hash(self):
        payload = asdict(self)
        payload["config_version"] = CONFIG_VERSION
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


# ---------------------------------------------------------------------------
# Inputs (design §1.2: independent MakerAccumulateInput)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MakerBookSide:
    best_bid: float
    best_ask: float
    best_bid_size: float
    best_ask_size: float
    bid_depth_total: float
    ask_depth_total: float
    bid_depth_at_improve_level: float
    age_ms: float
    snapshot_received: bool = True

    @property
    def midpoint(self):
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self):
        return self.best_ask - self.best_bid

    @property
    def imbalance(self):
        total = self.bid_depth_total + self.ask_depth_total
        if total <= 0:
            return 0.0
        return (self.bid_depth_total - self.ask_depth_total) / total


@dataclass(frozen=True)
class MakerAccumulateInput:
    market_id: str
    condition_id: str
    asset: str
    timeframe: str
    window: str
    generation: int
    session: str
    evaluation_sequence: int
    timestamp: float
    up: MakerBookSide
    down: MakerBookSide
    book_skew_ms: float
    seconds_to_close: float
    market_active: bool
    market_tradable: bool
    fee_schedule_available: bool
    taker_fee_rate: Optional[float] = None
    clock_skew_ms: Optional[float] = 0.0
    reference: object = None  # REFERENCE ONLY, NOT USED FOR ACCEPTANCE
    min_order_size: float = 5.0
    # Provenance of the book view (e.g. which fields are real C++ WS values and
    # which are documented derivations). Recorded verbatim in audit events.
    book_view_basis: Optional[str] = None
    # Optional real-book VWAP simulators for taker hedge/exit legs.
    ask_vwap: Optional[Callable[[str, float], Optional[float]]] = None
    bid_vwap: Optional[Callable[[str, float], Optional[float]]] = None


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MakerDecision:
    strategy: str
    decision: str
    reason: str
    state: str = IDLE
    episode_id: Optional[str] = None
    leg1_outcome: Optional[str] = None
    leg1_quote_price: Optional[float] = None
    leg1_quote_mode: Optional[str] = None
    leg1_order_size: Optional[float] = None
    expected_margin: Optional[float] = None
    hedge_exit_margin: Optional[float] = None
    orphan_loss_estimate: Optional[float] = None
    estimated_fill_probability: Optional[float] = None
    side_selection_score_gap: Optional[float] = None
    completed: bool = False
    real_order_submissions: int = 0
    blocking_reasons: tuple = ()


@dataclass(frozen=True)
class MakerEvaluation:
    decision: MakerDecision
    events: tuple = ()


@dataclass(frozen=True)
class PortfolioView:
    total_exposure: float = 0.0
    at_risk_exposure: float = 0.0
    daily_loss: float = 0.0
    circuit_open: bool = False
    episodes_in_market: int = 0


def _append_reason(reasons, reason):
    if reason and reason not in reasons:
        reasons.append(reason)


def _fee_rate(row, config):
    if row.taker_fee_rate is not None and row.taker_fee_rate > 0:
        return row.taker_fee_rate
    return config.default_taker_fee_rate


def configured_fill_probability(queue_depth_ahead, timeout_seconds, config):
    """Configured fill model prior (NOT a historical fill rate)."""
    expected_flow = config.assumed_sell_flow_shares_per_minute * max(timeout_seconds, 0.0) / 60.0
    return min(1.0, expected_flow / max(queue_depth_ahead, 1.0))


def select_leg1_side(row, config):
    """Design §2.2 scoring: extremity + ask-side imbalance + rebate tie-break."""
    scores = {}
    for outcome, side in (("Up", row.up), ("Down", row.down)):
        extremity = abs(side.midpoint - 0.5)
        if extremity > config.leg1_max_extremity + _EPS:
            continue
        scores[outcome] = (
            config.side_weight_extremity * extremity
            + config.side_weight_imbalance * (-side.imbalance)
            + config.side_weight_rebate * side.best_bid * (1.0 - side.best_bid)
        )
    if not scores:
        return None, scores, None
    if len(scores) == 1:
        return next(iter(scores)), scores, None
    gap = abs(scores["Up"] - scores["Down"])
    if gap < config.leg1_side_min_score_gap:
        # tie-break: deeper best-bid queue is more stable
        outcome = "Up" if row.up.best_bid_size >= row.down.best_bid_size else "Down"
    else:
        outcome = max(scores, key=scores.get)
    return outcome, scores, gap


def leg1_quote(side, config):
    """Design §2.3: never cross the spread; improve one tick when there is room."""
    tick = config.min_tick
    spread = side.best_ask - side.best_bid
    if spread <= config.max_spread_to_join + _EPS:
        return _round_price(side.best_bid), "join", side.best_bid_size, None
    if side.best_bid + tick <= side.best_ask - tick + _EPS and tick <= config.leg1_max_improve_cost_per_share + _EPS:
        return _round_price(side.best_bid + tick), "improve", 0.0, None
    return None, None, None, "leg1_no_improve_room"


def leg2_max_price(leg1_avg_price, config):
    """Design §3.1 dynamic cap: leg2 may never lock a loss."""
    return 1.0 - leg1_avg_price - config.buffer_per_share - config.min_realized_margin


def evaluate_maker_accumulate(row, config=None, portfolio=None):
    """Pure leg1 open evaluation (design §2.1-§2.5). Style-compatible with
    ``ev_strategies.evaluate_directional``: returns decision + reason +
    blocking_reasons; real_order_submissions is always 0."""
    config = config or MakerAccumulateConfig.from_env()
    portfolio = portfolio or PortfolioView()
    reasons = []

    if row.clock_skew_ms is None:
        _append_reason(reasons, "clock_skew_unavailable")
    elif abs(row.clock_skew_ms) > config.max_clock_skew_ms:
        _append_reason(reasons, "clock_skew_exceeded")
    if not row.up.snapshot_received and not row.down.snapshot_received:
        _append_reason(reasons, "books_not_ready")
    elif not row.up.snapshot_received:
        _append_reason(reasons, "waiting_up_snapshot")
    elif not row.down.snapshot_received:
        _append_reason(reasons, "waiting_down_snapshot")
    max_book_age_ms = _tf(config.max_book_age_ms, row.timeframe)
    if row.up.snapshot_received and row.up.age_ms > max_book_age_ms:
        _append_reason(reasons, "up_book_stale")
    if row.down.snapshot_received and row.down.age_ms > max_book_age_ms:
        _append_reason(reasons, "down_book_stale")
    if row.book_skew_ms > config.max_book_skew_ms:
        _append_reason(reasons, "books_not_synced")
    if not row.fee_schedule_available:
        _append_reason(reasons, "fee_schedule_unavailable")
    if not row.market_active or not row.market_tradable:
        _append_reason(reasons, "market_not_tradable")
    if row.seconds_to_close <= 0:
        _append_reason(reasons, "market_expired")
    window_min = _tf(config.window_min_seconds, row.timeframe)
    window_max = _tf(config.window_max_seconds, row.timeframe)
    force_flatten = _tf(config.force_flatten_seconds, row.timeframe)
    if row.seconds_to_close > 0 and not (
        window_min <= row.seconds_to_close <= window_max
        and row.seconds_to_close > force_flatten
    ):
        _append_reason(reasons, "outside_time_window")
    if portfolio.circuit_open:
        _append_reason(reasons, "orphan_circuit_breaker_open")
    if portfolio.daily_loss >= config.max_daily_loss:
        _append_reason(reasons, "daily_loss_limit_reached")
    if portfolio.episodes_in_market >= config.max_episodes_per_market_window:
        _append_reason(reasons, "max_episodes_per_market_reached")

    outcome, scores, gap = select_leg1_side(row, config)
    quote_price = quote_mode = None
    queue_ahead = 0.0
    fill_probability = None
    expected_margin = hedge_margin = orphan_estimate = order_size = None
    if outcome is None:
        _append_reason(reasons, "leg1_extremity_exceeded")
    else:
        side = row.up if outcome == "Up" else row.down
        other = row.down if outcome == "Up" else row.up
        quote_price, quote_mode, queue_ahead, quote_reason = leg1_quote(side, config)
        if quote_reason:
            _append_reason(reasons, quote_reason)
        else:
            if quote_mode == "join":
                fill_probability = configured_fill_probability(
                    queue_ahead, _tf(config.leg1_timeout_seconds, row.timeframe), config)
                if fill_probability < config.leg1_min_fill_probability:
                    _append_reason(reasons, "leg1_queue_too_deep")
            depth = side.best_bid_size if quote_mode == "join" else side.bid_depth_at_improve_level
            if depth < config.min_book_depth:
                _append_reason(reasons, "book_depth_insufficient")
            order_size = config.max_order_size
            if config.max_notional_per_market > 0:
                order_size = min(order_size, config.max_notional_per_market / max(quote_price, config.min_tick))
            order_size = math.floor(order_size * 1e5) / 1e5
            if order_size < row.min_order_size:
                _append_reason(reasons, "book_depth_insufficient")
            rate = _fee_rate(row, config)
            hedge_fee = taker_fee_per_share(other.best_ask, rate)
            expected_margin = 1.0 - (quote_price + other.best_bid + config.buffer_per_share)
            hedge_margin = 1.0 - (quote_price + other.best_ask + hedge_fee + config.buffer_per_share)
            orphan_estimate = order_size * config.max_orphan_giveback_per_share
            if expected_margin < config.min_expected_locked_margin:
                _append_reason(reasons, "expected_margin_below_threshold")
            if hedge_margin < config.min_hedge_exit_margin:
                _append_reason(reasons, "hedge_exit_margin_below_threshold")
            if orphan_estimate > config.max_orphan_loss_usd:
                _append_reason(reasons, "orphan_loss_estimate_exceeded")
            new_notional = order_size * quote_price
            if (portfolio.total_exposure + new_notional > config.max_total_exposure
                    or portfolio.at_risk_exposure + new_notional > config.max_at_risk_exposure):
                _append_reason(reasons, "portfolio_exposure_exceeded")

    return MakerDecision(
        strategy=STRATEGY,
        decision="REJECT" if reasons else "ACCEPT",
        reason=reasons[0] if reasons else "maker_pair_margins_pass",
        leg1_outcome=outcome,
        leg1_quote_price=quote_price,
        leg1_quote_mode=quote_mode,
        leg1_order_size=order_size,
        expected_margin=expected_margin,
        hedge_exit_margin=hedge_margin,
        orphan_loss_estimate=orphan_estimate,
        estimated_fill_probability=fill_probability,
        side_selection_score_gap=gap,
        blocking_reasons=tuple(reasons),
    )


# ---------------------------------------------------------------------------
# Episode + state machine (design §4)
# ---------------------------------------------------------------------------
@dataclass
class MakerEpisode:
    episode_id: str
    episode_sequence: int
    market_id: str
    condition_id: str
    asset: str
    timeframe: str
    window: str
    generation: int
    session: str
    state: str = IDLE
    opened_ts: float = 0.0
    leg1_outcome: str = "Up"
    leg2_outcome: str = "Down"
    leg1_quote_price: float = 0.0
    leg1_quote_mode: str = "improve"
    leg1_order_size: float = 0.0
    leg1_placed_ts: float = 0.0
    leg1_filled_size: float = 0.0
    leg1_avg_price: float = 0.0
    leg1_fill_ts: Optional[float] = None
    leg1_estimated_fill_probability: Optional[float] = None
    leg1_queue_depth_ahead: float = 0.0
    side_selection_score_gap: Optional[float] = None
    expected_margin: Optional[float] = None
    hedge_exit_margin: Optional[float] = None
    orphan_loss_estimate: Optional[float] = None
    leg2_quote_price: Optional[float] = None
    leg2_max_price: Optional[float] = None
    leg2_filled_size: float = 0.0
    leg2_avg_price: float = 0.0
    leg2_start_ts: Optional[float] = None
    leg2_improve_attempt: int = 0
    leg2_last_improve_ts: Optional[float] = None
    leg2_hedge_taker_fee: float = 0.0
    exit_path: Optional[str] = None
    exit_quote_price: Optional[float] = None
    exit_start_ts: Optional[float] = None
    exit_vwap: Optional[float] = None
    exit_taker_fee: float = 0.0
    terminal_reason: Optional[str] = None
    forced_exit_reason: Optional[str] = None
    orphan_max_drawdown: float = 0.0
    episode_realized_pnl: float = 0.0
    event_sequence: int = 0

    @property
    def locked_size(self):
        return min(self.leg1_filled_size, self.leg2_filled_size)

    @property
    def at_risk_size(self):
        return abs(self.leg1_filled_size - self.leg2_filled_size)

    @property
    def holding(self):
        return self.leg1_filled_size > self.leg2_filled_size


class MakerAccumulateStateMachine:
    """Shadow orphan-leg state machine (design §4). Pure in-memory; callers
    persist the returned audit events. No real-order semantics anywhere."""

    def __init__(self, config=None):
        self.config = config or MakerAccumulateConfig.from_env()
        self.config_version = CONFIG_VERSION
        self.config_hash = self.config.config_hash()
        self.episodes = {}            # condition_id -> active MakerEpisode
        self.completed = []           # terminal records
        self.consecutive_orphans = 0
        self.circuit_open_until = None
        self._episode_sequences = {}  # binding -> count
        self._episodes_per_market = {}
        self._last_binding = {}       # market_id -> (generation, session)
        self._seen_bindings = {}      # market_id -> set of known bindings
        self._last_reject_reason = {} # condition_id -> reason (dedup)
        self.real_order_submissions = 0
        self.real_orders = 0
        self.real_fills = 0

    # -- identity -----------------------------------------------------------
    def _next_episode_id(self, row):
        binding = (row.market_id, row.condition_id, row.generation, row.session)
        sequence = self._episode_sequences.get(binding, 0) + 1
        self._episode_sequences[binding] = sequence
        identity = "|".join(map(str, (*binding, sequence)))
        return f"maker-episode:{hashlib.sha256(identity.encode()).hexdigest()}", sequence

    @staticmethod
    def _reject_event_id(row):
        identity = "|".join(map(str, (row.market_id, row.condition_id, row.generation,
                                      row.session, row.evaluation_sequence)))
        return f"maker-rejected:{hashlib.sha256(identity.encode()).hexdigest()}"

    # -- audit ---------------------------------------------------------------
    def _base_event(self, row, event_type, episode, decision, reason):
        if episode is not None:
            episode.event_sequence += 1
            event_id = f"{episode.episode_id}:{event_type}:{episode.event_sequence}"
            episode_id = episode.episode_id
        else:
            event_id = self._reject_event_id(row)
            episode_id = None
        max_book_age_ms = _tf(self.config.max_book_age_ms, row.timeframe)
        event = {
            "event_id": event_id,
            "event_type": event_type,
            "strategy": STRATEGY,
            "episode_id": episode_id,
            "market_id": row.market_id,
            "condition_id": row.condition_id,
            "asset": row.asset,
            "timeframe": row.timeframe,
            "window": row.window,
            "generation": row.generation,
            "session": row.session,
            "evaluation_sequence": row.evaluation_sequence,
            "timestamp": row.timestamp,
            "ts": row.timestamp,
            "decision": decision,
            "reason": reason,
            "books_ready": row.up.snapshot_received and row.down.snapshot_received,
            "books_fresh": (row.up.age_ms <= max_book_age_ms
                            and row.down.age_ms <= max_book_age_ms),
            "books_synced": row.book_skew_ms <= self.config.max_book_skew_ms,
            "seconds_to_close": row.seconds_to_close,
            "clock_skew_ms": row.clock_skew_ms,
            "config_version": self.config_version,
            "config_hash": self.config_hash,
            "shadow_fill_mode": self.config.shadow_fill_mode,
            "real_order_submissions": 0,
            "real_orders": 0,
            "real_fills": 0,
        }
        reference = row.reference
        event["fast_price"] = getattr(reference, "fast_price", None)
        event["consensus_price"] = getattr(reference, "consensus_price", None)
        event["settlement_reference"] = getattr(reference, "settlement_reference", None)
        event["reference_state"] = getattr(reference, "reference_state", None)
        event["reference_usage"] = "REFERENCE ONLY, NOT USED FOR MAKER-ACCUMULATE ACCEPTANCE"
        if row.book_view_basis:
            event["book_view_basis"] = row.book_view_basis
        return event

    def _emit(self, events, row, event_type, episode, decision, reason, extra=None):
        event = self._base_event(row, event_type, episode, decision, reason)
        if extra:
            event.update(extra)
        events.append(event)
        return event

    def _state_change(self, events, row, episode, from_state, to_state, reason):
        episode.state = to_state
        self._emit(events, row, "maker_episode_state_change", episode,
                   "STATE_CHANGE", reason,
                   {"state_from": from_state, "state_to": to_state,
                    "locked_size": episode.locked_size, "at_risk_size": episode.at_risk_size})

    # -- fill simulation (design §7.2) ----------------------------------------
    @staticmethod
    def _buy_fill(mode, quote_price, best_ask):
        strict = best_ask < quote_price - _EPS
        queue = best_ask <= quote_price + _EPS
        filled = strict if mode == FILL_STRICT else queue
        return filled, strict, queue

    @staticmethod
    def _sell_fill(mode, quote_price, best_bid):
        strict = best_bid > quote_price + _EPS
        queue = best_bid >= quote_price - _EPS
        filled = strict if mode == FILL_STRICT else queue
        return filled, strict, queue

    def _books_evaluable(self, row):
        max_book_age_ms = _tf(self.config.max_book_age_ms, row.timeframe)
        return (row.up.snapshot_received and row.down.snapshot_received
                and row.up.age_ms <= max_book_age_ms
                and row.down.age_ms <= max_book_age_ms)

    def _side(self, row, outcome):
        return row.up if outcome == "Up" else row.down

    def _vwap(self, row, outcome, side, size, fallback):
        fn = row.ask_vwap if side == "ask" else row.bid_vwap
        if fn is not None:
            value = fn(outcome, size)
            if value is not None:
                return value
        return fallback

    # -- main entry ------------------------------------------------------------
    def evaluate(self, row):
        events = []
        decision = self._evaluate(row, events)
        return MakerEvaluation(decision=decision, events=tuple(events))

    def _evaluate(self, row, events):
        binding = (row.generation, row.session)
        episode = self.episodes.get(row.condition_id)
        last = self._last_binding.get(row.market_id)
        seen = self._seen_bindings.setdefault(row.market_id, set())

        # Drop late messages from an old generation/session (design §4.4).
        if binding != last and binding in seen:
            return MakerDecision(strategy=STRATEGY, decision="REJECT",
                                 reason="stale_message_dropped",
                                 state=episode.state if episode else IDLE,
                                 episode_id=episode.episode_id if episode else None)
        binding_changed = last is not None and binding != last
        seen.add(binding)
        self._last_binding[row.market_id] = binding

        if episode is not None and binding_changed \
                and binding != (episode.generation, episode.session):
            # Reconnect / resync / rotation: old-session working quotes are void.
            # New episodes may only open on a later tick of the new session.
            if episode.state == LEG1_WORKING:
                self._cancel_leg1(events, row, episode, "books_lost_mid_episode")
                return MakerDecision(strategy=STRATEGY, decision="COMPLETE",
                                     reason="books_lost_mid_episode",
                                     state=LEG1_CANCELLED,
                                     episode_id=episode.episode_id, completed=True)
            episode.forced_exit_reason = "books_lost_mid_episode"
            episode.generation, episode.session = row.generation, row.session

        if episode is None:
            return self._open_path(row, events)
        return self._episode_path(row, episode, events)

    # -- open path (IDLE -> LEG1_WORKING) --------------------------------------
    def _open_path(self, row, events):
        portfolio = self._portfolio_view(row)
        decision = evaluate_maker_accumulate(row, self.config, portfolio)
        if decision.decision == "REJECT":
            if self._last_reject_reason.get(row.condition_id) != decision.reason:
                self._last_reject_reason[row.condition_id] = decision.reason
                self._emit(events, row, "maker_episode_rejected", None,
                           "REJECT", decision.reason,
                           {"blocking_reasons": list(decision.blocking_reasons),
                            "up_book_age_ms": row.up.age_ms,
                            "down_book_age_ms": row.down.age_ms})
            return decision
        self._last_reject_reason.pop(row.condition_id, None)

        episode_id, sequence = self._next_episode_id(row)
        leg2_outcome = "Down" if decision.leg1_outcome == "Up" else "Up"
        episode = MakerEpisode(
            episode_id=episode_id, episode_sequence=sequence,
            market_id=row.market_id, condition_id=row.condition_id,
            asset=row.asset, timeframe=row.timeframe, window=row.window,
            generation=row.generation, session=row.session,
            state=LEG1_WORKING, opened_ts=row.timestamp,
            leg1_outcome=decision.leg1_outcome, leg2_outcome=leg2_outcome,
            leg1_quote_price=decision.leg1_quote_price,
            leg1_quote_mode=decision.leg1_quote_mode,
            leg1_order_size=decision.leg1_order_size,
            leg1_placed_ts=row.timestamp,
            leg1_estimated_fill_probability=decision.estimated_fill_probability,
            side_selection_score_gap=decision.side_selection_score_gap,
            expected_margin=decision.expected_margin,
            hedge_exit_margin=decision.hedge_exit_margin,
            orphan_loss_estimate=decision.orphan_loss_estimate,
        )
        side = self._side(row, episode.leg1_outcome)
        self.episodes[row.condition_id] = episode
        self._episodes_per_market[row.market_id] = \
            self._episodes_per_market.get(row.market_id, 0) + 1
        self._emit(events, row, "maker_episode_opened", episode,
                   "ACCEPT", decision.reason, {
                       "state_from": IDLE, "state_to": LEG1_WORKING,
                       "leg1_outcome": episode.leg1_outcome,
                       "leg1_quote_mode": episode.leg1_quote_mode,
                       "leg1_quote_price": episode.leg1_quote_price,
                       "leg1_order_size": episode.leg1_order_size,
                       "leg1_best_bid": side.best_bid, "leg1_best_ask": side.best_ask,
                       "leg1_midpoint": side.midpoint,
                       "leg1_book_imbalance": side.imbalance,
                       "leg1_queue_depth_ahead": episode.leg1_queue_depth_ahead,
                       "leg1_estimated_fill_probability": episode.leg1_estimated_fill_probability,
                       "fill_probability_model": "configured_fill_model",
                       "side_selection_score_gap": episode.side_selection_score_gap,
                       "expected_pair_cost": episode.leg1_quote_price
                       + self._side(row, leg2_outcome).best_bid,
                       "expected_margin": episode.expected_margin,
                       "hedge_exit_margin": episode.hedge_exit_margin,
                       "orphan_loss_estimate": episode.orphan_loss_estimate,
                       "up_age_ms": row.up.age_ms, "down_age_ms": row.down.age_ms,
                       "up_book_age_ms": row.up.age_ms,
                       "down_book_age_ms": row.down.age_ms,
                       "book_skew_ms": row.book_skew_ms,
                   })
        return MakerDecision(strategy=STRATEGY, decision="ACCEPT", reason=decision.reason,
                             state=LEG1_WORKING, episode_id=episode_id,
                             leg1_outcome=episode.leg1_outcome,
                             leg1_quote_price=episode.leg1_quote_price,
                             leg1_quote_mode=episode.leg1_quote_mode,
                             leg1_order_size=episode.leg1_order_size,
                             expected_margin=episode.expected_margin,
                             hedge_exit_margin=episode.hedge_exit_margin,
                             blocking_reasons=())

    # -- active episode path -----------------------------------------------------
    def _episode_path(self, row, episode, events):
        force_flatten = _tf(self.config.force_flatten_seconds, row.timeframe)
        expired = row.seconds_to_close <= 0 or not row.market_active
        in_flatten_window = 0 < row.seconds_to_close <= force_flatten

        if episode.state == LEG1_WORKING:
            if episode.forced_exit_reason:
                self._cancel_leg1(events, row, episode, episode.forced_exit_reason)
            elif expired:
                self._cancel_leg1(events, row, episode, "market_expired_mid_episode")
            elif in_flatten_window:
                self._cancel_leg1(events, row, episode, "emergency_flatten_window")
            else:
                self._leg1_working(row, episode, events)
        elif episode.state in (LEG1_FILLED, LEG2_WORKING):
            if expired:
                self._emergency_flatten(events, row, episode, "market_expired_mid_episode")
            elif episode.forced_exit_reason and self._books_evaluable(row):
                self._emergency_flatten(events, row, episode, episode.forced_exit_reason)
            elif in_flatten_window:
                self._emergency_flatten(events, row, episode, "emergency_flatten_window")
            elif not self._books_evaluable(row):
                pass  # stale books: no fill decisions, episode waits (design §7.2)
            else:
                self._leg2_working(row, episode, events)
        elif episode.state == HEDGING_DIRECTIONAL_EXIT:
            if expired or in_flatten_window:
                self._emergency_flatten(events, row, episode,
                                        "market_expired_mid_episode" if expired
                                        else "emergency_flatten_window")
            elif episode.forced_exit_reason and self._books_evaluable(row):
                self._emergency_flatten(events, row, episode, episode.forced_exit_reason)
            elif self._books_evaluable(row):
                self._directional_exit(row, episode, events)

        state = episode.state
        return MakerDecision(strategy=STRATEGY,
                             decision="COMPLETE" if state in TERMINAL_STATES else "WORKING",
                             reason=episode.terminal_reason or "episode_working",
                             state=state, episode_id=episode.episode_id,
                             completed=state in TERMINAL_STATES)

    # -- LEG1_WORKING -------------------------------------------------------------
    def _cancel_leg1(self, events, row, episode, reason):
        from_state = episode.state
        episode.terminal_reason = reason
        self._emit(events, row, "maker_leg1_cancelled", episode, "CANCELLED", reason, {
            "leg1_outcome": episode.leg1_outcome,
            "leg1_quote_price": episode.leg1_quote_price,
            "leg1_filled_size": episode.leg1_filled_size,
        })
        self._state_change(events, row, episode, from_state, LEG1_CANCELLED, reason)
        self._finalize(events, row, episode, LEG1_CANCELLED, pnl=0.0)

    def _leg1_working(self, row, episode, events):
        timeout = _tf(self.config.leg1_timeout_seconds, row.timeframe)
        if row.timestamp - episode.leg1_placed_ts > timeout:
            self._cancel_leg1(events, row, episode, "leg1_timeout")
            return
        if not self._books_evaluable(row):
            return
        side = self._side(row, episode.leg1_outcome)
        other = self._side(row, episode.leg2_outcome)
        # cancel if book deterioration breaks the open margins (design §4.2)
        expected_margin = 1.0 - (episode.leg1_quote_price + other.best_bid
                                 + self.config.buffer_per_share)
        if expected_margin < self.config.min_expected_locked_margin:
            self._cancel_leg1(events, row, episode, "expected_margin_below_threshold")
            return
        filled, strict, queue = self._buy_fill(self.config.shadow_fill_mode,
                                               episode.leg1_quote_price, side.best_ask)
        if not filled:
            return
        fill_size = min(episode.leg1_order_size - episode.leg1_filled_size,
                        side.best_ask_size)
        if fill_size <= 0:
            return
        previous = episode.leg1_filled_size
        episode.leg1_avg_price = (
            (episode.leg1_avg_price * previous + episode.leg1_quote_price * fill_size)
            / (previous + fill_size)
        )
        episode.leg1_filled_size += fill_size
        self._emit(events, row, "maker_leg_filled", episode, "FILLED", "leg1_filled", {
            "leg": 1, "outcome": episode.leg1_outcome,
            "fill_price": episode.leg1_quote_price, "fill_size": fill_size,
            "leg1_avg_price": episode.leg1_avg_price,
            "leg1_filled_size": episode.leg1_filled_size,
            "fill_mode": self.config.shadow_fill_mode,
            "strict_would_fill": strict, "queue_would_fill": queue,
            "fill_probability_model": "configured_queue_model"
            if self.config.shadow_fill_mode == FILL_QUEUE else None,
        })
        min_ratio = episode.leg1_filled_size / max(episode.leg1_order_size, _EPS)
        if episode.leg1_filled_size < episode.leg1_order_size - _EPS \
                and min_ratio < self.config.leg1_min_fill_ratio:
            return  # partial fill below min ratio: keep working the remainder
        from_state = LEG1_WORKING
        episode.leg1_fill_ts = row.timestamp
        self._state_change(events, row, episode, from_state, LEG1_FILLED, "leg1_filled")
        self._open_leg2(events, row, episode)

    # -- LEG2 ---------------------------------------------------------------------
    def _open_leg2(self, events, row, episode):
        other = self._side(row, episode.leg2_outcome)
        episode.leg2_max_price = leg2_max_price(episode.leg1_avg_price, self.config)
        episode.leg2_start_ts = row.timestamp
        episode.leg2_last_improve_ts = row.timestamp
        episode.leg2_improve_attempt = 0
        from_state = episode.state
        episode.state = LEG2_WORKING
        self._emit(events, row, "maker_episode_state_change", episode,
                   "STATE_CHANGE", "leg2_opened",
                   {"state_from": from_state, "state_to": LEG2_WORKING})
        if episode.leg2_max_price <= other.best_bid + _EPS:
            self._abandon_leg2(events, row, episode, "leg2_max_price_below_bid")
            return
        quote = _round_price(min(other.best_bid + self.config.min_tick,
                                 other.best_ask - self.config.min_tick,
                                 episode.leg2_max_price))
        episode.leg2_quote_price = quote
        self._emit(events, row, "maker_quote_updated", episode, "QUOTE", "leg2_opened", {
            "leg": 2, "outcome": episode.leg2_outcome,
            "old_quote_price": None, "new_quote_price": quote,
            "quote_reason": "leg2_initial",
            "leg1_avg_price": episode.leg1_avg_price,
            "leg1_filled_size": episode.leg1_filled_size,
            "leg2_max_price": episode.leg2_max_price,
            "leg2_best_bid": other.best_bid, "leg2_best_ask": other.best_ask,
            "improve_attempt": 0, "max_improves": self.config.leg2_max_improves,
            "min_realized_margin": self.config.min_realized_margin,
        })

    def _leg2_working(self, row, episode, events):
        side = self._side(row, episode.leg2_outcome)
        leg1_side = self._side(row, episode.leg1_outcome)
        # orphan double caps (design §4.3), conservative bid-marked drawdown
        orphan_loss = episode.holding and \
            (episode.leg1_filled_size - episode.leg2_filled_size) \
            * max(0.0, episode.leg1_avg_price - leg1_side.best_bid) or 0.0
        episode.orphan_max_drawdown = max(episode.orphan_max_drawdown, orphan_loss)
        if orphan_loss > self.config.max_orphan_loss_usd:
            self._emergency_flatten(events, row, episode, "orphan_loss_limit_exceeded")
            return
        max_orphan = _tf(self.config.max_orphan_seconds, row.timeframe)
        orphan_seconds = row.timestamp - (episode.leg1_fill_ts or row.timestamp)
        timeout = _tf(self.config.leg2_timeout_seconds, row.timeframe)
        leg2_elapsed = row.timestamp - (episode.leg2_start_ts or row.timestamp)
        if orphan_seconds > max_orphan:
            self._abandon_leg2(events, row, episode, "orphan_seconds_exceeded")
            return
        if leg2_elapsed > timeout:
            self._abandon_leg2(events, row, episode, "leg2_timeout")
            return
        # fill check
        filled, strict, queue = self._buy_fill(self.config.shadow_fill_mode,
                                               episode.leg2_quote_price, side.best_ask)
        if filled:
            remaining = episode.leg1_filled_size - episode.leg2_filled_size
            fill_size = min(remaining, side.best_ask_size)
            if fill_size > 0:
                previous = episode.leg2_filled_size
                episode.leg2_avg_price = (
                    (episode.leg2_avg_price * previous
                     + episode.leg2_quote_price * fill_size) / (previous + fill_size)
                )
                episode.leg2_filled_size += fill_size
                self._emit(events, row, "maker_leg_filled", episode, "FILLED",
                           "leg2_filled", {
                               "leg": 2, "outcome": episode.leg2_outcome,
                               "fill_price": episode.leg2_quote_price,
                               "fill_size": fill_size,
                               "leg2_avg_price": episode.leg2_avg_price,
                               "leg2_filled_size": episode.leg2_filled_size,
                               "leg2_max_price": episode.leg2_max_price,
                               "fill_mode": self.config.shadow_fill_mode,
                               "strict_would_fill": strict, "queue_would_fill": queue,
                           })
                if episode.leg2_filled_size >= episode.leg1_filled_size - _EPS:
                    self._complete_maker(events, row, episode)
                    return
        # improve loop (design §3.2)
        interval_ms = _tf(self.config.leg2_improve_interval_ms, row.timeframe)
        if (row.timestamp - episode.leg2_last_improve_ts) * 1000.0 < interval_ms:
            return
        episode.leg2_improve_attempt += 1
        episode.leg2_last_improve_ts = row.timestamp
        if episode.leg2_improve_attempt > self.config.leg2_max_improves:
            self._abandon_leg2(events, row, episode, "leg2_improves_exhausted")
            return
        episode.leg2_max_price = leg2_max_price(episode.leg1_avg_price, self.config)
        if episode.leg2_max_price <= side.best_bid + _EPS:
            self._abandon_leg2(events, row, episode, "leg2_max_price_below_bid")
            return
        step_ticks = _tf(self.config.leg2_improve_step_ticks, row.timeframe)
        new_quote = _round_price(min(side.best_bid + step_ticks * self.config.min_tick,
                                     side.best_ask - self.config.min_tick,
                                     episode.leg2_max_price))
        if new_quote > episode.leg2_quote_price + _EPS:
            old_quote = episode.leg2_quote_price
            episode.leg2_quote_price = new_quote
            self._emit(events, row, "maker_quote_updated", episode, "QUOTE",
                       "improve_loop", {
                           "leg": 2, "outcome": episode.leg2_outcome,
                           "old_quote_price": old_quote, "new_quote_price": new_quote,
                           "quote_reason": "improve_loop",
                           "leg2_max_price": episode.leg2_max_price,
                           "leg2_best_bid": side.best_bid, "leg2_best_ask": side.best_ask,
                           "improve_attempt": episode.leg2_improve_attempt,
                           "max_improves": self.config.leg2_max_improves,
                           "leg2_elapsed_ms": (row.timestamp - episode.leg2_start_ts) * 1000.0,
                       })

    def _abandon_leg2(self, events, row, episode, reason):
        """Design §3.3 ordered branches: taker hedge -> directional exit."""
        side = self._side(row, episode.leg2_outcome)
        remaining = episode.leg1_filled_size - episode.leg2_filled_size
        rate = _fee_rate(row, self.config)
        hedge_vwap = self._vwap(row, episode.leg2_outcome, "ask", remaining, side.best_ask)
        hedge_fee_total = taker_fee_total(hedge_vwap, remaining, rate)
        hedge_fee_ps = hedge_fee_total / remaining if remaining > 0 else 0.0
        hedge_margin = 1.0 - (episode.leg1_avg_price + hedge_vwap + hedge_fee_ps
                              + self.config.buffer_per_share)
        if remaining > 0 and hedge_margin >= self.config.min_hedge_exit_margin:
            # branch 1: taker hedge completes the pair
            previous = episode.leg2_filled_size
            episode.leg2_avg_price = (
                (episode.leg2_avg_price * previous + hedge_vwap * remaining)
                / (previous + remaining)
            )
            episode.leg2_filled_size += remaining
            episode.leg2_hedge_taker_fee = hedge_fee_total
            episode.exit_path = EXIT_TAKER_HEDGE
            episode.terminal_reason = reason
            self._emit(events, row, "maker_leg_filled", episode, "FILLED",
                       "leg2_taker_hedge", {
                           "leg": 2, "outcome": episode.leg2_outcome,
                           "fill_price": hedge_vwap, "fill_size": remaining,
                           "fill_mode": "taker_vwap",
                           "leg2_avg_price": episode.leg2_avg_price,
                           "hedge_taker_fee": hedge_fee_total,
                           "hedge_exit_margin": hedge_margin,
                       })
            self._complete_maker(events, row, episode)
            return
        # branch 2: directional exit of the orphan leg
        leg1_side = self._side(row, episode.leg1_outcome)
        episode.exit_path = EXIT_DIRECTIONAL
        episode.terminal_reason = reason
        floor = episode.leg1_avg_price - self.config.max_orphan_giveback_per_share
        episode.exit_quote_price = _round_price(
            max(floor, leg1_side.best_bid + self.config.min_tick))
        episode.exit_start_ts = row.timestamp
        from_state = episode.state
        self._state_change(events, row, episode, from_state,
                           HEDGING_DIRECTIONAL_EXIT, reason)
        self._emit(events, row, "maker_quote_updated", episode, "QUOTE",
                   "directional_exit_opened", {
                       "leg": 1, "outcome": episode.leg1_outcome, "side": "sell",
                       "old_quote_price": None,
                       "new_quote_price": episode.exit_quote_price,
                       "quote_reason": "directional_exit",
                       "abandon_trigger": reason,
                       "hedge_exit_margin": hedge_margin,
                       "hedge_branch_reason": "hedge_margin_below_threshold",
                   })

    def _directional_exit(self, row, episode, events):
        leg1_side = self._side(row, episode.leg1_outcome)
        remaining = episode.leg1_filled_size - episode.leg2_filled_size
        timeout = self.config.directional_exit_timeout_seconds
        if row.timestamp - episode.exit_start_ts > timeout:
            self._flatten_taker(events, row, episode, "directional_exit_timeout",
                                EXIT_DIRECTIONAL)
            return
        filled, strict, queue = self._sell_fill(self.config.shadow_fill_mode,
                                                episode.exit_quote_price,
                                                leg1_side.best_bid)
        if not filled or remaining <= 0:
            return
        rate = _fee_rate(row, self.config)
        exit_price = episode.exit_quote_price
        episode.exit_vwap = exit_price
        episode.exit_taker_fee = 0.0  # maker sell exit: no taker fee
        self._emit(events, row, "maker_leg_filled", episode, "FILLED",
                   "directional_exit_filled", {
                       "leg": 1, "outcome": episode.leg1_outcome, "side": "sell",
                       "fill_price": exit_price, "fill_size": remaining,
                       "fill_mode": self.config.shadow_fill_mode,
                       "strict_would_fill": strict, "queue_would_fill": queue,
                   })
        self._close_with_loss(events, row, episode, episode.terminal_reason,
                              exit_vwap=exit_price, exit_fee=0.0)

    def _emergency_flatten(self, events, row, episode, reason):
        from_state = episode.state
        episode.exit_path = EXIT_EMERGENCY
        episode.terminal_reason = reason
        if episode.state != EMERGENCY_FLATTEN:
            self._state_change(events, row, episode, from_state, EMERGENCY_FLATTEN, reason)
        self._flatten_taker(events, row, episode, reason, EXIT_EMERGENCY)

    def _flatten_taker(self, events, row, episode, reason, exit_path):
        leg1_side = self._side(row, episode.leg1_outcome)
        remaining = episode.leg1_filled_size - episode.leg2_filled_size
        rate = _fee_rate(row, self.config)
        vwap = self._vwap(row, episode.leg1_outcome, "bid", max(remaining, _EPS),
                          leg1_side.best_bid)
        fee = taker_fee_total(vwap, remaining, rate)
        episode.exit_vwap = vwap
        episode.exit_taker_fee = fee
        episode.exit_path = exit_path
        episode.terminal_reason = reason
        self._emit(events, row, "maker_leg_filled", episode, "FILLED",
                   "taker_exit", {
                       "leg": 1, "outcome": episode.leg1_outcome, "side": "sell",
                       "fill_price": vwap, "fill_size": remaining,
                       "fill_mode": "taker_vwap", "exit_taker_fee": fee,
                   })
        self._close_with_loss(events, row, episode, reason, exit_vwap=vwap, exit_fee=fee)

    # -- terminal accounting -------------------------------------------------------
    def _complete_maker(self, events, row, episode):
        episode.exit_path = episode.exit_path or EXIT_MAKER_COMPLETE
        episode.terminal_reason = episode.terminal_reason or "pair_completed"
        from_state = episode.state
        size = episode.locked_size
        rate = _fee_rate(row, self.config)
        gross = episode.leg1_avg_price + episode.leg2_avg_price
        gas = self.config.gas_cost_per_share
        buffer_ps = self.config.buffer_per_share
        hedge_fee_total = episode.leg2_hedge_taker_fee
        hedge_fee_ps = hedge_fee_total / size if size > 0 else 0.0
        net = gross + hedge_fee_ps + gas + buffer_ps
        locked_profit = 1.0 - net
        locked_roi = locked_profit / net if net > 0 else None
        # ESTIMATED REBATE only; never counted into realized PnL (design §5.2)
        rebate_ps = self.config.rebate_share_ratio * (
            taker_fee_per_share(episode.leg1_avg_price, rate)
            + (taker_fee_per_share(episode.leg2_avg_price, rate)
               if episode.exit_path == EXIT_MAKER_COMPLETE else 0.0)
        )
        episode.episode_realized_pnl = size * (1.0 - gross) - hedge_fee_total \
            - size * 2 * gas
        self._state_change(events, row, episode, from_state, COMPLETE,
                           episode.terminal_reason)
        self._emit(events, row, "maker_episode_completed", episode, "COMPLETE",
                   episode.terminal_reason, {
                       "gross_cost": gross,
                       "maker_fees": 0.0,
                       "hedge_taker_fee": hedge_fee_ps,
                       "hedge_taker_fee_raw": size * rate * episode.leg2_avg_price
                       * (1 - episode.leg2_avg_price) if hedge_fee_total else 0.0,
                       "hedge_taker_fee_rounded": hedge_fee_total,
                       "fee_rate": rate, "fee_formula_version": "taker_p1p_round1e5",
                       "gas_cost_per_share": gas,
                       "buffer_per_share": buffer_ps,
                       "net_cost": net,
                       "guaranteed_payout": 1.0,
                       "locked_profit": locked_profit,
                       "locked_roi": locked_roi,
                       "locked_size": size, "at_risk_size": episode.at_risk_size,
                       "estimated_rebate": rebate_ps,
                       "estimated_rebate_label": "ESTIMATED REBATE, NOT IN REALIZED PNL",
                       "estimated_liquidity_reward":
                           self.config.estimated_liquidity_reward_per_share,
                       "estimated_liquidity_reward_label": "ESTIMATED REWARD",
                       "realized_rebate": 0.0,
                       "exit_path": episode.exit_path,
                       "exit_vwap": episode.exit_vwap,
                       "orphan_seconds": row.timestamp - (episode.leg1_fill_ts or row.timestamp),
                       "orphan_max_drawdown": episode.orphan_max_drawdown,
                       "episode_realized_pnl": episode.episode_realized_pnl,
                       "leg1_avg_price": episode.leg1_avg_price,
                       "leg2_avg_price": episode.leg2_avg_price,
                       "leg1_filled_size": episode.leg1_filled_size,
                       "leg2_filled_size": episode.leg2_filled_size,
                       "leg2_max_price": episode.leg2_max_price,
                       "min_realized_margin": self.config.min_realized_margin,
                   })
        self._finalize(events, row, episode, COMPLETE, pnl=episode.episode_realized_pnl)

    def _close_with_loss(self, events, row, episode, reason, exit_vwap, exit_fee):
        from_state = episode.state
        size_locked = episode.locked_size
        at_risk = episode.leg1_filled_size - episode.leg2_filled_size
        gas = self.config.gas_cost_per_share
        rate = _fee_rate(row, self.config)
        locked_pnl = size_locked * (1.0 - episode.leg1_avg_price - episode.leg2_avg_price) \
            if size_locked > 0 else 0.0
        exit_pnl = at_risk * (exit_vwap - episode.leg1_avg_price)
        episode.episode_realized_pnl = locked_pnl + exit_pnl - exit_fee \
            - episode.leg2_hedge_taker_fee \
            - (episode.leg1_filled_size + episode.leg2_filled_size) * gas
        self._state_change(events, row, episode, from_state, CLOSED_WITH_LOSS, reason)
        self._emit(events, row, "maker_episode_closed_with_loss", episode,
                   "CLOSED_WITH_LOSS", reason, {
                       "gross_cost": episode.leg1_avg_price
                       + (episode.leg2_avg_price if episode.leg2_filled_size else 0.0),
                       "maker_fees": 0.0,
                       "hedge_taker_fee": episode.leg2_hedge_taker_fee,
                       "exit_taker_fee": exit_fee,
                       "fee_rate": rate, "fee_formula_version": "taker_p1p_round1e5",
                       "gas_cost_per_share": gas,
                       "buffer_per_share": self.config.buffer_per_share,
                       "locked_size": size_locked, "at_risk_size": at_risk,
                       "estimated_rebate": self.config.rebate_share_ratio
                       * taker_fee_per_share(episode.leg1_avg_price, rate),
                       "estimated_rebate_label": "ESTIMATED REBATE, NOT IN REALIZED PNL",
                       "realized_rebate": 0.0,
                       "exit_path": episode.exit_path,
                       "exit_vwap": exit_vwap,
                       "orphan_seconds": row.timestamp - (episode.leg1_fill_ts or row.timestamp),
                       "orphan_max_drawdown": episode.orphan_max_drawdown,
                       "episode_realized_pnl": episode.episode_realized_pnl,
                       "leg1_avg_price": episode.leg1_avg_price,
                       "leg1_filled_size": episode.leg1_filled_size,
                   })
        self._finalize(events, row, episode, CLOSED_WITH_LOSS,
                       pnl=episode.episode_realized_pnl)

    def _finalize(self, events, row, episode, terminal_state, pnl):
        self.episodes.pop(row.condition_id, None)
        record = {
            "episode_id": episode.episode_id,
            "market_id": episode.market_id,
            "condition_id": episode.condition_id,
            "terminal_state": terminal_state,
            "exit_path": episode.exit_path,
            "reason": episode.terminal_reason,
            "pnl": pnl,
            "ts": row.timestamp,
        }
        self.completed.append(record)
        if terminal_state == CLOSED_WITH_LOSS:
            self.consecutive_orphans += 1
            if self.consecutive_orphans >= self.config.max_consecutive_orphans:
                self.circuit_open_until = row.timestamp + self.config.circuit_cooldown_seconds
        elif terminal_state == COMPLETE:
            self.consecutive_orphans = 0

    # -- portfolio ------------------------------------------------------------------
    def _portfolio_view(self, row):
        total = 0.0
        at_risk = 0.0
        for episode in self.episodes.values():
            total += (episode.leg1_filled_size * episode.leg1_avg_price
                      + episode.leg2_filled_size * episode.leg2_avg_price)
            if episode.state == LEG1_WORKING:
                total += (episode.leg1_order_size - episode.leg1_filled_size) \
                    * episode.leg1_quote_price
            at_risk += (episode.leg1_filled_size - episode.leg2_filled_size) \
                * episode.leg1_avg_price
        today = int(row.timestamp // 86400)
        daily_loss = -sum(
            min(0.0, record["pnl"]) for record in self.completed
            if int(record["ts"] // 86400) == today
        )
        circuit_open = (self.circuit_open_until is not None
                        and row.timestamp < self.circuit_open_until)
        if self.circuit_open_until is not None and row.timestamp >= self.circuit_open_until:
            self.circuit_open_until = None
            self.consecutive_orphans = 0
        return PortfolioView(
            total_exposure=total, at_risk_exposure=max(0.0, at_risk),
            daily_loss=daily_loss, circuit_open=circuit_open,
            episodes_in_market=self._episodes_per_market.get(row.market_id, 0),
        )

    # -- statistics (design §7.4; N/A semantics when samples are 0) ------------------
    def statistics(self):
        opened = len(self.completed) + len(self.episodes)
        by_state = {"COMPLETE": 0, "LEG1_CANCELLED": 0, "CLOSED_WITH_LOSS": 0}
        for record in self.completed:
            by_state[record["terminal_state"]] = by_state.get(record["terminal_state"], 0) + 1
        completed_profit = [r["pnl"] for r in self.completed if r["terminal_state"] == COMPLETE]
        orphan_losses = [r["pnl"] for r in self.completed
                         if r["terminal_state"] == CLOSED_WITH_LOSS]
        leg1_filled = by_state["COMPLETE"] + by_state["CLOSED_WITH_LOSS"]
        # Live portfolio usage (same formulas as _portfolio_view, without
        # needing an input row); used by the web maker panel limit meters.
        total_exposure = 0.0
        at_risk_exposure = 0.0
        for episode in self.episodes.values():
            total_exposure += (episode.leg1_filled_size * episode.leg1_avg_price
                               + episode.leg2_filled_size * episode.leg2_avg_price)
            if episode.state == LEG1_WORKING:
                total_exposure += (episode.leg1_order_size - episode.leg1_filled_size) \
                    * episode.leg1_quote_price
            at_risk_exposure += (episode.leg1_filled_size - episode.leg2_filled_size) \
                * episode.leg1_avg_price
        # Daily loss relative to the latest event-clock day seen by the
        # machine (event timestamps, not wall clock).
        reference_ts = max(
            [record["ts"] for record in self.completed]
            + [episode.opened_ts for episode in self.episodes.values()],
            default=None,
        )
        daily_loss = 0.0
        if reference_ts is not None:
            today = int(reference_ts // 86400)
            daily_loss = -sum(
                min(0.0, record["pnl"]) for record in self.completed
                if int(record["ts"] // 86400) == today
            )
        return {
            "strategy": STRATEGY,
            "episodes_opened": opened,
            "episodes_completed": by_state["COMPLETE"],
            "episodes_cancelled": by_state["LEG1_CANCELLED"],
            "episodes_closed_with_loss": by_state["CLOSED_WITH_LOSS"],
            "active_episodes": len(self.episodes),
            "leg1_fill_rate": leg1_filled / opened if opened else None,
            "leg2_completion_rate": by_state["COMPLETE"] / leg1_filled if leg1_filled else None,
            "orphan_rate": by_state["CLOSED_WITH_LOSS"] / leg1_filled if leg1_filled else None,
            "average_locked_profit": (sum(completed_profit) / len(completed_profit)
                                      if completed_profit else None),
            "average_orphan_loss": (sum(orphan_losses) / len(orphan_losses)
                                    if orphan_losses else None),
            "max_orphan_loss": min(orphan_losses) if orphan_losses else None,
            "realized_shadow_pnl": sum(r["pnl"] for r in self.completed),
            "consecutive_orphans": self.consecutive_orphans,
            "circuit_breaker_open": self.circuit_open_until is not None,
            "active_total_exposure": total_exposure,
            "active_at_risk_exposure": max(0.0, at_risk_exposure),
            "daily_loss": daily_loss,
            "limits": {
                "max_notional_per_market": self.config.max_notional_per_market,
                "max_total_exposure": self.config.max_total_exposure,
                "max_at_risk_exposure": self.config.max_at_risk_exposure,
                "max_daily_loss": self.config.max_daily_loss,
                "max_consecutive_orphans": self.config.max_consecutive_orphans,
            },
            "real_order_submissions": 0,
            "real_orders": 0,
            "real_fills": 0,
        }
