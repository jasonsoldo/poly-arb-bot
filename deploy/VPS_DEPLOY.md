# VPS dry-run deployment

This deployment starts a dry-run shadow loop. It does not send live orders.

## 1. Install packages

Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip g++ libboost-system-dev libssl-dev pkg-config
```

## 2. Clone

```bash
sudo mkdir -p /opt/poly-arb-bot
sudo chown "$USER":"$USER" /opt/poly-arb-bot
git clone https://github.com/jasonsoldo/poly-arb-bot.git /opt/poly-arb-bot
cd /opt/poly-arb-bot
```

## 3. Build and test

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip pytest
bash scripts/build_cpp.sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
```

## 4.1 C++ WebSocket shadow engine

The low-latency path is handled by `build/market_ws_engine`: Boost.Beast connects directly to the Polymarket market WebSocket, applies `book` and `price_change` messages, then runs VWAP, fee, depth and FOK checks in C++. It only writes Shadow opportunities and never submits orders.

```bash
./build/market_ws_engine data/live_markets.json 10 0.07 \
  | tee -a logs/shadow-cpp.tsv
```

## 4. Auto-scan live markets

The bot can now auto-generate `/opt/poly-arb-bot/data/live_markets.json` from Polymarket Gamma:

```bash
python -m poly_arb_bot.cli scan-updown \
  --output data/live_markets.json \
  --intervals 5m,15m,1h,4h \
  --slug-window current,next
```

If it returns zero markets, widen the search:

```bash
python -m poly_arb_bot.cli scan-updown \
  --output data/live_markets.json \
  --intervals 5m,15m,1h,4h \
  --slug-window previous,current,next
```

## 5. Run one dry-run iteration

```bash
. .venv/bin/activate
python -m poly_arb_bot.cli live-run \
  --mode dry_run \
  --auto-scan \
  --markets data/live_markets.json \
  --output data/live_snapshot.json \
  --interval-seconds 2 \
  --iterations 1 \
  --require-cpp \
  --state-file state/orders.json \
  --log-file logs/orders.jsonl
```

## 6. Install systemd dry-run service

```bash
cp deploy/env.example .env
sudo cp deploy/poly-arb-bot.service /etc/systemd/system/poly-arb-bot.service
sudo systemctl daemon-reload
sudo systemctl enable poly-arb-bot
sudo systemctl start poly-arb-bot
sudo systemctl status poly-arb-bot

# Start the web monitor on port 8787
sudo cp deploy/poly-arb-web.service /etc/systemd/system/poly-arb-web.service
sudo systemctl daemon-reload
sudo systemctl enable poly-arb-web
sudo systemctl start poly-arb-web
sudo ufw allow 8787/tcp

# Install and validate JSONL rotation.
sudo cp deploy/poly-arb-bot.logrotate /etc/logrotate.d/poly-arb-bot
sudo chmod 0644 /etc/logrotate.d/poly-arb-bot
sudo logrotate -d /etc/logrotate.d/poly-arb-bot
```

Logs:

```bash
tail -f /var/log/poly-arb-bot.log
tail -f /var/log/poly-arb-bot.err.log
tail -f /opt/poly-arb-bot/logs/orders.jsonl
```

## Safety

Current service uses `--mode dry_run`. Keep it this way until:

- real market scanner is verified
- orderbook snapshots are logged
- stale orderbook checks are clean
- duplicate order guard is persistent
- position sync is verified
- live execution client is implemented and tested
