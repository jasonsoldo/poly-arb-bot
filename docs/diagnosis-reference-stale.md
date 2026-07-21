# 诊断报告：Kraken / Chainlink STALE 与市场矩阵 REF 0/2

- 诊断日期：2026-07-21（本机实测，Windows + Git Bash）
- 诊断方式：只读。未修改任何仓库代码；临时探测脚本与日志位于 `build/tmp/diag/`（探测脚本为任务明确允许的临时产物）。
- 运行约束：全程 Shadow / Dry Run，未提交任何真实订单。

## 实测产物

| 文件 | 内容 |
|---|---|
| `build/tmp/diag/probe_rtds.py` / `rtds_probe.log` | Polymarket RTDS 原始流探测，观察 190 秒 |
| `build/tmp/diag/probe_kraken.py` / `kraken_probe.log` | Kraken WS v2 原始流探测，观察 190 秒 |
| `build/tmp/diag/venue-status.json` | reference 引擎实测 4 分 20 秒后的最终聚合状态 |
| `build/tmp/diag/engine.stderr.log` / `engine.stdout.log` | 引擎运行日志 |

引擎启动命令（按 `docs/local-run-windows.md` 契约）：

```bash
export SSL_CERT_FILE="C:/Program Files/Git/usr/ssl/certs/ca-bundle.crt"
export CLOCK_SKEW_MS=50
rm -f state/reference-price.sock
timeout 260 ./build/reference_price_engine.exe build/tmp/diag/venue-status.json
```

诊断结束后已确认：无任何 `reference_price_engine.exe` / `market_ws_engine.exe` 进程存活（`state/*.pid` 全部为历史残留），`state/reference-price.sock` 已删除。

---

## 1. Kraken STALE：推送频率本身低于 freshness 阈值（结构性，非故障）

### 实测证据

Kraken v2 订阅（`reference_price_engine.cpp:699`）：

```json
{"method":"subscribe","params":{"channel":"ticker","symbol":["BTC/USD",...,"HYPE/USD"]}}
```

Kraken 订阅确认响应（`kraken_probe.log` 开头）明确写着：

```json
{"method":"subscribe","result":{"channel":"ticker","event_trigger":"trades","snapshot":true,...},"success":true}
```

即 **ticker 频道是成交触发型（`event_trigger=trades`），没有成交就没有推送**。190 秒实测各 symbol 推送间隔：

| Symbol | 消息数 | 平均间隔 | 与 3s 阈值关系 |
|---|---|---|---|
| BTC/USD | 68 | 2.8 s | 临界，FRESH/STALE 闪烁 |
| ETH/USD | 38 | 5.0 s | 大部分时间 STALE |
| SOL/USD | 31 | 6.1 s | 大部分时间 STALE |
| XRP/USD | 26 | 7.3 s | 大部分时间 STALE |
| DOGE/USD | 5 | 38.1 s | 几乎永远 STALE |
| HYPE/USD | 3 | 63.4 s | 几乎永远 STALE |
| BNB/USD | 2 | 95.2 s | 几乎永远 STALE |

引擎 4 分 20 秒实测（`venue-status.json`）：kraken 全部 7 资产 STALE，年龄 3.1s（ETH）到 121.8s（HYPE）不等，与上表间隔完全吻合；`matched_messages=82623`、`unmatched=1`，说明**消息在收、解析正常**。昨日截图（`data/dashboard-live.png` 22:44）也吻合：Kraken BTC 显示 FRESH 2193MS（闪烁到 FRESH 的瞬间），BNB STALE 98601MS、DOGE STALE 45838MS、XRP STALE 35751MS。

freshness 阈值：`REFERENCE_MAX_AGE_MS` 默认 3000ms（`reference_price_engine.cpp:177-204`），只有 coinbase 有 10000ms 特例。Kraken 没有对应的 per-source 阈值。

### 结论

不是"完全收不到"，也不是"解析失败"，而是**推送频率本身低于 3 秒阈值**。与此前已知问题相比没有质的变化（没有恶化到连接死亡——Kraken 每秒 heartbeat 正常、订阅 ack 成功、数据持续到达），只是低流动性 USD 交易对（BNB/DOGE/HYPE，间隔 38–95s）在 3s 阈值下永远 STALE，ETH/SOL/XRP（5–7s）也大部分时间 STALE，造成"Kraken 全部 STALE"的视觉感受。

### 修复建议

按影响从小到大三选一或组合：

1. **加 per-source 阈值**（最小改动）：仿照 `COINBASE_REFERENCE_MAX_AGE_MS`，在 `source_freshness_limit_ms()`（`reference_price_engine.cpp:194-204`）加 `KRAKEN_REFERENCE_MAX_AGE_MS`（默认如 15000）。可让 BTC/ETH/SOL/XRP 大部分转 FRESH，但 BNB/DOGE/HYPE（38–95s 间隔）仍 STALE。
2. **低流动性 symbol 标记 UNSUPPORTED**：`ASSETS`（`reference_price_engine.cpp:152-160`）中把 Kraken 的 BNB/DOGE/HYPE symbol 置空。这些 USD 对流动性不足以充当 3 秒级 fresh 参考，显示 UNSUPPORTED 比永远 STALE 语义更诚实（AGENTS.md 4.6 也要求先验证流动性）。
3. **改订 `book` 频道（深度 10）**：报价变化触发推送，通常比成交触发频繁；需要改订阅与解析代码（`reference_price_engine.cpp:699, 772-783`），是较彻底的方案。

注意：当前 quorum 不依赖 Kraken（Coinbase 提供 fresh USD spot），所以本项优先级是"诊断可读性 + 冗余"，不是解锁 REF。

### 验证方法

重编译后启动引擎 5 分钟，检查 `venue-status.json` 中 kraken 状态与 `message_age_ms` 分布；或直接运行 `python build/tmp/diag/probe_kraken.py` 对比推送间隔与配置的阈值。

---

## 2. Chainlink STALE：当前协议与数据完全健康；截图时状态是运行期静默停滞

### 实测证据（当前状态）

RTDS 原始流探测（`rtds_probe.log`，190 秒）：

- 订阅 `{"action":"subscribe","subscriptions":[{"topic":"crypto_prices_chainlink","type":"*","filters":""}]}` 立即生效；
- 全部 7 个 symbol 持续推送，**每 symbol 约 0.95 条/秒**（间隔约 1.0–1.2s），总 1260 条；
- payload 格式与 C++ 解析器（`reference_price_engine.cpp:734-752`）期望完全一致：

```json
{"topic":"crypto_prices_chainlink","type":"update",
 "payload":{"symbol":"btc/usd","value":65428.49,"timestamp":1784597157000,
            "full_accuracy_value":"65428489795297380000000"}}
```

引擎实测 4 分 20 秒：chainlink 全部 7 资产 FRESH，年龄 0.3–0.5s，无 `REFERENCE_ERROR`、无重连。昨日 22:44 截图放大确认：Chainlink 列当时也全部 FRESH（年龄 811–973MS），聚合显示 QUORUM MET / READY。

### 对"Chainlink 全部 STALE、年龄 578s→20min"的解释

当前无法复现——协议、主题、payload、频率全部正常。该症状（年龄单调增长、其它源 FRESH）指向**运行期静默停滞**，最可能的机制在 `websocket_loop()`（`reference_price_engine.cpp:590-658`）：

- 该循环**没有读超时 / 数据看门狗**。只有在 read/write 返回错误时才重连。
- 每 5 秒应用层写 `"PING"`；TCP/TLS 写成功不代表服务端仍在推送数据。若 RTDS 服务端（或中间代理）静默停止投递数据但保持连接不断开，`async_read` 永远挂起、无错误、永不重连，`received_at` 冻结，年龄无限增长——正是 578s→20min 的形态。
- 次要可能：当时 reference 引擎进程已死，`web_monitor.py:955-956, 980-982` 把不断增长的 `file_age` 叠加到冻结的年龄上（显示效果相同）；但那种情况下**所有**源都会显示 STALE，与"仅 Kraken+Chainlink STALE、其它 FRESH"不符，故静默停滞更符合观察。

由于当时运行的 stderr 未留存，两种机制无法事后完全区分，但修复方向相同（看门狗）。

### 修复建议

在 `websocket_loop()` 内加**数据看门狗**：记录每条消息的到达时间，若超过 N 秒无任何数据（RTDS 正常 ~1 条/秒，N 取 30s 足够保守），主动 cancel 连接并重连。Kraken 正常也有 1/s heartbeat，可同机制复用（阈值按源配置，如 Kraken 60s、RTDS 30s、交易所 15s）。

### 验证方法

修复后运行引擎 30+ 分钟，人为断网/恢复或用防火墙静默丢包模拟停滞，观察 `REFERENCE_ERROR ... reconnect` 是否自动出现、`venue-status.json` 中 chainlink 年龄是否被看门狗重置而非无限增长。

---

## 3. 市场矩阵 REF 0/2 的因果链

### 判定链路

1. Web：`web_monitor.py:1021-1056` 对每个市场调用 `reference_layer.reference_state_for_asset(asset_sources, market.settlement_source, REFERENCE_MAX_AGE_MS=3000)`，quorum 满足计 `reference_ready`，否则 `reference_blocked`；前端 `web/index.html:850` 渲染 `REF rr/n`。
2. 阻止原因优先级（`reference_layer.py:68-79`，C++ `market_ws_engine.cpp:470-476` 完全同构）：
   `insufficient_reference_sources`（fresh 现货源 < 2）→ `required_usd_spot_source_unavailable`（无 fresh USD 现货）→ `settlement_reference_unavailable`（`market.settlement_source` 指定的源不 FRESH 或不存在）→ `cross_source_divergence_exceeded`。
3. C++ 侧 `build_reference_view`（`market_ws_engine.cpp:407-476`）同逻辑决定方向/彩票策略 REJECT 原因；maker 路径（1076-1080 行）输出 `settlement_reference_unverified`。

### 各情形的归属

| 情形 | 影响范围 | 阻止原因 |
|---|---|---|
| **Chainlink 停滞/STALE** | 全部 5m/15m/4h 市场（56 中的 48 个，`settlement_source=chainlink`） | `settlement_reference_unavailable` → 这就是"大部分 REF 0/2"的直接原因 |
| **Kraken STALE（单独发生）** | 无 | Coinbase 仍是 fresh USD spot，`fresh_usd=1` 过线；Kraken STALE 不会单独造成 REF 0/2 |
| **Coinbase + Kraken 同时不 FRESH** | 全部市场 | `required_usd_spot_source_unavailable` |
| **HYPE 1h（永久性）** | HYPE 1h 两个市场 | `settlement_source=binance`，但引擎 HYPE 无 binance symbol（UNSUPPORTED）→ `settlement_reference_unavailable`，永久 REF 0/2 |
| **HYPE 其它周期（脆弱）** | HYPE 5m/15m/4h | binance/bybit UNSUPPORTED，fresh 现货只有 coinbase+okx=2，任一抖动即 < 2 → `insufficient_reference_sources`（今日 08:52 截图正是此原因 + NO REFERENCE DATA——当时 reference 引擎自昨日 22:45 起未运行，`data/venue-status.json` 已 10.9 小时未更新） |

### HYPE 1h 的专项证据

- `data/live_markets.json`：全部 1h 市场 `settlement_source="binance"`（scanner 按市场规则文本关键词派生，`market_scanner.py:230-236`），5m/15m/4h 为 `chainlink`。
- 引擎 `ASSETS` 表（`reference_price_engine.cpp:159`）HYPE 行 binance 为空字符串 → UNSUPPORTED。
- 实测 Binance 现货**不存在 hypeusdt 流**：同一连接 `btcusdt@bookTicker` 35 秒收 2725 条、`hypeusdt@bookTicker` 0 条。
- 昨日 22:44 截图精确吻合：HYPE 1h 永久 REF 0/2，其余 HYPE 格子 2/2；BTC/ETH 等 1h 因 binance FRESH 为 2/2。

HYPE 1h 的 REF 0/2 是 **fail-closed 正确行为**（AGENTS.md 4.6：settlement reference 未验证前禁止方向 ACCEPT），但它是一个永久的配置/数据不匹配，应显式决策而非无声阻塞：

1. 若 Polymarket HYPE 小时市场规则引用的是 Binance HYPEUSDT **永续**价格：可新增 binance perpetual 类型源（`market_type=perpetual`，不参与现货 quorum，仅作 settlement reference），需改 `ASSETS` 与订阅/解析，并按 AGENTS.md 4.6 验证规则匹配后再启用；
2. 若确认无法支持：scanner 侧将 HYPE 1h 标记 `settlement_reference_unverified`（仅 Shadow 研究）或不发现 HYPE 1h，避免矩阵永久红格被误读为故障。

---

## 4. 消息年龄显示 bug（"1198S126MS"）：纯前端格式化问题

### 定位

- **前端**：`web/index.html:573`（参考价格表）：

  ```js
  (isNum(x.message_age_ms)?Number(x.message_age_ms).toFixed(0)+'MS':'AGE N/A')
  ```

  直接拼接原始毫秒数与 `"MS"`。`message_age_ms = 1198126`（≈20 分钟）渲染为 **`1198126MS`**，视觉上被读成 "1198S126MS"。截图中的 `98601MS`、`45838MS`、`35751MS` 同款。顶部 `MSG AGE` chip（`web/index.html:986`）同样裸拼毫秒（截图显示 `MSG AGE 5742MS`）。

- **后端无 bug**：`venue-status.json` 中 `message_age_ms` 是正确数值；`web_monitor.py:980-982` 正确叠加 `file_age`。

### 修复建议

`web/index.html:573` 与 `:986` 复用已有的 `dur()`（`web/index.html:472`，输出 `578S` / `20.0M` / `1.2H`），把毫秒先除 1000；或新增 ms 感知的小工具（< 10s 显示 `xxxMS`，否则显示 `dur(ms/1000)`）。

### 验证方法

浏览器打开仪表盘，参考表中任一 STALE 源年龄应显示如 `98.6S` / `1.6M` 而非 `98601MS`。

---

## 5. 附：其它观察

1. **RTDS 偶发空帧**：探测在连接后 1.9s 收到一个空字符串帧；引擎 4 分钟 `unmatched_messages=1` 与它对应，`websocket_loop` 的 catch 已正确处理，无害。
2. **web_monitor 显示层阈值与策略层不一致**：`web_monitor.py:985-986` 用硬编码 10 秒做 FRESH→STALE 显示覆盖，而策略/quorum 用 `REFERENCE_MAX_AGE_MS`（默认 3s，coinbase 10s）。不影响 quorum 判定，但显示语义与策略语义有偏差，建议统一读取同一配置。
3. **昨日 22:44 截图实际状态健康**：Chainlink 全 FRESH、QUORUM MET/READY、shadow-acceptance 恒等式全 PASS、real orders/submissions = 0。除 Kraken 结构性 STALE 与 HYPE 1h 永久 REF 0/2 外无异常。

## 6. 修复优先级汇总

| # | 问题 | 文件与位置 | 优先级 |
|---|---|---|---|
| 1 | websocket_loop 无数据看门狗 → chainlink 静默停滞后永不重连（REF 0/2 的最大风险源） | `cpp/reference_price_engine/reference_price_engine.cpp:590-658` | 高 |
| 2 | Kraken 3s 阈值与成交触发推送不匹配 | `cpp/reference_price_engine/reference_price_engine.cpp:194-204, 699` | 中 |
| 3 | HYPE 1h settlement_source=binance 但无 Binance 现货 HYPE 流 → 永久 REF 0/2 | `poly_arb_bot/market_scanner.py:230-236` + `reference_price_engine.cpp:159` | 中 |
| 4 | 年龄裸毫秒拼接显示 | `web/index.html:573, 986` | 低 |
| 5 | web_monitor 显示阈值 10s 硬编码与策略 3s 不一致 | `poly_arb_bot/web_monitor.py:955-986` | 低 |

任何修复完成后须按 AGENTS.md 23 节跑对应测试（Kraken/RTDS 解析、freshness、reference quorum、前端 JS 解析、shadow-acceptance），并保持 `real_order_submissions = 0`。
