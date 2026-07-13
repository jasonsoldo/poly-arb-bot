#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p data logs state

refresh_seconds="${MARKET_REFRESH_SECONDS:-60}"
size="${SHADOW_SIZE:-10}"
fee_rate="${SHADOW_FEE_RATE:-0.07}"

while true; do
  python -m poly_arb_bot.cli scan-updown \
    --output data/live_markets.json \
    --intervals 5m,15m \
    --slug-window previous,current,next

  market_count="$(python -c 'import json; print(len(json.load(open("data/live_markets.json"))["markets"]))')"
  if [[ "$market_count" -eq 0 ]]; then
    echo "SHADOW_LOOP no_markets retry_s=$refresh_seconds"
    sleep "$refresh_seconds"
    continue
  fi

  echo "SHADOW_LOOP start markets=$market_count refresh_s=$refresh_seconds"
  timeout --signal=TERM "$refresh_seconds" \
    ./build/market_ws_engine data/live_markets.json "$size" "$fee_rate" logs/shadow-audit.jsonl || status=$?
  status="${status:-0}"
  if [[ "$status" -ne 0 && "$status" -ne 124 && "$status" -ne 143 ]]; then
    echo "SHADOW_LOOP engine_exit=$status retry_s=2"
    sleep 2
  fi
  unset status
done
