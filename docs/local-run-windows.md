# 本机（Windows + Git Bash）运行 C++ 引擎指南

本文档记录在 `D:\poly-arb-bot` 本机启动 `market_ws_engine` 与 `reference_price_engine` 的精确命令、参数契约、输入输出路径与已知注意事项。所有运行均为 Shadow / Dry Run，真实订单数恒为 0。

验证日期：2026-07-20（本机实测，结果见文末「Smoke 验证记录」）。

---

## 1. 工具链与依赖位置

| 依赖 | 位置 | 说明 |
|---|---|---|
| MinGW g++ 16.1.0 | `C:\ProgramData\chocolatey\bin\g++.exe`（真实二进制在 `C:\ProgramData\chocolatey\lib\mingw\tools\install\mingw64\bin`） | chocolatey shim 目录不含运行库 DLL |
| Boost 1.91（头文件 + 静态库） | `C:\tools\vcpkg\installed\x64-mingw-static\{include,lib}` | vcpkg triplet `x64-mingw-static`；2026-07-20 补装了 `boost-property-tree`、`boost-beast` |
| OpenSSL 3.6.3（头文件 + 静态库） | `D:\poly-arb-bot\build\tmp\openssl-mingw\ucrt64\{include,lib}` | 从 MSYS2 仓库下载的 `mingw-w64-ucrt-x86_64-openssl-3.6.3-1` 预编译包，7-Zip 解压 |
| CA 证书包 | `C:\Program Files\Git\usr\ssl\certs\ca-bundle.crt`（Git 自带，推荐）或 `D:\poly-arb-bot\build\tmp\cacert.pem`（curl.se 下载） | Windows 上 OpenSSL 无默认 CA store，必须显式指定，否则 TLS 握手报 `certificate verify failed` |

Git Bash 默认 PATH 不含 g++，编译前需要：

```bash
export PATH="/c/ProgramData/chocolatey/bin:/c/ProgramData/chocolatey/lib/mingw/tools/install/mingw64/bin:$PATH"
```

第二个目录（mingw64/bin）含有 `libgcc_s_seh-1.dll`、`libstdc++-6.dll`、`libwinpthread-1.dll`，是运行**非静态链接**测试二进制（如 `latest_value_server_test`）所必需；缺它会 exit 127。

---

## 2. 编译

### 2.1 测试与无 TLS 组件（scripts/build_cpp.sh）

`build_cpp.sh` 为 MSYS2/Linux 编写，不会在本机自动找到 Boost/OpenSSL，需要用环境变量喂路径（Windows 风格、分号分隔在本机实测单路径可用）：

```bash
export PATH="/c/ProgramData/chocolatey/bin:/c/ProgramData/chocolatey/lib/mingw/tools/install/mingw64/bin:$PATH"
export CPATH="C:/tools/vcpkg/installed/x64-mingw-static/include"
export LIBRARY_PATH="C:/tools/vcpkg/installed/x64-mingw-static/lib"
cd /d/poly-arb-bot
bash scripts/build_cpp.sh
```

2026-07-20 实测：`rc=0`，全部测试通过（reference_snapshot / latest_value_server / latest_value_client / ev_strategy / complete_set_arb / dynamic_position_sizing / observed_arb / microstructure_reversion），并构建 `pnl_curve_engine`、`market_engine`。脚本末尾按设计输出 `skip market_ws_engine: install libboost-system-dev libssl-dev`（它只认 `/ucrt64` 或 pkg-config，本机都没有），两个引擎需按 2.2 手动编译。

注意：`CPATH`/`LIBRARY_PATH` 经 Git Bash 传给原生 g++ 的行为偶有异常（同一变量有时不被继承）。若编译报 `boost/...: No such file or directory`，改用命令行 `-I`/`-L` 显式传参（见 2.2，最可靠）。

### 2.2 两个引擎（全静态链接，推荐命令）

与 `scripts/build_cpp.ps1` 相同的静态链接策略，加 `-I`/`-L` 指向本机依赖：

```bash
cd /d/poly-arb-bot
BOOST_I="C:/tools/vcpkg/installed/x64-mingw-static/include"
OSSL="D:/poly-arb-bot/build/tmp/openssl-mingw/ucrt64"

g++ -std=c++17 -O3 -Wall -Wextra -static -static-libgcc -static-libstdc++ \
  -DBOOST_BIND_GLOBAL_PLACEHOLDERS -DBOOST_ERROR_CODE_HEADER_ONLY \
  -I"$BOOST_I" -I"$OSSL/include" \
  cpp/market_ws_engine/market_ws_engine.cpp \
  -o build/market_ws_engine.exe \
  -L"$OSSL/lib" -lssl -lcrypto -lws2_32 -lcrypt32 -lmswsock -lgdi32

g++ -std=c++17 -O3 -Wall -Wextra -static -static-libgcc -static-libstdc++ \
  -DBOOST_BIND_GLOBAL_PLACEHOLDERS -DBOOST_ERROR_CODE_HEADER_ONLY \
  -I"$BOOST_I" -I"$OSSL/include" \
  cpp/reference_price_engine/reference_price_engine.cpp \
  -o build/reference_price_engine.exe \
  -L"$OSSL/lib" -lssl -lcrypto -lws2_32 -lcrypt32 -lmswsock -lgdi32
```

`-DBOOST_ERROR_CODE_HEADER_ONLY` 使 boost.system 头文件化，无需链接 `libboost_system`。
`-static` 产物只依赖 Windows 系统 DLL（KERNEL32/WS2_32/CRYPT32/MSWSOCK/GDI32 + UCRT），可在任意干净环境双击/裸命令启动，不依赖 mingw 运行库 DLL。验证方式：

```bash
objdump -p build/market_ws_engine.exe | grep "DLL Name"   # 应只剩系统 DLL
```

---

## 3. 引擎启动契约

### 3.1 reference_price_engine（先启动）

```
reference_price_engine [venue_status_output.json]
```

- 唯一位置参数：聚合状态输出路径，默认 `data/venue-status.json`。
- 环境变量：
  - `REFERENCE_IPC_PATH`：IPC socket 路径，默认 `state/reference-price.sock`（Windows 为 AF_UNIX）。
  - `CLOCK_SKEW_MS`：**Windows 必需**。源码仅在 Linux 下通过 `adjtimex` 自动测时钟偏移；非 Linux 平台若不设置该变量，`clock_skew_ms = null`，方向/彩票策略将全部以 `clock_skew_unavailable` 拒绝（fail closed，符合规范）。生产本机运行前应设置为实测时钟偏差（毫秒）。
  - `SSL_CERT_FILE`：CA 证书包路径（Windows 必需，见第 1 节）。
- 输出：
  - `data/venue-status.json`（或参数指定路径）：每资产 × 每源的 `status`（FRESH/STALE/DISCONNECTED/NOT_RECEIVED/UNSUPPORTED/OUTLIER）、price/bid/ask、message_age_ms。
  - IPC socket：向 `market_ws_engine` 推送最新参考快照（`reference_snapshot` 协议）。
  - stderr：`REFERENCE_CONNECTED source=...`、`REFERENCE_ERROR ... reconnect_s=2`。
- 订阅的源：binance（`data-stream.binance.vision`）、coinbase、kraken、bybit、okx、chainlink（`ws-live-data.polymarket.com`）。

### 3.2 market_ws_engine（后启动）

```
market_ws_engine <markets.json> [size] [fallback_fee_rate] [audit.jsonl] \
  [buffer_per_share] [min_profit] [leg_interval_us] [execution_half_life_us] \
  [orphan_loss_per_share] [min_expected_value] [health.json] [strategy_audit.jsonl]
```

| # | 参数 | 默认 | 说明 |
|---|---|---|---|
| 1 | markets.json | 必填 | 市场配置，生产为 `data/live_markets.json`；要求 `version`/`generated_at` 元数据、未过期、≤56 个市场、token 不重复、fee/tick/min_order_size 有效 |
| 2 | size | 10 | 目标份额 |
| 3 | fallback_fee_rate | 0.07 | 兜底费率 |
| 4 | audit.jsonl | logs/shadow-audit.jsonl | paired_lock 等审计 |
| 5 | buffer_per_share | 0.002 | 执行 buffer |
| 6 | min_profit | 0.01 | 最低锁定利润 |
| 7 | leg_interval_us | 50000 | 双腿间隔压力模型 |
| 8 | execution_half_life_us | 250000 | 执行半衰期 |
| 9 | orphan_loss_per_share | 0.02 | 孤腿损失 |
| 10 | min_expected_value | 0.01 | 最低 EEV |
| 11 | health.json | data/shadow-health.json | 健康状态 |
| 12 | strategy_audit.jsonl | logs/strategy-audit.jsonl | 策略评估审计 |

- 特殊用法：`market_ws_engine --strategy-config-hash [strategy]` 打印策略配置 hash 后退出。
- 环境变量：`REFERENCE_IPC_PATH`（须与 reference 引擎一致）、`SSL_CERT_FILE`、以及大量策略阈值（`DIRECTIONAL_*`、`LOTTERY_*`、`PAIRED_*`、`MAX_CLOCK_SKEW_MS` 等，见源码 `strategy_config_from_environment`）。
- 行为要点：
  - 连接 `ws-subscriptions-clob.polymarket.com/ws/market`，订阅全部 Up/Down token。
  - 仅在当前 WS session 收到双边完整 book 快照后 READY（stderr `BOOK_BOOTSTRAP_SKIPPED reason=ws_snapshot_required` 属正常）。
  - 每 5 秒检查 markets.json 变更并热切换订阅（generation 隔离）。
  - 断连自动 `WS_RECONNECT delay_s=2`，READY 不跨 session 保留。
- 输入 markets.json 的最小字段见 `load_markets()`（cpp/market_ws_engine/market_ws_engine.cpp:85）：`market_id`、`up_token_id`、`down_token_id`、`fee_rate`、`min_order_size`、`tick_size`、`close_ts`，可选 `condition_id`、`asset`、`interval`、`window`、`open_price`、`settlement_source` 等。

### 3.3 生产启动方式（推荐）

```bash
bash scripts/run_shadow_loop.sh
```

该脚本已处理：日志目录创建、45 秒 scan deadline、Gamma 失败保留缓存、stale socket 清理（`rm -f state/reference-price.sock`）、Git CA bundle 自动探测、启动顺序（scanner → reference 引擎 → shadow_execution → ev_shadow → market_ws_engine）。需要 `.venv`（`PYTHON_BIN` 可覆盖）。VPS 上由 `deploy/poly-arb-bot.service`（systemd）拉起同一脚本。

---

## 4. 已知注意事项（本机实测）

1. **stale IPC socket 导致 reference 引擎崩溃**：`reference_price_engine` 被强杀（timeout/任务管理器）后，`state/reference-price.sock` 残留文件会导致下次启动抛 `bind: ... [system:10048]` 并以 exit 127 终止。启动前必须 `rm -f state/reference-price.sock`（`run_shadow_loop.sh` 已内置此步骤；手动运行时容易踩坑）。
2. **Windows 无默认 CA store**：不设 `SSL_CERT_FILE` 时两个引擎 TLS 全部失败（`WS_ERROR stage=tls ... certificate verify failed`），无限重连但不退出。Git 自带的 `ca-bundle.crt` 可用。
3. **`CLOCK_SKEW_MS`（Windows）**：不设置时方向/彩票策略评估数 >0 但全部 `clock_skew_unavailable` 拒绝。这是 fail-closed 设计，不是故障。
4. **settlement reference**：smoke 输入若缺 `settlement_source`/`open_price`，方向与彩票策略以 `settlement_reference_unavailable` 拒绝（符合 AGENTS.md「settlement reference 未验证前默认禁止 ACCEPT」）。生产 scanner 会填充这些字段。
5. **Binance**：REST API 本机 451（geo-block），但 WebSocket `data-stream.binance.vision` 本机可连且 FRESH（2026-07-20 实测）。参考层 quorum 本机可由 binance/coinbase/kraken/bybit/okx 满足；不要因 REST 451 绕过 Binance WS。
6. **历史 markets JSON 不能直接用于 smoke**：`data/hourly-integration.json` 等 `close_ts` 已过期，`load_markets` 会跳过并以 `no unexpired markets`（exit 1）拒绝启动。smoke 需用当前未过期市场（本任务用 `build/tmp/make_smoke_markets.py` 从 Gamma series 只读拉取生成 `build/tmp/smoke-markets.json`）。
7. **测试二进制的 exit 127**：`build_cpp.sh` 构建的 `latest_value_server_test` 等未静态链接，运行需要 mingw64/bin 在 PATH（libgcc_s_seh-1.dll / libstdc++-6.dll / libwinpthread-1.dll）。两个引擎 exe 已全静态，无此问题。
8. **AF_UNIX 依赖 Windows 版本**：`boost::asio::local::stream_protocol` 需要 Windows 10 17063+，本机可用。

---

## 5. Smoke 验证记录（2026-07-20，本机实测）

编译：`scripts/build_cpp.sh` rc=0（全部单元测试通过）；两个引擎按 2.2 静态编译 rc=0，objdump 确认仅系统 DLL。

联合 smoke（reference 引擎 + market 引擎，55 秒，输入 8 个未过期 BTC/ETH 市场）：

```bash
export SSL_CERT_FILE="D:/poly-arb-bot/build/tmp/cacert.pem"
export CLOCK_SKEW_MS=50
rm -f state/reference-price.sock
timeout 55 ./build/reference_price_engine.exe build/tmp/smoke-venue-status3.json &
sleep 3
timeout 48 ./build/market_ws_engine.exe build/tmp/smoke-markets.json 10 0.07 \
  build/tmp/smoke-audit3.jsonl 0.002 0.01 50000 250000 0.02 0.01 \
  build/tmp/smoke-health3.json build/tmp/smoke-strategy-audit3.jsonl
```

实测结果：

- CLOB WS 连接成功，收到全部 16 个 token 的 book 快照与持续 price_change（252 条 book 消息）。
- health：`ws_connected=true`、`ready_markets=8/8`、`reference_connected=true`、`reference_reconnects=0`。
- 参考行情：6 源全部 `REFERENCE_CONNECTED`；venue-status 中 BTC/ETH 各源 FRESH（binance WS 亦 FRESH）。
- 策略评估：paired_lock 81 次、directional 16 次、lottery 16 次，全部 REJECT（原因分别为成本/阈值类与 `settlement_reference_unavailable`——smoke 输入缺 settlement 字段），0 ACCEPT 属预期。
- `real_order_submissions=0`、`real_orders=0`、`real_fills=0`（审计事件实测）。
- `timeout` 强杀退出码 124 属预期（引擎为常驻进程）。
