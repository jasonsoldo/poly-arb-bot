#!/usr/bin/env bash
# vps_bootstrap.sh - one-shot, idempotent VPS environment preparation for the
# poly-arb-bot Shadow / Dry Run deployment (Ubuntu 22.04 / 24.04).
#
# What it does (safe to re-run):
#   1. Installs required apt packages (only when missing).
#   2. Enables NTP time sync (systemd-timesyncd) and reports sync status.
#   3. Creates /opt/poly-arb-bot and clones or fast-forwards the repository
#      (skipped when the directory was populated by rsync without .git).
#   4. Normalizes CRLF -> LF on shell scripts and deploy files (defensive;
#      .gitattributes already enforces LF for fresh clones).
#   5. Creates the Python virtualenv and installs the project + pytest.
#   6. Creates data/logs/state directories, seeds .env from deploy/env.example,
#      and removes a stale reference-price IPC socket.
#   7. Self-checks every shell script with bash -n.
#
# Optional flags:
#   --with-build     compile C++ engines via scripts/build_cpp.sh
#   --with-tests     run the Python test suite (implies venv install)
#   --with-systemd   install systemd units + logrotate config + hourly
#                    logrotate timer override, then daemon-reload and enable
#                    (does NOT start services; start them manually after
#                    reviewing .env and passing the acceptance steps)
#
# Environment overrides:
#   APP_DIR   (default /opt/poly-arb-bot)
#   REPO_URL  (default https://github.com/jasonsoldo/poly-arb-bot.git)
#   GIT_REF   (default: stay on the cloned default branch / current checkout)
#
# This script never enables a real-order path. POLY_ARB_MODE must stay dry_run.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/poly-arb-bot}"
REPO_URL="${REPO_URL:-https://github.com/jasonsoldo/poly-arb-bot.git}"
GIT_REF="${GIT_REF:-}"
WITH_BUILD=0
WITH_TESTS=0
WITH_SYSTEMD=0

for arg in "$@"; do
  case "$arg" in
    --with-build) WITH_BUILD=1 ;;
    --with-tests) WITH_TESTS=1 ;;
    --with-systemd) WITH_SYSTEMD=1 ;;
    -h|--help)
      sed -n '2,40p' "$0"
      exit 0
      ;;
    *)
      echo "BOOTSTRAP_ERROR unknown_arg=$arg" >&2
      exit 2
      ;;
  esac
done

log() { echo "BOOTSTRAP $*"; }
warn() { echo "BOOTSTRAP_WARN $*" >&2; }
die() { echo "BOOTSTRAP_ERROR $*" >&2; exit 1; }

# --- privilege handling ------------------------------------------------------
SUDO=""
if [[ "$(id -u)" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    warn "not root and sudo missing; apt/systemd steps will fail"
  fi
fi
APP_USER="${SUDO_USER:-$(id -un)}"

# --- 1. apt packages ----------------------------------------------------------
APT_PACKAGES=(
  git ca-certificates curl jq rsync
  python3 python3-venv python3-pip
  g++ make pkg-config libboost-system-dev libssl-dev
  logrotate sysstat
)

missing=()
if command -v dpkg-query >/dev/null 2>&1; then
  for pkg in "${APT_PACKAGES[@]}"; do
    dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" \
      || missing+=("$pkg")
  done
else
  die "dpkg-query not found; this script targets Ubuntu/Debian (apt)"
fi

if ((${#missing[@]})); then
  log "installing missing apt packages: ${missing[*]}"
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get update
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
else
  log "apt packages already installed (${#APT_PACKAGES[@]} checked)"
fi

# --- 2. NTP time synchronization ----------------------------------------------
# Directional/lottery strategies fail closed on clock skew; the bot service
# refuses to start until NTPSynchronized=yes (scripts/check_ntp.sh).
if command -v timedatectl >/dev/null 2>&1; then
  $SUDO timedatectl set-ntp true || warn "timedatectl set-ntp failed"
  ntp_state="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown)"
  if [[ "$ntp_state" == "yes" ]]; then
    log "NTP synchronized"
  else
    warn "NTPSynchronized=$ntp_state (first sync can take minutes; verify before starting services: timedatectl status)"
  fi
else
  warn "timedatectl not found; ensure chrony/ntpd keeps the clock synchronized"
fi

# --- 3. code checkout ----------------------------------------------------------
$SUDO mkdir -p "$APP_DIR"
# Give the invoking (non-root) user ownership so builds do not need root.
if [[ "$(id -u)" -eq 0 && -n "${SUDO_USER:-}" ]]; then
  chown "$APP_USER":"$APP_USER" "$APP_DIR"
fi

if [[ -d "$APP_DIR/.git" ]]; then
  log "repository present; fast-forwarding"
  git -C "$APP_DIR" fetch --prune origin
  if [[ -n "$GIT_REF" ]]; then
    git -C "$APP_DIR" checkout "$GIT_REF"
  else
    git -C "$APP_DIR" pull --ff-only || die "git pull --ff-only failed; resolve local divergence in $APP_DIR manually"
  fi
elif [[ -z "$(ls -A "$APP_DIR")" ]]; then
  log "cloning $REPO_URL into $APP_DIR"
  git clone "$REPO_URL" "$APP_DIR"
  [[ -n "$GIT_REF" ]] && git -C "$APP_DIR" checkout "$GIT_REF"
else
  warn "$APP_DIR exists without .git (rsync flow?); skipping clone/update"
fi

# --- 4. CRLF -> LF normalization (defensive, idempotent) ----------------------
# Fresh clones are LF via .gitattributes; this also repairs rsync copies made
# from a Windows working tree.
shopt -s nullglob
text_files=("$APP_DIR"/scripts/*.sh "$APP_DIR"/deploy/*)
normalized=0
for f in "${text_files[@]}"; do
  [[ -f "$f" ]] || continue
  if LC_ALL=C grep -q $'\r' "$f"; then
    sed -i 's/\r$//' "$f"
    normalized=$((normalized + 1))
  fi
done
log "line-ending check done (normalized=$normalized files)"
chmod +x "$APP_DIR"/scripts/*.sh 2>/dev/null || true

# --- 5. Python virtualenv + dependencies ---------------------------------------
cd "$APP_DIR"
if [[ ! -x .venv/bin/python ]]; then
  log "creating virtualenv .venv"
  python3 -m venv .venv
fi
# websockets is a hard runtime dependency (poly_arb_bot.cli imports shadow_ws),
# so the project itself must be installed, not just pytest.
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e . pytest
log "python environment ready: $(.venv/bin/python --version 2>&1)"

# --- 6. runtime directories, .env, stale socket --------------------------------
mkdir -p data logs state logs/archive
touch logs/shadow-audit.jsonl logs/strategy-audit.jsonl \
      logs/shadow-execution.jsonl logs/strategy-parity.jsonl
if [[ ! -f .env ]]; then
  cp deploy/env.example .env
  log ".env seeded from deploy/env.example (review it before starting services)"
else
  log ".env already exists; left untouched"
fi
rm -f state/reference-price.sock
if grep -q '^POLY_ARB_MODE=' .env && ! grep -q '^POLY_ARB_MODE=dry_run' .env; then
  die ".env POLY_ARB_MODE is not dry_run; refusing to continue (Shadow-only deployment)"
fi

# --- 7. bash -n self-check ------------------------------------------------------
script_files=("$APP_DIR"/scripts/*.sh)
for f in "${script_files[@]}"; do
  bash -n "$f" || die "bash syntax check failed: $f"
done
log "bash -n OK (${#script_files[@]} scripts)"

# --- optional: C++ build --------------------------------------------------------
if ((WITH_BUILD)); then
  log "building C++ engines (scripts/build_cpp.sh)"
  bash scripts/build_cpp.sh
fi

# --- optional: tests ------------------------------------------------------------
if ((WITH_TESTS)); then
  log "running Python tests"
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
fi

# --- optional: systemd + logrotate install --------------------------------------
if ((WITH_SYSTEMD)); then
  [[ "$(id -u)" -eq 0 || -n "$SUDO" ]] || die "--with-systemd requires root or sudo"
  $SUDO cp deploy/poly-arb-bot.service /etc/systemd/system/poly-arb-bot.service
  $SUDO cp deploy/poly-arb-web.service /etc/systemd/system/poly-arb-web.service
  $SUDO cp deploy/poly-arb-bot.logrotate /etc/logrotate.d/poly-arb-bot
  $SUDO chmod 0644 /etc/logrotate.d/poly-arb-bot

  # Run logrotate hourly so the maxsize triggers fire within the hour; the
  # stock logrotate.timer only runs daily, which is too slow for these logs.
  $SUDO mkdir -p /etc/systemd/system/logrotate.timer.d
  $SUDO tee /etc/systemd/system/logrotate.timer.d/poly-arb-hourly.conf >/dev/null <<'EOF'
[Timer]
OnCalendar=
OnCalendar=hourly
EOF

  $SUDO systemd-analyze verify \
    /etc/systemd/system/poly-arb-bot.service \
    /etc/systemd/system/poly-arb-web.service
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable poly-arb-bot poly-arb-web
  $SUDO systemctl restart logrotate.timer
  log "systemd units installed+enabled (NOT started); logrotate.timer set to hourly"
  log "next: review $APP_DIR/.env, then: sudo systemctl start poly-arb-bot poly-arb-web"
fi

log "DONE app_dir=$APP_DIR mode=shadow_dry_run real_orders=0"
