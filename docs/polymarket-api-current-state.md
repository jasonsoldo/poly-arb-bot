# Polymarket 官方 API 当前状态核查报告

> 核查日期：2026-07-21（UTC+8 本机时间）
> 核查方式：实际抓取 docs.polymarket.com 官方文档与 changelog，并对 gamma-api.polymarket.com 做了只读实测。
> 本报告按本系统的四个集成点组织，每条注明来源 URL。文末单独列出【需要我们代码改动】清单。

---

## 0. 总览结论（先看这里）

1. **RTDS `crypto_prices_chainlink` 主题仍然存在，名称与字段未变**。文档未说明推送频率或"偏差超阈值才推送"的机制。文档明确要求 **RTDS 每 5 秒发 PING**（而 CLOB market channel 是每 10 秒）——如果我们按 10 秒心跳或不发 PING，连接会被服务端断开/无数据，这很可能是 Chainlink 流 20 分钟级 STALE 的首要嫌疑。
2. **RTDS 官方支持的 crypto symbol 只有 BTC / ETH / SOL / XRP 四个**（Binance 源和 Chainlink 源都是）。BNB / DOGE / HYPE 不在 RTDS 覆盖范围内，必须走交易所直连。
3. **CLOB WSS market channel 订阅格式与端点未变**，`book` / `price_change` 结构与我们预期一致（price_change 自 2025-09-15 起含 `best_bid` / `best_ask` / `hash`）。
4. **CLOB V2（2026-04-28 上线）对只读行情集成基本无影响**：REST host 不变（`https://clob.polymarket.com`）、`/book` 响应结构不变、WSS market channel 不变。签名/pUSD/EIP-712 变化只影响下单路径，我们 Shadow 模式不受影响。
5. **手续费体系已全面改版（Fee Structure V2，2026-03-30）**：费用改为按 market 的 `feeSchedule` 对象（`exponent` / `rate` / `takerOnly` / `rebateRate`）计算，且**自 2026-03-06 起所有 crypto 市场（含 1H/4H）均收 taker 费**。Gamma market 对象现在直接内嵌 `feeSchedule`，实测 BTC 5m 市场为 `{exponent: 1, rate: 0.07, takerOnly: true, rebateRate: 0.2}`。这是对我们 fee 计算逻辑影响最大的变更。
6. **Crypto 短周期 series slug 实测确认**：5m/15m/4h 为 `{asset}-up-or-down-{5m|15m|4h}`，**1h 的 slug 是 `{asset}-up-or-down-hourly`（不是 `-1h`）**。结算源均为 Chainlink Data Streams（`https://data.chain.link/streams/{asset}-usd`）。

---

## 1. 集成点一：Polymarket RTDS（Chainlink 结算参考价）

**来源：https://docs.polymarket.com/market-data/websocket/rtds （抓取于 2026-07-21）**
**changelog 来源：https://docs.polymarket.com/changelog （抓取于 2026-07-21）**

### 1.1 连接与订阅

- 端点：**`wss://ws-live-data.polymarket.com`** —— 未变。
- 订阅消息格式（文档原文）：

```json
{
  "action": "subscribe",
  "subscriptions": [
    { "topic": "crypto_prices_chainlink", "type": "*", "filters": "" }
  ]
}
```

- 单 symbol 过滤为 JSON 字符串：`"filters": "{\"symbol\":\"eth/usd\"}"`。
- **Chainlink symbol 为 slash 格式（`eth/usd`、`btc/usd`）；Binance 源 `crypto_prices` 主题为 lowercase 拼接格式（`btcusdt`）**。两者格式不同，文档明确区分。
- 取消订阅：同样结构，`"action": "unsubscribe"`。支持不断连动态增删订阅。
- **心跳要求：文档明确写 "Send PING messages every 5 seconds to maintain the connection."**（RTDS 是 5 秒；对比 CLOB market/user channel 是 10 秒。见 https://docs.polymarket.com/market-data/websocket/overview ）

### 1.2 消息结构（未变）

```json
{
  "topic": "crypto_prices_chainlink",
  "type": "update",
  "timestamp": 1753314088421,
  "payload": { "symbol": "btc/usd", "timestamp": 1753314088395, "value": 67234.50 }
}
```

payload 字段仅三个：`symbol` / `timestamp`（ms）/ `value`。与主题改名、字段变更相关的 changelog 条目**没有**——主题名 `crypto_prices_chainlink` 自 2025-09-24 RTDS 正式发布沿用至今。

### 1.3 推送频率 —— 文档未说明，需自行实测

- 文档**没有**任何关于 Chainlink 源推送频率、心跳间隔、偏差阈值（deviation threshold）触发推送的说明。"Chainlink 价格只在偏差超阈值时推送"这一说法**在官方文档中不存在**。
- 文档中唯一与频率相关的线索：equity_prices（Pyth 源）写明"sub-second (up to 5 per second per feed)"，crypto_prices / crypto_prices_chainlink 没有类似描述。
- 文档在 Chainlink 小节挂了一个提示：**"Trading 15m Crypto Markets? Get a sponsored Chainlink API key with onboarding support from Chainlink. Fill out this form."** 这暗示 15m crypto 市场相关的 Chainlink 数据存在"赞助 API key"通道；公开 RTDS 流与赞助流的服务等级可能不同。**我们 20 分钟级 STALE 不能排除是公开流的实际推送特性，但结合 5 秒 PING 要求，更优先排查我们自己的心跳与订阅保活。**
- Chainlink Data Streams 本身是低延迟产品（亚秒级报告），20 分钟无更新不符合其正常行为。

### 1.4 支持的 symbol（重要边界）

文档明确列出的支持 symbol：

| 源 | symbol |
|---|---|
| Binance (`crypto_prices`) | `btcusdt` `ethusdt` `solusdt` `xrpusdt` |
| Chainlink (`crypto_prices_chainlink`) | `btc/usd` `eth/usd` `sol/usd` `xrp/usd` |

**BNB / DOGE / HYPE 均不在 RTDS 覆盖内。** 这三个资产的参考价必须来自交易所直连（Binance/Bybit/OKX 等），且没有 Chainlink 结算流可用于交叉验证。

### 1.5 RTDS 相关 changelog

- **2025-09-24**：RTDS 正式发布，含 Binance & Chainlink 两个 crypto 价格源。
- **2026-01-16**："RTDS docs updated to reflect RTDS supports comments and crypto prices only. Removed legacy CLOB references and clob_auth from RTDS docs." —— 即 RTDS 不再承担任何 CLOB 数据职能，只做 comments + 价格流。（此后文档又加入了 equity_prices 主题。）

---

## 2. 集成点二：CLOB WebSocket market channel

**来源：https://docs.polymarket.com/market-data/websocket/market-channel 与 https://docs.polymarket.com/market-data/websocket/overview （抓取于 2026-07-21）**

### 2.1 端点与订阅格式 —— 未变

- 端点：**`wss://ws-subscriptions-clob.polymarket.com/ws/market`**
- 订阅消息：

```json
{
  "assets_ids": ["<token_id_1>", "<token_id_2>"],
  "type": "market",
  "custom_feature_enabled": true
}
```

- `custom_feature_enabled: true` 为可选，开启后额外收到 `best_bid_ask` / `new_market` / `market_resolved` 事件。
- 心跳：**每 10 秒发 `PING`**（文本帧），服务端回 `PONG`。
- 动态订阅：`{"assets_ids": [...], "operation": "subscribe" | "unsubscribe"}`，无需重连。
- 2025-05-28 changelog：market channel 的 100 token 订阅上限已移除；新增可选 `initial_dump` 字段（默认 true，控制是否推送初始订单簿快照）。

### 2.2 消息类型（当前文档共 7 种）

| event_type | 说明 | 需要 custom_feature_enabled |
|---|---|---|
| `book` | 完整订单簿快照（订阅时 + 影响簿的成交后） | 否 |
| `price_change` | 价格档位变动 | 否 |
| `tick_size_change` | tick size 切换（价格 >0.96 或 <0.04 时） | 否 |
| `last_trade_price` | 成交 | 否 |
| `best_bid_ask` | 最优买卖价变化 | 是 |
| `new_market` | 新市场创建（含完整元数据与 `fee_schedule`） | 是 |
| `market_resolved` | 市场结算（含 `winning_asset_id` / `winning_outcome`） | 是 |

### 2.3 price_change 结构（2025-09-15 改版后的现行结构）

```json
{
  "market": "0x5f65...",
  "price_changes": [
    {
      "asset_id": "7132...",
      "price": "0.5",
      "size": "200",
      "side": "BUY",
      "hash": "56621a...",
      "best_bid": "0.5",
      "best_ask": "1"
    }
  ],
  "timestamp": "1757908892351",
  "event_type": "price_change"
}
```

- `size: "0"` 表示该价位从簿上移除。
- 每条 change 自带 `best_bid` / `best_ask` / `hash`（changelog 2025-09-15 "WSS price_change event update"）。
- `book` 消息含 `bids` / `asks` / `timestamp` / `hash` / `market` / `asset_id`。

### 2.4 new_market 事件内嵌 fee_schedule

`new_market` 事件自带 `condition_id`、`clob_token_ids`、`order_price_min_tick_size`、`taker_base_fee`、`fees_enabled` 及 `fee_schedule` 对象：

```json
"fee_schedule": { "exponent": "2", "rate": "0.02", "taker_only": true, "rebate_rate": "0" }
```

字段：`exponent` / `rate` / `taker_only` / `rebate_rate`。

### 2.5 结论

CLOB V2 上线（2026-04-28）**没有**改变 market channel 的端点、订阅格式或消息结构。我们现有的 book / price_change 解析逻辑与当前文档一致（前提是 price_change 解析已是 2025-09-15 之后的结构）。

---

## 3. 集成点三：CLOB REST（订单簿验证）

**来源：https://docs.polymarket.com/api-reference/market-data/get-order-book （OpenAPI: /api-spec/clob-openapi.yaml，抓取于 2026-07-21）**

### 3.1 端点与响应

- `GET https://clob.polymarket.com/book?token_id=<token_id>` —— 生产 host 在 CLOB V2 后**不变**。
- 响应 schema（OrderBookSummary）：`market` / `asset_id` / `timestamp` / `hash` / `bids[]` / `asks[]` / `min_order_size` / `tick_size` / `neg_risk` / `last_trade_price`。
  - `min_order_size` / `tick_size` / `neg_risk` 三个字段是 2025-07-23 changelog 加入的。
  - bids 按价格降序、asks 按价格升序。
- 404：`No orderbook exists for the requested token id`（无簿 token 的明确错误）。

### 3.2 CLOB V2 对只读端点的影响

- 无破坏性变化。V2 的破坏性变更集中在：下单签名（EIP-712 domain version "1"→"2"，Order struct 移除 `nonce`/`feeRateBps`/`taker`，新增 `timestamp`/`metadata`/`builder`）、pUSD 抵押、V1 SDK 废弃。全部属于下单/账户路径。
- 费率不再内嵌在订单里，改为撮合时按 market 的 feeSchedule 收取——对行情解读的影响是：**fee 信息要从 market 元数据（Gamma `feeSchedule` 或 CLOB `getClobMarketInfo(conditionID)` 返回的 `fd = {r, e, to}`）读取**，不再有 per-order 的 feeRateBps。
- 2026-07-17 changelog：POST /order 与 POST /orders 响应不再返回 `transactionHashes`（改返回 `tradeIDs`）——纯下单路径，与我们无关。

### 3.3 其他值得注意的 CLOB REST 变化（2026 changelog）

- 2026-04-10：新增 `GET /markets/keyset` 与 `GET /events/keyset`（cursor 分页），offset 版 `/markets`、`/events` 仍可用但未来将废弃。changelog 未明确标注 host；docs 的 keyset 页面与 Gamma API reference 同族，**具体归属 host 建议实测确认后再决定是否迁移**（我们目前主要用 Gamma `/events`，暂未受影响）。
- 2026-05-14：`GET /markets/keyset` 的 `limit` 上限降为 100。
- 2026-04-09：`GET /markets` 的 `closed` 参数默认值改为 `false`。
- 2026-04-08 / 06-01：多次提高下单类端点 rate limit——只读行情端点不在其中。

---

## 4. 集成点四：Gamma API（市场发现）

**来源：https://docs.polymarket.com/market-data/fetching-markets 、https://docs.polymarket.com/api-reference/series/list-series （OpenAPI: /api-spec/gamma-openapi.yaml）（抓取于 2026-07-21）；另含对 gamma-api.polymarket.com 的只读实测（2026-07-21）**

### 4.1 文档层面

- host `https://gamma-api.polymarket.com` 不变；`/series` `/events` `/markets` 均支持 `slug`（数组）、`limit`/`offset`、`order`/`ascending`、`closed` 等参数。
- `/series` 的 `recurrence` 过滤参数合法值实测为：`daily, weekly, monthly, annual, hourly`（传 `5m` 会返回 validation error）——**不要用 recurrence 过滤 5m/15m，直接用 slug 数组**。
- 官方推荐的市场发现策略：events 端点 + `active=true&closed=false`（events 内嵌 markets）。
- Series schema 含 `events[]`（内嵌 Event→Market 全量字段）；Market schema 当前包含 `feesEnabled`（bool）与 `feeSchedule`（`exponent`/`rate`/`takerOnly`/`rebateRate`）——**feeSchedule 已进 Gamma 官方 schema**。
- 2026-04-09 changelog：`closed` 默认 `false` 同样适用于市场查询（我们若显式传参则不受影响）。

### 4.2 Crypto 短周期 series slug —— 实测结果（2026-07-21）

对 `GET /series?slug=<候选>` 逐一实测：

| 资产 | 5m | 15m | 1h | 4h |
|---|---|---|---|---|
| BTC | `btc-up-or-down-5m` ✅ | `btc-up-or-down-15m` ✅ | `btc-up-or-down-hourly` ✅ | `btc-up-or-down-4h` ✅ |
| ETH | `eth-up-or-down-5m` ✅ | `eth-up-or-down-15m` ✅ | `eth-up-or-down-hourly` ✅ | `eth-up-or-down-4h` ✅ |
| SOL | `sol-up-or-down-5m` ✅ | `sol-up-or-down-15m` ✅ | **`sol-up-or-down-hourly` 未找到** ❌ | `sol-up-or-down-4h` ✅ |
| XRP | `xrp-up-or-down-5m` ✅ | `xrp-up-or-down-15m` ✅ | `xrp-up-or-down-hourly` ✅ | `xrp-up-or-down-4h` ✅ |
| BNB | `bnb-up-or-down-5m` ✅ | `bnb-up-or-down-15m` ✅ | `bnb-up-or-down-hourly` ✅ | `bnb-up-or-down-4h` ✅ |
| DOGE | `doge-up-or-down-5m` ✅ | `doge-up-or-down-15m` ✅ | `doge-up-or-down-hourly` ✅ | `doge-up-or-down-4h` ✅ |
| HYPE | `hype-up-or-down-5m` ✅ | `hype-up-or-down-15m` ✅ | `hype-up-or-down-hourly` ✅ | `hype-up-or-down-4h` ✅ |

注意：

- **1h 周期 slug 后缀是 `-hourly`，不是 `-1h`**（`btc-up-or-down-1h` 不存在）。
- `recurrence` 字段不可靠：BNB/DOGE/HYPE 的所有周期、以及所有 4h series 的 `recurrence` 都显示 `daily`；BTC/ETH/SOL/XRP 的 5m/15m 显示 `5m`/`15m`、1h 显示 `hourly`。**不要用 recurrence 判断周期，用 slug。**
- `sol-up-or-down-hourly` 两次实测均返回空——SOL 的 1h series 当前可能确实不存在或 slug 不同，需要后续再核实（也不排除抓取时遇到限流，已隔 1 秒重试仍为空）。
- 实测 btc 5m series：`{"id":"10684","slug":"btc-up-or-down-5m","recurrence":"5m","active":true,"closed":false,"restricted":true}`。

### 4.3 事件与市场结构 —— 实测样例（2026-07-21）

`GET /events?series_id=10684&order=id&ascending=false&limit=3`（BTC 5m 最新事件）：

```
btc-updown-5m-1784682900 | Bitcoin Up or Down - July 21, 9:15PM-9:20PM ET | active=True closed=False
btc-updown-5m-1784682600 | Bitcoin Up or Down - July 21, 9:10PM-9:15PM ET | active=True closed=False
```

- **事件 slug 模式：`{asset}-updown-{周期}-{窗口起始 unix 秒}`**（注意是 `updown` 无连字符，与 series slug 的 `up-or-down` 不同）。相邻事件 slug 时间戳差 = 周期秒数（300）。
- `resolutionSource`：**`https://data.chain.link/streams/btc-usd`**（Chainlink Data Streams）。
- market.description 明确写："The resolution source for this market is information from Chainlink, specifically the BTC/USD data stream available at https://data.chain.link/streams/btc-usd. Please note that this market is about the price according to Chainlink data stream BTC/USD, not according to other sources or spot markets."
- market 实测字段：
  - `outcomes`: `["Up", "Down"]`，`clobTokenIds`: 2 个 token
  - `feesEnabled: true`
  - **`feeSchedule: {'exponent': 1, 'rate': 0.07, 'takerOnly': True, 'rebateRate': 0.2}`**
  - `takerBaseFee: 1000`、`makerBaseFee: 1000`（旧字段仍存在）
  - `orderPriceMinTickSize: 0.01`、`orderMinSize: 5`
  - `conditionId`、`endDate`（ISO）均正常

### 4.4 结算价来源结论

- 加密短周期 Up/Down 市场结算源为 **Chainlink Data Streams**（`{asset}/usd`），与 RTDS `crypto_prices_chainlink` 主题的 symbol 格式（`btc/usd`）一一对应——我们的 settlement_reference 匹配逻辑可以 `resolutionSource` URL 中的 `{asset}-usd` → RTDS symbol `{asset}/usd` 映射。
- 结算机制本身（UMA Optimistic Oracle 提案/争议流程）见 https://docs.polymarket.com/concepts/resolution ，短周期 crypto 市场为自动提案结算；规则描述以 market.description 为准。

---

## 5. 手续费体系（Fee Structure V2）—— 对我们影响最大的变更

**来源：https://docs.polymarket.com/trading/fees （抓取于 2026-07-21）+ changelog**

### 5.1 当前公式与参数

- 文档公式：**`fee = C × feeRate × p × (1 − p)`**（C=股数，p=价格）。feeSchedule 的 `exponent` 字段（实测 crypto=1，WSS 示例中其他类别=2）暗示完整形式为 `fee = C × rate × [p(1−p)]^exponent`——文档页只给出 exponent=1 的简化形式，**建议以 feeSchedule.exponent 参与计算并与实测成交核对**。
- 分类费率（taker only，maker 永远 0）：

| 类别 | taker rate | maker rebate |
|---|---|---|
| **Crypto** | **0.07** | **20%** |
| Sports | 0.05 | 15%（2026-07-10 起，原 25%→15%，rate 0.03→0.05） |
| Finance / Politics / Mentions / Tech | 0.04 | 25% |
| Economics / Culture / Weather / Other | 0.05 | 25% |
| Geopolitics | 0 | — |

- 精度：**费用舍入到 5 位小数，最小收费 0.00001 USDC**，更小则舍为 0（极低端价格的彩票单可能实际零费）。
- 以 crypto rate=0.07 计，100 股 @ $0.50 费用 $1.75（即每股 $0.0175），曲线关于 50¢ 对称。
- 费用在撮合时由协议收取，订单中不含费率字段。

### 5.2 Crypto 市场费率覆盖时间线（changelog）

- **2026-01-05**：15 分钟 crypto 市场开始收 taker 费，峰值约 1.56%（50% 概率处）。
- **2026-02-12**：**5 分钟 crypto 市场上线**，费用曲线与 15m 相同。
- **2026-03-01**：自 2026-03-06 起，taker 费与 maker rebate **扩展到全部 crypto 市场（1H、4H、daily、weekly）**；只影响 3 月 6 日之后创建的市场。
- **2026-03-30**：Fee Structure V2，按类别费率（上表）。
- **2026-03-31**：**"Fees should now be calculated using the feeSchedule object within a market."** —— 官方明确 feeSchedule 为费率计算数据源。

### 5.3 对我们的含义

- 旧字段 `takerBaseFee: 1000` 在 Gamma 响应中仍存在，但官方口径已转向 `feeSchedule`。AGENTS.md 要求"动态读取市场 fee schedule、禁止 fee 缺失默认按 0"——当前应优先解析 `feeSchedule{rate, exponent, takerOnly, rebateRate}`，缺失时再决定回退策略。
- 1h/4h crypto 市场（2026-03-06 后创建的）同样收费，不能假设长周期无费。
- 低价彩票策略注意：价格接近 0.01 时费用可能舍入为 0，但 $0.05 档 100 股已收 $0.33（crypto），成本链不能忽略。

---

## 6. CLOB V2 上线（2026-04-28）影响评估

**来源：changelog 2026-04-17 与 2026-04-28 条目（https://docs.polymarket.com/changelog ，抓取于 2026-07-21）**

### 6.1 变更内容（changelog 原文要点）

- 新 Exchange 合约（CTF Exchange V2 + Neg Risk CTF Exchange V2）+ 重写的 CLOB 后端 + 新抵押代币 pUSD（Polygon 上标准 ERC-20，USDC 1:1 背书）。
- Order struct：移除 `nonce` / `feeRateBps` / `taker`；新增 `timestamp`(ms) / `metadata` / `builder`。
- 费用改在撮合时确定。
- EIP-712 Exchange domain version `"1"` → `"2"`（ClobAuth 保持 `"1"`）。
- 2026-04-28 ~11:00 UTC 切换，约 1 小时停机；**V1 挂单全部清空，V1 SDK/V1 签名订单不再支持**；生产 URL 不变（`https://clob.polymarket.com`）。

### 6.2 对只读行情集成的影响：**基本为零**

| 我们用的能力 | 是否受 V2 影响 |
|---|---|
| CLOB REST `GET /book` | 否（host 与 schema 不变，见 §3） |
| CLOB WSS market channel（book/price_change） | 否（端点与消息结构不变，见 §2） |
| Gamma series/events 市场发现 | 否（结构不变，且新增 feeSchedule 字段，见 §4） |
| RTDS crypto_prices_chainlink | 否（见 §1） |
| 下单签名 / pUSD / EIP-712 | 是，但我们 Shadow 模式不下单，**无影响**；未来实盘必须整体迁移 V2 SDK（`py-clob-client-v2`）与新签名 |

- 注意：V2 切换时**全部挂单被清空**，意味着切换后订单簿需要重新积累——如果我们的历史 Shadow 数据横跨 2026-04-28，统计口径上应把该日作为流动性断层点标注。
- 官方独立的 "Migrating to CLOB V2" 指南页在 changelog 中有链接，但本次未能在 llms.txt 索引中定位到该页路径（可能已移除或改址）；上表内容以 changelog 条目原文为准。未来做实盘迁移时需重新定位该文档。

---

## 7. 2026 年 changelog 全量梳理（按相关度）

来源：https://docs.polymarket.com/changelog （抓取于 2026-07-21）

### 高相关（直接影响我们的集成）

| 日期 | 条目 | 影响 |
|---|---|---|
| 2026-01-05 | 15m crypto 市场开始收 taker 费 + maker rebate | fee 成本链 |
| 2026-01-16 | RTDS 文档更新：只支持 comments + crypto 价格；移除 CLOB/clob_auth 残留 | RTDS 职责边界 |
| 2026-02-12 | **5m crypto 市场上线**（带费率，曲线同 15m） | 我们 5m 策略的标的来源 |
| 2026-03-01 | 3 月 6 日起费用扩展至**全部 crypto 市场（1H/4H/daily/weekly）** | 长周期市场也有费 |
| 2026-03-30 | Fee Structure V2：分类费率 | fee 计算 |
| 2026-03-31 | **fee 应以 market 内 feeSchedule 对象计算** | fee 数据源 |
| 2026-04-09 | `GET /markets` closed 默认 false | Gamma 查询行为 |
| 2026-04-10 | 新增 keyset 分页端点；offset 版未来将废弃 | 发现层远期迁移项 |
| 2026-04-17 / 04-28 | **CLOB V2 上线**（pUSD、签名、撮合时费率、挂单清空） | 只读无影响；实盘需迁移 |
| 2026-05-14 | `/markets/keyset` limit 上限 100 | 分页 |

### 低相关（记录备查，不影响只读行情）

- 2026-06-01 / 04-08：下单类端点 rate limit 多次提高。
- 2026-06-15：`DELETE /orders` 批量上限降为 1000。
- 2026-07-10：sports taker fee 0.03→0.05、maker rebate 25%→15%。
- 2026-07-14：Relayer 停用 CLOB v1 Neg Risk Adapter（0xd91E…5296），V2 用 0xadA2…eAab。
- 2026-07-17（7 月 24 日 04:00 UTC 生效）：异步成交管线，POST /order(s) 成功响应不再返回 `transactionHashes`，改返回 `tradeIDs`。
- 2026-01-06：Daily Releases、HeartBeats API、Post-Only 订单。
- 2026-01-28 / 06-25：Bridge API 提现端点、X-Builder-Code header。
- 2026-05-18：Data API builders leaderboard/volume 增加 builderCode 字段。

### 2025 年下半年关键条目（背景）

- 2025-09-24：RTDS 正式发布（Binance + Chainlink 价格流）。
- 2025-09-15：`price_change` 消息结构重大变更（现行结构）。
- 2025-07-23：`/book` `/books` 增加 `min_order_size` / `neg_risk` / `tick_size`。
- 2025-05-28：market channel 移除 100 token 订阅上限；新增 `initial_dump` 字段。

---

## 8. 【需要我们代码改动】清单

按优先级排序：

### P0 — 直接关联当前 Chainlink STALE 问题

1. **RTDS 心跳改为 5 秒 PING**。文档明确要求 RTDS 每 5 秒发 PING 保活（CLOB market channel 才是 10 秒）。如果我们对 RTDS 沿用 10 秒心跳或不发应用层 PING，连接会被静默断开，可解释 20 分钟级 STALE。同时确认断线后有自动重连 + 重新订阅。
2. **核对 RTDS 订阅消息格式**：`{"action":"subscribe","subscriptions":[{"topic":"crypto_prices_chainlink","type":"*","filters":""}]}`；单 symbol 过滤是 JSON 字符串 `{"symbol":"eth/usd"}`（slash 格式，非 `ethusdt`）。格式错误会导致连接被关闭或无数据。
3. **为 RTDS Chainlink 流增加独立诊断**：区分"连接断开"、"订阅被拒"、"连接正常但无消息"三种状态；文档未说明 Chainlink 源推送频率，不能假设连续推送。另外关注官方"15m crypto 市场可申请 Chainlink 赞助 API key"的提示——公开流与赞助流服务等级可能不同，长期方案需评估是否申请。

### P1 — fee 计算（官方口径已变）

4. **fee 数据源切换到 market 的 `feeSchedule` 对象**（`rate` / `exponent` / `takerOnly` / `rebateRate`），官方 2026-03-31 changelog 明确以此为计算依据。Gamma market 对象与 WSS `new_market` 事件均已内嵌该字段。
5. **fee 公式核对**：文档给出 `fee = C × rate × p × (1−p)`（crypto exponent=1 实测吻合）；保留 exponent 参与计算（其他类别 exponent=2），并对齐 5 位小数舍入、最小 0.00001 的官方精度规则。
6. **crypto 费率更新为 rate=0.07（taker only），且 1h/4h 市场同样收费**（2026-03-06 后创建的市场）。检查配置中是否还残留旧的"15m 峰值 1.56%"硬编码或按周期区分 fee 的逻辑。
7. **fee schedule 缺失时 fail closed 的逻辑保留**，但注意旧字段 `takerBaseFee` 仍存在——回退顺序应为 feeSchedule → （明确标记降级的）takerBaseFee，禁止静默按 0。

### P1 — 市场发现

8. **series slug 表按实测修正**：1h 后缀是 `-hourly` 而非 `-1h`；全量映射见 §4.2 表。`sol-up-or-down-hourly` 当前未找到，需按 AGENTS.md 要求作为"series 不存在"的合法诊断状态显式处理，不能静默跳过。
9. **不要依赖 Gamma 的 `recurrence` 字段判断周期**（4h 与 BNB/DOGE/HYPE 全周期都显示 `daily`）；周期从 slug 解析。
10. **事件 slug 模式确认**：`{asset}-updown-{周期}-{窗口起始unix秒}`，可用于直接构造 current/next 事件 slug 做定点查询（减少全量扫描），相邻窗口时间戳严格相差周期秒数。
11. **settlement_reference 匹配**：用 event/market 的 `resolutionSource`（`https://data.chain.link/streams/{asset}-usd`）映射到 RTDS symbol `{asset}/usd`；BNB/DOGE/HYPE 无 Chainlink RTDS 流，按 AGENTS.md 应标记 `settlement_reference_unverified` 并默认只允许 Shadow 研究。

### P2 — 健壮性与展示

12. **CLOB WSS 可选启用 `custom_feature_enabled: true`**，获得 `best_bid_ask` / `new_market` / `market_resolved`：`market_resolved` 自带 `winning_asset_id`/`winning_outcome`，可用于 Shadow 结算结果确认；`new_market` 自带 fee_schedule 与 clob_token_ids，可辅助发现。
13. 确认 `price_change` 解析为 2025-09-15 后结构（每条 change 含 `best_bid`/`best_ask`/`hash`，`size:"0"` 表示档位移除）。
14. Web 展示与审计中，RTDS 仅支持 BTC/ETH/SOL/XRP 的事实应反映在 reference 面板上：BNB/DOGE/HYPE 的 Chainlink 源显示 `UNSUPPORTED` 而非 `NOT_RECEIVED`（符合 AGENTS.md §4.5 状态语义）。
15. 若历史 Shadow 数据横跨 2026-04-28（V2 切换、挂单清空），统计时应把该日标记为流动性断层点。
16. （远期）Gamma/CLOB 的 offset 分页端点官方已声明未来废弃，keyset 分页（`after_cursor`/`next_cursor`，limit≤100）是新方向；迁移前需先实测确认 keyset 端点的 host 归属（本次核查未能从 changelog 确认，docs 页面与 Gamma reference 同族但未显式标注）。

### 不需要改动（确认无影响）

- CLOB REST host、`/book` 端点与响应结构 —— 未变。
- CLOB WSS market channel 端点、订阅格式、book/price_change 结构 —— 未变。
- EIP-712 / pUSD / V2 SDK —— 仅下单路径；Shadow 模式零影响（未来实盘再整体迁移）。

---

## 附：本次核查未能完全确认的事项（诚实声明）

1. **RTDS Chainlink 源的推送频率/触发机制**：官方文档无任何说明。"偏差超阈值才推送"的说法未获官方文档支持，也未被否认。20 分钟级 STALE 的根因需结合我们自己的连接日志（心跳、重连、订阅 ack）进一步定位；PING 间隔不匹配是当前最大嫌疑。
2. **独立的 "Migrating to CLOB V2" 指南页**：changelog 中有链接但本次未在文档索引中定位到页面路径，可能已移除或改址；V2 要点以 changelog 原文为准。
3. **`/markets/keyset` 与 `/events/keyset` 的 host 归属**（Gamma vs CLOB）：changelog 未标注，docs 页面未显式说明；对我们当前使用的 Gamma `/events` offset 分页暂无影响（官方只说"未来将废弃"）。
4. **`sol-up-or-down-hourly`**：两次实测返回空数组。可能是该 series 确实不存在、slug 不同，或抓取时限流（已间隔重试）。建议代码侧按"series 不存在"诊断处理并保留复查。
