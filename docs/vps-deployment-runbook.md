# VPS 生产部署手册（Shadow / Dry Run）

本文是 poly-arb-bot 在生产 Linux VPS 上的完整部署手册。部署产物为**纯 Shadow / Dry Run** 系统：`POLY_ARB_MODE=dry_run`，真实订单提交数恒为 0，不配置任何钱包或下单凭据。

相关文件：

- `deploy/VPS_DEPLOY.md` —— 验收门槛（parity、性能、shadow-acceptance、回滚）的权威清单，本手册引用而不重复
- `deploy/poly-arb-bot.service` / `deploy/poly-arb-web.service` —— systemd 单元
- `deploy/poly-arb-bot.logrotate` —— 日志轮转配置
- `deploy/env.example` —— 全部策略与风控阈值（显式配置，无隐藏 magic number）
- `scripts/vps_bootstrap.sh` —— 一键幂等环境准备脚本
- `scripts/run_shadow_loop.sh` —— 主编排脚本（CA 探测、stale socket 清理、45s scan deadline、Gamma 失败保留缓存）

---

## 1. VPS 选址

### 1.1 地区选择

- **必须避开美国**。Polymarket 对美国等司法辖区有访问限制（geo-block），美国机房无法正常访问 Gamma / CLOB。受限地区清单以 Polymarket 最新条款为准，部署前实测为准。
- **优先欧洲 Frankfurt / Amsterdam**：
  - 不受 Polymarket geo-block 影响；
  - 参考行情源（Binance / Coinbase / Kraken / Bybit / OKX）在欧洲均有低延迟接入点；
  - 到 Polymarket API 与公共 Polygon RPC 端点的链路质量普遍较好。
- 常见供应商：Hetzner（Falkenstein / Nuremberg，如 CPX21 2C4G）、OVH（Gravelines）、Contabo（纽伦堡）、DigitalOcean FRA1 / AMS3。

### 1.2 下单前实测

Shadow 模式没有真实成交，对延迟不敏感，但参考行情 freshness 阈值（`REFERENCE_MAX_AGE_MS=3000`）与 45 秒 scan deadline 要求链路稳定。用 looking glass 或试用机实测到以下端点的 RTT 与丢包：

```text
gamma-api.polymarket.com          （市场发现）
clob.polymarket.com               （REST 预热/校验）
ws-subscriptions-clob.polymarket.com （CLOB WebSocket）
data-stream.binance.vision        （Binance 公共行情 WS）
你选择的 Polygon RPC 端点          （Chainlink 结算参考）
```

注意：本机（Windows）实测 `data-api.binance.vision` REST 返回 451 但 Binance WebSocket 可连且 FRESH。VPS 上需重新实测；参考层按 quorum 设计容忍单源故障，**不要**因为某一源异常就绕过或删除它（AGENTS.md §4.1）。

### 1.3 配置规格

起步规格：**2 vCPU / 4 GB RAM / 40+ GB SSD**。

| 项目 | 依据 |
|---|---|
| 2 vCPU | C++ 双引擎稳态 CPU 占用低；web-monitor 周期性 analytics 刷新需要余量（验收门槛：Web 进程不得持续占满单核 80%） |
| 4 GB RAM | g++ `-O3` 编译 `market_ws_engine.cpp`（Boost.Beast 重型模板）峰值内存可达约 1.5–2.5 GB；2 GB 机型编译可能 OOM。稳态运行本身远低于 4 GB |
| 40+ GB SSD | 本机观察日志体量约 250 MB / 25 分钟（主要是引擎 stderr 与 JSONL 审计），必须靠 hourly logrotate + gzip 压缩控制；磁盘还需容纳 clone、venv、build 与 rotate 30 份压缩归档 |

省钱方案：2C2G + 2 GB swap 也可运行（见 3.4），编译慢一些但功能等价。

---

## 2. 系统准备（Ubuntu 22.04 LTS / 24.04 LTS）

以下全部由 `scripts/vps_bootstrap.sh` 自动完成（见第 4 节），此处为手动对照清单。

### 2.1 依赖包

```bash
sudo apt-get update
sudo apt-get install -y \
  git ca-certificates curl jq rsync \
  python3 python3-venv python3-pip \
  g++ make pkg-config libboost-system-dev libssl-dev \
  logrotate sysstat
```

说明：

- `libboost-system-dev` + `libssl-dev`：C++ WebSocket 引擎（Boost.Beast + OpenSSL）编译所需；Beast 本体为头文件库。
- `ca-certificates`：Linux 有系统 CA store，`run_shadow_loop.sh` 的 CA 探测在 Linux 会自动落到 `/etc/ssl/certs/ca-certificates.crt`，无需像 Windows 那样手动指定 Git 的 ca-bundle。
- `sysstat`：提供 `pidstat`，用于 VPS_DEPLOY.md §8 的性能门槛测量。

### 2.2 NTP 时间同步（硬性门槛）

方向/彩票策略在 clock skew 超阈值时 fail closed；`poly-arb-bot.service` 的 `ExecStartPre=scripts/check_ntp.sh` 要求 `NTPSynchronized=yes` 才允许启动。

```bash
sudo timedatectl set-ntp true
timedatectl status          # 确认 System clock synchronized: yes
timedatectl show -p NTPSynchronized --value   # 必须输出 yes
```

首次同步可能需要几分钟。建议时区设为 UTC，便于 journal 与 logrotate 对齐：`sudo timedatectl set-timezone UTC`。

### 2.3 CA 证书

```bash
sudo update-ca-certificates
curl -fsS --max-time 15 https://gamma-api.polymarket.com/events?limit=1 -o /dev/null && echo TLS_OK
```

### 2.4 可选：swap（2C2G 机型）

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

g++ 编译被 OOM killer 终止（`cc1plus: fatal error: Killed signal`）就是内存不足的表现。

---

## 3. 代码部署（/opt/poly-arb-bot）

### 3.1 方式 A：git clone（推荐）

```bash
sudo mkdir -p /opt/poly-arb-bot
sudo chown "$USER":"$USER" /opt/poly-arb-bot
git clone https://github.com/jasonsoldo/poly-arb-bot.git /opt/poly-arb-bot
```

**行尾注意（已修复）**：仓库根目录 `.gitattributes` 已强制 `*.sh` 与 `deploy/*` 以 LF 存储与检出——此前 git 中提交的是 CRLF，Linux clone 出的 bash 脚本会因 `set -euo pipefail\r`、`sleep "60\r"` 之类的回车污染直接报错。如果你手上是修复前的旧 clone，执行 `git add --renormalize . && git checkout -- .` 或重新 clone。`vps_bootstrap.sh` 第 4 步也内置了防御性 CRLF→LF 归一化。

### 3.2 方式 B：rsync（从 Windows 本机推送）

在本机 Git Bash 中：

```bash
rsync -avz --delete \
  --exclude .git --exclude .venv --exclude build \
  --exclude data --exclude logs --exclude state \
  --exclude __pycache__ --exclude .pytest_cache \
  /d/poly-arb-bot/ user@<vps>:/opt/poly-arb-bot/
```

`--exclude` 清单必须保留：`data/`、`logs/`、`state/` 是运行时产物，本机 Windows 的过期货会污染 VPS（例如历史 `live_markets.json` 的 `close_ts` 已过期会导致引擎拒绝启动前的扫描语义混乱）。rsync 之后务必跑 `vps_bootstrap.sh`（其 CRLF 归一化会修复 Windows 工作区带来的行尾）。

### 3.3 禁止事项

- 不要把 `.env` 提交进 git（`.gitignore` 已覆盖；`deploy/env.example` 不含任何 secret）。
- 不要配置钱包私钥、API key 等下单凭据；本部署不存在真实下单路径。

---

## 4. 一键引导（vps_bootstrap.sh）

```bash
cd /opt/poly-arb-bot
bash scripts/vps_bootstrap.sh                    # 仅环境准备
bash scripts/vps_bootstrap.sh --with-build       # + 编译 C++
bash scripts/vps_bootstrap.sh --with-tests       # + 跑 Python 测试
bash scripts/vps_bootstrap.sh --with-systemd     # + 安装 systemd/logrotate（只 enable 不 start）
```

脚本幂等，可重复执行。行为清单：

1. 检测并只安装缺失的 apt 包（非 root 自动走 sudo）；
2. `timedatectl set-ntp true` 并报告同步状态（未同步只警告不失败，因为首启同步有延迟）；
3. clone 或 `git pull --ff-only` 更新代码（目录非空且无 `.git` 时判定为 rsync 流，跳过）；
4. 防御性 CRLF→LF 归一化 `scripts/*.sh` 与 `deploy/*`，并 `chmod +x`；
5. 建 `.venv` 并 `pip install -e . pytest`——**必须装项目本体**，`poly_arb_bot.cli` 顶层 import `shadow_ws` 依赖 `websockets`，只装 pytest 会在运行 `scan-updown` / `web-monitor` 时 ImportError（这是 `deploy/VPS_DEPLOY.md` 旧步骤的缺口，bootstrap 已修正）；
6. 建 `data logs state logs/archive`，touch 四个 JSONL，`.env` 不存在时从 `deploy/env.example` 播种，删除 stale `state/reference-price.sock`；
7. 对全部 `scripts/*.sh` 跑 `bash -n` 自检；
8. 校验 `.env` 中 `POLY_ARB_MODE` 不是 `dry_run` 时直接拒绝继续。

`--with-systemd` 额外做：拷贝两个 unit 与 logrotate 配置到系统目录、安装 hourly logrotate timer drop-in、`systemd-analyze verify`、`daemon-reload`、`enable`（**不 start**，等你审阅 `.env` 并完成第 7 节验收前检查后手动启动）。

环境变量覆盖：`APP_DIR`、`REPO_URL`、`GIT_REF`。

---

## 5. 配置 .env

```bash
cd /opt/poly-arb-bot
cp -n deploy/env.example .env     # bootstrap 已做则跳过
```

逐项审阅 `deploy/env.example`：三套策略阈值、时间窗、费用/滑点/buffer、freshness、clock skew、Shadow 资金约束全部显式配置。重点确认：

- `POLY_ARB_MODE=dry_run`（永远）；
- `SCAN_DEADLINE_SECONDS=45`（AGENTS.md §8 硬 deadline）；
- `SHADOW_CALIBRATION_MODE=1` 只关闭 Shadow 组合层采样节流，市场数据/结算/费用/深度/时钟/策略 EV 检查仍然 fail closed，真实提交恒为 0（见 `deploy/VPS_DEPLOY.md` 首部说明）；
- Linux 下**不要**设置 `CLOCK_SKEW_MS`（引擎在 Linux 通过 `adjtimex` 自动读取内核 NTP 偏移；该变量仅为非 Linux 平台准备）。

---

## 6. C++ 编译

```bash
cd /opt/poly-arb-bot
bash scripts/build_cpp.sh
```

预期：8 个单元测试二进制（reference_snapshot / latest_value_server / latest_value_client / ev_strategy / complete_set_arb / dynamic_position_sizing / observed_arb / microstructure_reversion）全部构建并运行通过，随后产出 `pnl_curve_engine`、`market_engine`、`market_ws_engine`、`reference_price_engine`。OpenSSL 经 pkg-config 探测，缺失时会打印 `skip market_ws_engine`——生产环境不允许跳过，回到 2.1 装 `libboost-system-dev libssl-dev pkg-config`。

## 7. systemd 安装与启动

```bash
sudo cp deploy/poly-arb-bot.service /etc/systemd/system/
sudo cp deploy/poly-arb-web.service /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/poly-arb-bot.service /etc/systemd/system/poly-arb-web.service
sudo systemctl daemon-reload
sudo systemctl enable poly-arb-bot poly-arb-web
sudo systemctl start poly-arb-bot
sudo systemctl start poly-arb-web
sudo systemctl status poly-arb-bot poly-arb-web --no-pager -l
```

要点：

- **启动顺序**：先 `poly-arb-bot`（内部顺序：45s deadline scanner → reference_price_engine → 等 IPC socket → shadow_execution → ev_shadow parity verifier → market_ws_engine），后 `poly-arb-web`（单元已声明 `After=poly-arb-bot.service`）。
- **有界重启**：两个单元均配置 `Restart=always RestartSec=5` + `StartLimitIntervalSec=300 StartLimitBurst=10`（10 次/5 分钟内放弃，避免无控制 restart loop，AGENTS.md §20）。触发上限后恢复：`sudo systemctl reset-failed poly-arb-bot && sudo systemctl start poly-arb-bot`。
- **NTP 门槛**：开机早期 NTP 未同步时 `check_ntp.sh` 会让启动失败并进入有界重试，同步完成后自愈；属预期行为。
- 日志：`journalctl -u poly-arb-bot -f`；文件日志在 `/var/log/poly-arb-bot{,.err}.log` 与 `/var/log/poly-arb-web{,.err}.log`。
- Web/Python 崩溃不影响 C++ 行情引擎；analytics 初次部署可能短暂 `REBUILDING`，属正常。

## 8. 日志轮转（重点）

### 8.1 配置

`deploy/poly-arb-bot.logrotate` 覆盖：

- JSONL 审计：`shadow-audit.jsonl`、`strategy-audit.jsonl`、`shadow-execution.jsonl`、`strategy-parity.jsonl`、`orders.jsonl` —— `daily` + `maxsize 256M`、rotate 30、compress；
- 引擎/服务文本日志：`/var/log/poly-arb-{bot,web}{,.err}.log` —— `daily` + `maxsize 64M`、rotate 30、compress。

`daily` + `maxsize` 即「按天 + 按大小」双触发：到达日切或超过 maxsize 任一条件即轮转。

### 8.2 必须把 logrotate 改为每小时执行

Ubuntu 的 `logrotate.timer` **默认每天只跑一次**，`maxsize` 只在 logrotate 执行时检查。按约 250 MB / 25 分钟的日志增速，每日执行意味着单日可累积十余 GB 未轮转日志。安装 hourly drop-in（`vps_bootstrap.sh --with-systemd` 已自动完成）：

```bash
sudo mkdir -p /etc/systemd/system/logrotate.timer.d
sudo tee /etc/systemd/system/logrotate.timer.d/poly-arb-hourly.conf <<'EOF'
[Timer]
OnCalendar=
OnCalendar=hourly
EOF
sudo systemctl daemon-reload
sudo systemctl restart logrotate.timer
systemctl list-timers logrotate.timer    # 确认 NEXT 在 1 小时内
```

### 8.3 JSONL 行完整性与 copytruncate 的折中

AGENTS.md §21 要求 JSONL 轮转保持每行完整，且「更推荐应用支持 reopen」。当前生产者（C++ `ofstream` 追加、Python append）持有 fd 且不支持 reopen，因此配置选用 `copytruncate`——它保持 inode 不变，web-monitor 等尾随消费者不会断流，也是此类生产者的唯一现实选择。

必须诚实记录的代价：copy 与 truncate 之间存在小竞态窗口，其间写入的行会丢失；truncate 瞬间正在写入的行可能在新文件头部留下半个 JSON 行。下游消费者（web-monitor、acceptance）应跳过残缺行而非整体判失败。当前 Shadow 审计语义下该折中可接受；若未来要求轮转边界严格零丢失，需要为生产者实现 reopen（如 SIGUSR1 重开文件）或在轮转窗口内短暂重启服务——记为后续改进项，本次不改动业务代码。

验证：

```bash
sudo logrotate -d /etc/logrotate.d/poly-arb-bot   # dry-run 预览
sudo logrotate -f /etc/logrotate.d/poly-arb-bot   # 强制轮转一次
ls -lh /opt/poly-arb-bot/logs/
df -h /                                            # 磁盘水位巡检
```

磁盘打满必须视为 DEGRADED/FAILED 事件处理（AGENTS.md §21）。

## 9. 防火墙

web-monitor 单元绑定 `0.0.0.0:8787` 且 **dashboard 无认证**，不得对全网开放。`vps_bootstrap.sh` 故意不触碰防火墙，避免 SSH 锁死；请手动执行：

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH                       # 如改过 SSH 端口，先放行新端口
# 方式一（推荐）：只允许你的管理 IP 访问 8787
sudo ufw allow from <你的管理IP> to any port 8787 proto tcp
sudo ufw enable
sudo ufw status verbose
```

方式二（更严）：改 `poly-arb-web.service` 的 `--host 0.0.0.0` 为 `--host 127.0.0.1`，仅经 SSH 隧道访问：`ssh -L 8787:127.0.0.1:8787 user@<vps>`。改单元后需 `daemon-reload` + restart。

## 10. 官方集成连通性验证

按 `deploy/VPS_DEPLOY.md` §4 执行：Gamma / CLOB / Binance 三个 REST 探针 HTTP 200，然后 `scan-updown` 全量扫描并 `python -m json.tool` 校验产物。硬要求：scanner 在 45 秒全局 deadline 内完成；扫描失败必须保留仍有未结束市场的旧 `live_markets.json`；**零市场不得视为成功**。

## 11. 健康检查与 shadow-acceptance 验收

服务运行并度过暖机（≥5 分钟）后：

```bash
cd /opt/poly-arb-bot
curl -fsS --max-time 30 http://127.0.0.1:8787/api/status -o /tmp/poly-status.json
.venv/bin/python -m poly_arb_bot.cli shadow-acceptance
echo "exit=$?"        # 0=PASS 1=invariant FAIL 2=INCOMPLETE；INCOMPLETE 不得当 PASS
```

再按 `deploy/VPS_DEPLOY.md` §7 的断言脚本核验真实订单不变量（`real_order_submissions / real_orders / real_fills / executed_orders` 全为 0），按 §6 跑 30 分钟 C++/Python parity 窗口（`strategy_parity_mismatch = 0`、`strategy_audit_backpressure = 0`），按 §8 核对本地管道性能门槛（reference IPC receive age p95 < 50 ms、CLOB→策略评估 p95 < 250 µs 等）。

### websocket_stability_budget 观察项（已知问题）

CLOB WebSocket 存在阵发性 EOF 重连。引擎内部自动重连，READY 状态不跨 WS session 保留，重连后重新等待双边完整快照——这是设计行为。验收侧由 `websocket_stability_budget` 检查兜底：

- `WS_STABILITY_MIN_OBSERVATION_SECONDS`（默认 300）：引擎运行不足 5 分钟时稳定性「未观察到」，不阻塞验收；
- 之后要求 `ws_reconnects/h ≤ MAX_WS_RECONNECTS_PER_HOUR`（默认 12）且 `full_resyncs/h ≤ MAX_BOOK_RESYNCS_PER_HOUR`（默认 60）。

部署后至少观察一个完整小时再对 WS 稳定性下结论。若超预算导致 acceptance FAIL，先排查机房网络质量（换机房/换线路），**不要**调高阈值凑过线。

### 其余已知事项

- **日志体量**（约 250 MB / 25 分钟）：靠 8.2 的 hourly logrotate + compress 控制，并把 `df -h` 纳入日常巡检。
- **paired_lock 审计事件 `ts` / `timestamp` 字段名偏差**：不阻塞验收；消费端按兼容处理，记录为后续清理项。
- analytics 初次部署/摘要丢失后短暂 `REBUILDING` 属正常，不得把健康引擎判为 DEGRADED。

## 12. 回滚

parity、性能、acceptance 或真实订单不变量任一失败：

```bash
sudo systemctl stop poly-arb-bot poly-arb-web
cd /opt/poly-arb-bot
git log --oneline -10
git checkout <previous-tested-shadow-commit>
bash scripts/build_cpp.sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
sudo systemctl start poly-arb-bot poly-arb-web
# 重新执行第 11 节验收
```

回滚只能恢复到此前测试过的 Shadow commit；任何情况下不得借回滚或配置变更打开真实下单路径。

## 13. 日常运维速查

```bash
sudo systemctl status poly-arb-bot poly-arb-web --no-pager -l
journalctl -u poly-arb-bot --since '1 hour ago'
tail -F logs/shadow-audit.jsonl logs/strategy-audit.jsonl
jq '{ws_connected, reference_connected, ready_markets, full_resyncs,
     reference_ipc_receive_age_ms_p95, clob_to_strategy_evaluation_us_p95,
     strategy_audit_backpressure}' data/shadow-health.json
.venv/bin/python -m poly_arb_bot.cli shadow-acceptance | jq '{status, failed: [.checks[] | select(.passed | not) | .name]}'
df -h / ; du -sh logs/ /var/log/poly-arb-*.log
```
