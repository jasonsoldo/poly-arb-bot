# VPS dry-run deployment

This deployment starts a dry-run shadow loop. It does not send live orders.

## 1. Install packages

Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip g++
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

## 4. Configure live markets

Create `/opt/poly-arb-bot/data/live_markets.json` from `data/live_markets.example.json`.

You must fill real values:

- `market_id`
- `open_price`
- `close_ts`
- `up_token_id`
- `down_token_id`

Do not run live trading until the dry-run logs are stable.

## 5. Run one dry-run iteration

```bash
. .venv/bin/activate
python -m poly_arb_bot.cli live-run --mode dry_run --markets data/live_markets.json --output data/live_snapshot.json --interval-seconds 2 --iterations 1 --require-cpp
```

## 6. Install systemd dry-run service

```bash
cp deploy/env.example .env
sudo cp deploy/poly-arb-bot.service /etc/systemd/system/poly-arb-bot.service
sudo systemctl daemon-reload
sudo systemctl enable poly-arb-bot
sudo systemctl start poly-arb-bot
sudo systemctl status poly-arb-bot
```

Logs:

```bash
tail -f /var/log/poly-arb-bot.log
tail -f /var/log/poly-arb-bot.err.log
```

## Safety

Current service uses `--mode dry_run`. Keep it this way until:

- real market scanner is verified
- orderbook snapshots are logged
- stale orderbook checks are clean
- duplicate order guard is persistent
- position sync is verified
- live execution client is implemented and tested
