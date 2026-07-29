"""Pin the Python mirror of the C++ canonical strategy config hash.

config_hash values in strategy-audit / shadow-complete rows are produced by
strategy_config_hash() in cpp/market_ws_engine/market_ws_engine.cpp. Python
consumers (shadow_report current-config filtering, strategy_shadow_lifecycle
loss limits) compare row hashes against canonical_strategy_config_hash(), so
any drift between the two implementations must fail these tests.
"""
from pathlib import Path

import pytest

from poly_arb_bot.ev_shadow import (
    _CANONICAL_STRATEGY_CONFIG_ENV,
    canonical_strategy_config_hash,
)

SOURCE = Path("cpp/market_ws_engine/market_ws_engine.cpp").read_text(encoding="utf-8")

# Digests computed with every canonical env var unset (all defaults). Verified
# byte-identical to the live C++ producer: the VPS market_ws_engine emits the
# same directional/lottery hashes under the deployed .env.
DEFAULT_HASH = "2766fcce6afc639e156bcf90b2532e7e8d4d2bcd018e8d36d72688b6186353e1"
DIRECTIONAL_HASH = "7b808614087fd326b804d5aa928678c17fd7e02bb32c7057b91bb670b90a4f74"
LOTTERY_HASH = "927adb85e22c496176430af3362d73c84cb16708e40f9cb756809be405b51412"


@pytest.fixture
def clean_env(monkeypatch):
    for _, env, _ in _CANONICAL_STRATEGY_CONFIG_ENV:
        monkeypatch.delenv(env, raising=False)
    return monkeypatch


def test_default_environment_digests_are_pinned(clean_env):
    assert canonical_strategy_config_hash() == DEFAULT_HASH
    assert canonical_strategy_config_hash("late_window_directional_ev") == DIRECTIONAL_HASH
    assert canonical_strategy_config_hash("low_price_lottery_ev") == LOTTERY_HASH


def test_canonical_only_key_changes_default_and_directional_but_not_lottery(clean_env):
    clean_env.setenv("DIRECTIONAL_FRACTIONAL_KELLY", "0.25")
    assert canonical_strategy_config_hash() != DEFAULT_HASH
    assert canonical_strategy_config_hash("late_window_directional_ev") != DIRECTIONAL_HASH
    assert canonical_strategy_config_hash("low_price_lottery_ev") == LOTTERY_HASH


def test_directional_threshold_does_not_leak_into_lottery_hash(clean_env):
    clean_env.setenv("DIRECTIONAL_MIN_NET_EV", "0.50")
    assert canonical_strategy_config_hash("low_price_lottery_ev") == LOTTERY_HASH
    assert canonical_strategy_config_hash("late_window_directional_ev") != DIRECTIONAL_HASH


def test_common_key_changes_both_strategy_hashes(clean_env):
    clean_env.setenv("CLOB_MAX_BOOK_AGE_MS", "1000")
    assert canonical_strategy_config_hash("late_window_directional_ev") != DIRECTIONAL_HASH
    assert canonical_strategy_config_hash("low_price_lottery_ev") != LOTTERY_HASH


def test_calibration_cohort_version_rotates_both_probability_strategy_hashes(clean_env):
    assert (
        "probability_calibration_cohort_version",
        "PROBABILITY_CALIBRATION_COHORT_VERSION",
        "2",
    ) in _CANONICAL_STRATEGY_CONFIG_ENV
    before_directional = canonical_strategy_config_hash("late_window_directional_ev")
    before_lottery = canonical_strategy_config_hash("low_price_lottery_ev")

    clean_env.setenv("PROBABILITY_CALIBRATION_COHORT_VERSION", "next")

    assert canonical_strategy_config_hash("late_window_directional_ev") != before_directional
    assert canonical_strategy_config_hash("low_price_lottery_ev") != before_lottery


def test_cash_ledger_version_rotates_both_probability_strategy_hashes(clean_env):
    assert (
        "shadow_cash_ledger_version",
        "SHADOW_CASH_LEDGER_VERSION",
        "2",
    ) in _CANONICAL_STRATEGY_CONFIG_ENV
    before_directional = canonical_strategy_config_hash("late_window_directional_ev")
    before_lottery = canonical_strategy_config_hash("low_price_lottery_ev")

    clean_env.setenv("SHADOW_CASH_LEDGER_VERSION", "next")

    assert canonical_strategy_config_hash("late_window_directional_ev") != before_directional
    assert canonical_strategy_config_hash("low_price_lottery_ev") != before_lottery


def test_every_canonical_triple_exists_in_cpp_source():
    for key, env, default in _CANONICAL_STRATEGY_CONFIG_ENV:
        assert f'{{"{key}", environment_value(' in SOURCE, key
        assert f'"{env}", "{default}")' in SOURCE, (env, default)
    assert '{"probability_reference", "settlement_reference"}' in SOURCE


def test_cpp_per_strategy_filter_keeps_common_keys():
    for key in ("coinbase_reference_max_age_ms", "shadow_sizing_capital_usd",
                "shadow_profit_exit_buffer_per_share",
                "shadow_cash_ledger_version"):
        assert f'key == "{key}"' in SOURCE, key
