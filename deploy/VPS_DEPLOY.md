# VPS Shadow deployment

This deployment runs all three strategies in Shadow / Dry Run. It never submits a real order.

## 1. Install dependencies

Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip g++ \
  libboost-system-dev libssl-dev pkg-config curl jq sysstat
```

## 2. Clone and configure

```bash
sudo mkdir -p /opt/poly-arb-bot
sudo chown "$USER":"$USER" /opt/poly-arb-bot
git clone https://github.com/jasonsoldo/poly-arb-bot.git /opt/poly-arb-bot
cd /opt/poly-arb-bot
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip pytest
cp deploy/env.example .env
mkdir -p data logs state logs/archive
rm -f state/reference-price.sock
```

Do not add wallet keys or live-order credentials. `POLY_ARB_MODE` must remain `dry_run`.

`SHADOW_CALIBRATION_MODE=1` disables only portfolio sampling throttles in Shadow
(daily loss, consecutive loss, correlation, position count, and notional limits).
Market-data, settlement, fee, depth, slippage, freshness, clock, and strategy EV
checks remain fail closed. Each opened position records `would_block_reason`, and
real submissions/orders/fills remain zero. Never use this setting for live execution.

## 3. Build and verify locally on the VPS

```bash
. .venv/bin/activate
bash scripts/build_cpp.sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
bash -n scripts/run_shadow_loop.sh scripts/check_ntp.sh scripts/build_cpp.sh
```

Install and validate the units before starting them:

```bash
sudo cp deploy/poly-arb-bot.service /etc/systemd/system/poly-arb-bot.service
sudo cp deploy/poly-arb-web.service /etc/systemd/system/poly-arb-web.service
sudo systemd-analyze verify \
  /etc/systemd/system/poly-arb-bot.service \
  /etc/systemd/system/poly-arb-web.service
```

## 4. Verify official integrations

The REST probes must return HTTP 200. The scanner and running C++ services perform the official WebSocket checks.

```bash
curl -fsS --max-time 15 \
  'https://gamma-api.polymarket.com/events?active=true&closed=false&limit=1' \
  -o /tmp/gamma-event.json
curl -fsS --max-time 15 'https://clob.polymarket.com/time'
curl -fsS --max-time 15 'https://data-api.binance.vision/api/v3/time'

python -m poly_arb_bot.cli scan-updown \
  --output data/live_markets.json \
  --intervals 5m,15m,1h,4h \
  --slug-window current,next
python -m json.tool data/live_markets.json >/dev/null
```

For discovery diagnostics only, widen the event window without weakening CLOB validation:

```bash
python -m poly_arb_bot.cli scan-updown \
  --output data/live_markets.json \
  --intervals 5m,15m,1h,4h \
  --slug-window previous,current,next
```

The scanner must finish inside its 45 second global deadline. A failed scan must retain a still-active old `live_markets.json`; zero markets is not a successful release result.

## 5. Install log rotation and start services

```bash
sudo cp deploy/poly-arb-bot.logrotate /etc/logrotate.d/poly-arb-bot
sudo chmod 0644 /etc/logrotate.d/poly-arb-bot
sudo logrotate -d /etc/logrotate.d/poly-arb-bot

sudo systemctl daemon-reload
sudo systemctl enable --now poly-arb-bot poly-arb-web
sudo systemctl status poly-arb-bot poly-arb-web --no-pager -l
```

`run_shadow_loop.sh` removes a stale `state/reference-price.sock`, starts `reference_price_engine`, waits for the Unix socket, and only then starts `market_ws_engine`.

## 6. C++ / Python parity window

Run at least 30 minutes after warm-up. Python runs only as a verifier; C++ remains the canonical producer.

```bash
cd /opt/poly-arb-bot
mkdir -p logs/archive
sudo systemctl stop poly-arb-bot
if [ -s logs/strategy-parity.jsonl ]; then
  mv logs/strategy-parity.jsonl \
    "logs/archive/strategy-parity-$(date -u +%Y%m%dT%H%M%SZ).jsonl"
fi
: > logs/strategy-parity.jsonl
sudo systemctl start poly-arb-bot
sleep 1800

grep -c 'strategy_parity_mismatch' logs/strategy-parity.jsonl
grep -E 'REFERENCE_CONNECTED|WS_DATA|strategy_audit_backpressure' \
  /var/log/poly-arb-bot.err.log | tail -100
```

Required result:

```text
strategy_parity_mismatch = 0
strategy_audit_backpressure = 0
```

Any mismatch blocks release. Do not loosen fees, buffers, freshness, depth, quorum, or strategy thresholds to make the gate pass.

## 7. Shadow acceptance and real-order invariants

```bash
. .venv/bin/activate
python -m poly_arb_bot.cli shadow-acceptance
curl -fsS --max-time 30 http://127.0.0.1:8787/api/status \
  -o /tmp/poly-status.json
python - <<'PY'
import json

status = json.load(open('/tmp/poly-status.json'))
lifecycle = status['shadow_lifecycle']
execution = status['shadow_execution']
assert lifecycle['real_order_submissions'] == 0
assert lifecycle['real_orders'] == 0
assert lifecycle['real_fills'] == 0
assert execution['real_order_submissions'] == 0
assert execution['real_orders'] == 0
assert execution['real_fills'] == 0
assert status['counts']['executed_orders'] == 0
print('real_order_submissions=0 real_orders=0 real_fills=0')
PY
```

Required result:

```text
shadow-acceptance = PASS
real_order_submissions = 0
real_orders = 0
real_fills = 0
```

## 8. Local pipeline performance gates

After five minutes of warm-up, inspect the bounded p95 samples written by C++:

```bash
jq '{
  reference_ipc_receive_age_ms_p95,
  reference_ipc_receive_age_samples,
  clob_to_strategy_evaluation_us_p95,
  clob_to_strategy_evaluation_samples,
  strategy_audit_queue,
  strategy_audit_backpressure
}' data/shadow-health.json

pidstat -p "$(pgrep -d, -f 'market_ws_engine|reference_price_engine|web-monitor')" 1 60
pidstat -d -p "$(pgrep -d, -f 'market_ws_engine|reference_price_engine|web-monitor')" 1 60
curl -sS -o /dev/null -w 'status_total=%{time_total}s\n' \
  http://127.0.0.1:8787/api/status
```

Release thresholds:

```text
reference IPC receive age p95 < 50 ms
CLOB mutation to strategy evaluation p95 < 250 us
Web process must not sustain > 80% of one CPU
steady aggregate disk writes < 200 KiB/s excluding log rotation
strategy_audit_backpressure = 0
```

These are local pipeline metrics. Exchange/network message age is a different metric and must not be relabeled as latency.

## 9. Monitoring

```bash
journalctl -u poly-arb-bot -f
journalctl -u poly-arb-web -f
tail -F logs/shadow-audit.jsonl logs/strategy-audit.jsonl \
  logs/shadow-execution.jsonl logs/strategy-parity.jsonl
```

Analytics may temporarily report `REBUILDING` after first deployment or loss of its compact summary. This must not change a healthy engine from `ONLINE` to `DEGRADED`. Web/Python failure must not stop the C++ market and reference feeds.

## ROLLBACK

If parity, performance, acceptance, or real-order invariants fail:

```bash
sudo systemctl stop poly-arb-bot poly-arb-web
git log --oneline -10
git checkout <previous-tested-shadow-commit>
bash scripts/build_cpp.sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
sudo systemctl start poly-arb-bot poly-arb-web
```

Rollback restores a previously tested Shadow commit. It must never enable real execution.
