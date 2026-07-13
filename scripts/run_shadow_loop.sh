#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p data logs state
touch logs/shadow-audit.jsonl
python_bin="${PYTHON_BIN:-$PWD/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
  echo "PYTHON_NOT_EXECUTABLE path=$python_bin" >&2
  exit 1
fi

refresh_seconds="${MARKET_REFRESH_SECONDS:-60}"
size="${SHADOW_SIZE:-10}"
fee_rate="${SHADOW_FEE_RATE:-0.07}"
buffer_per_share="${SHADOW_BUFFER_PER_SHARE:-0.002}"
min_profit="${SHADOW_MIN_PROFIT:-0.01}"
leg_interval_us="${SHADOW_LEG_INTERVAL_US:-50000}"
execution_half_life_us="${SHADOW_EXECUTION_HALF_LIFE_US:-250000}"
orphan_loss_per_share="${SHADOW_ORPHAN_LOSS_PER_SHARE:-0.02}"
min_expected_value="${SHADOW_MIN_EXPECTED_VALUE:-0.01}"

scan_once() {
  "$python_bin" -m poly_arb_bot.cli scan-updown \
    --output data/live_markets.json \
    --intervals 5m,15m,1h,4h \
    --slug-window current,next
}

until scan_once && [[ "$("$python_bin" -c 'import json; print(len(json.load(open("data/live_markets.json"))["markets"]))')" -gt 0 ]]; do
  echo "SHADOW_LOOP no_markets retry_s=$refresh_seconds"
  sleep "$refresh_seconds"
done

(
  while true; do
    sleep "$refresh_seconds"
    scan_once || echo "SHADOW_LOOP scan_error retry_s=$refresh_seconds"
  done
) &
scanner_pid=$!
./build/reference_price_engine data/venue-status.json &
reference_pid=$!
"$python_bin" -m poly_arb_bot.shadow_execution &
execution_pid=$!
trap 'kill "$scanner_pid" "$reference_pid" "$execution_pid" 2>/dev/null || true' EXIT INT TERM

echo "SHADOW_LOOP engine_start dynamic_reload_s=5 market_scan_s=$refresh_seconds"
./build/market_ws_engine data/live_markets.json "$size" "$fee_rate" logs/shadow-audit.jsonl \
  "$buffer_per_share" "$min_profit" "$leg_interval_us" "$execution_half_life_us" \
  "$orphan_loss_per_share" "$min_expected_value" data/shadow-health.json
