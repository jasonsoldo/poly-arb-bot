# AGENTS.md

## 1. 项目目的

本仓库实现一个面向 Polymarket 加密货币短周期市场的：

- 低延迟行情系统
- Shadow / Dry Run 交易验证系统
- 多策略正期望值机会发现系统
- Web 监控与结构化审计系统
- 未来可扩展的自动执行系统

本项目**不是单一 paired-lock 双边锁定套利机器人**。

项目的核心目标是综合以下信息：

- Polymarket CLOB 订单簿
- 多交易所 `fast_price`
- 多交易所 `consensus_price`
- settlement reference / Chainlink（当市场规则匹配时）
- fresh exchange source count
- USD spot source count
- cross-source divergence
- 市场 Price to Beat
- 距离结算时间
- 波动率与短时动量
- 订单簿深度与不平衡
- 手续费、滑点、延迟与执行风险

从而发现真实可执行、扣除全部成本后仍具有正期望值的交易机会。

当前生产模式为：

```text
SHADOW / DRY RUN
```

未经用户明确授权，禁止提交任何真实订单。

---

## 2. 不可更改的策略优先级

项目必须保持三套策略独立运行，禁止用一个通用的 `EDGE`、`SCORE` 或 `ACCEPT` 指标混合它们。

策略优先级：

1. `late_window_directional_ev`
2. `low_price_lottery_ev`
3. `paired_lock`

其中：

- `late_window_directional_ev` 是主策略
- `low_price_lottery_ev` 是主策略
- `paired_lock` 是低频辅助策略

禁止再次把整个项目改成只寻找：

```text
Up + Down + fees + buffer < 1
```

的双边锁定套利系统。

每种策略必须独立拥有：

- 输入字段
- 评估逻辑
- 风控规则
- ACCEPT 条件
- REJECT 原因
- 审计事件
- Shadow 统计
- PnL 统计
- Web 展示

---

## 3. 支持市场

### 资产

市场发现与 CLOB 监控支持：

- BTC
- ETH
- SOL
- XRP
- BNB
- DOGE
- HYPE

### 周期

支持：

- 5m
- 15m
- 1h
- 4h

### 市场窗口

每个资产、每个周期最多保留：

- current
- next

最大市场数：

```text
7 资产 × 4 周期 × 2 窗口 = 56 个市场
```

系统不得假设 56 个市场永远全部存在。

以下状态都属于合法但必须明确诊断的状态：

- series 不存在
- event 不存在
- 市场尚未创建
- 市场已经结束
- condition ID 缺失
- Up/Down token 缺失
- CLOB 订单簿不可用
- 市场已发现但尚未 WS READY

这些状态不能被静默当成成功。

---

## 4. 多交易所参考价格层

方向策略不得再把 Binance 作为唯一参考价格来源。

项目必须实现独立的多交易所参考价格层：

```text
REFERENCE PRICE LAYER
├── Binance
├── Coinbase
├── Kraken
├── Bybit
├── OKX
└── Chainlink
```

其中：

- Binance、Coinbase、Kraken、Bybit、OKX 属于交易所行情源
- Chainlink 属于结算参考与外部确认源
- Polymarket CLOB 属于交易场所与实际成交价格来源

这三类数据源的职责不同，禁止混为一个价格。

### 4.1 数据源优先级

推荐接入顺序：

1. 修复并保留 Binance
2. 接入 Coinbase
3. 接入 Kraken
4. 实现多源 consensus 与 outlier 检测
5. 接入 Bybit
6. 接入 OKX

不得因为 Binance 当前故障就删除或永久绕过 Binance。新增交易所的目的必须是提高冗余和鲁棒性，而不是用新源掩盖旧解析错误。

### 4.2 市场类型分组

不同交易所的价格必须按市场类型分组：

```text
USD SPOT
├── Coinbase BTC-USD
└── Kraken BTC/USD

USDT SPOT
├── Binance BTCUSDT
├── Bybit BTCUSDT
└── OKX BTC-USDT

PERPETUAL
├── Binance perpetual
├── Bybit linear perpetual
└── OKX swap
```

禁止直接把 USD 现货、USDT 现货和永续合约价格简单平均。

每条行情必须明确记录：

- `source`
- `asset`
- `symbol`
- `market_type`
- `quote_currency`
- `price`
- `bid`
- `ask`
- `source_timestamp`
- `received_at`
- `message_age_ms`
- `status`

### 4.3 三类参考价格

聚合层必须明确区分：

```text
fast_price
consensus_price
settlement_reference
```

#### fast_price

用于：

- 短时动量
- 领先信号
- 微结构变化
- 尾盘快速方向判断

可优先使用更新频率高且流动性好的交易所行情，例如：

- Binance
- Bybit
- OKX

#### consensus_price

用于判断多个独立交易所是否一致。

默认应使用通过 freshness 和 outlier 检查后的有效现货源中位数：

```text
consensus_price = median(valid_normalized_spot_prices)
```

禁止默认使用简单算术平均值。

#### settlement_reference

用于：

- 结算规则确认
- Price to Beat 对照
- 结算风险检查

应优先使用该 Polymarket 市场规则明确指定的价格源。

Chainlink 只能在市场规则与对应 feed 匹配时作为 settlement reference。

### 4.4 Reference quorum

方向策略不得再硬编码为“Binance 必须可用”。

默认准入条件应升级为可信参考源法定人数：

```text
min_fresh_exchange_sources >= 2
require_usd_spot_source = true
require_settlement_reference = true
max_cross_source_divergence_bps = configurable
```

例如：

```text
Binance = NOT_RECEIVED
Coinbase = FRESH
Kraken = FRESH
Chainlink = FRESH
REFERENCE = READY
```

这种情况下方向策略可以继续评估。

如果只有一个交易所源，或只有 Chainlink：

```text
decision = REJECT
reason = insufficient_reference_sources
```

### 4.5 每个来源的状态

每个行情源必须区分：

```text
FRESH
STALE
DISCONNECTED
NOT_RECEIVED
UNSUPPORTED
OUTLIER
```

禁止把 `price = null` 显示成 `STALE`。

状态语义：

- 从未收到有效价格：`NOT_RECEIVED`
- 连接已断开：`DISCONNECTED`
- 收到过价格但超过 freshness 阈值：`STALE`
- 当前价格有效：`FRESH`
- 官方或实现不支持：`UNSUPPORTED`
- 与可信共识偏差过大：`OUTLIER`

聚合状态必须区分：

```text
REFERENCE_READY
REFERENCE_DEGRADED
REFERENCE_BLOCKED
```

### 4.6 资产支持规则

BTC、ETH、SOL、XRP 应优先接入：

- 至少两个交易所源
- 至少一个 USD 现货源
- 合适的 settlement reference

BNB、DOGE、HYPE 可以接入其他交易所现货行情，但必须先验证：

- symbol 对应同一真实资产
- market type 已明确
- 流动性达到最低标准
- quote currency 已标准化
- 与 Polymarket 市场结算规则相容

尤其 HYPE，必须防止：

- 同名不同资产
- 用永续价格冒充现货价格
- 流动性不足
- 价格源与结算规则不一致

在 settlement reference 未验证前：

```text
settlement_reference_unverified
```

此时只允许 Shadow 研究，默认禁止方向策略正式 ACCEPT。

### 4.7 paired_lock 的关系

多交易所参考价格只属于 paired_lock 的辅助观测数据：

```text
REFERENCE ONLY
NOT USED FOR PAIRED-LOCK ACCEPTANCE
```

paired_lock ACCEPT 只能依据：

- Polymarket Up/Down 订单簿
- 双边 VWAP
- 双腿深度
- fee
- execution buffer
- book freshness/sync
- execution pressure model

不得因为参考交易所价格变化而直接接受或拒绝 paired_lock。

---

## 5. 主策略 A：尾盘方向正 EV

策略名称：

```text
late_window_directional_ev
```

这是项目主策略之一。

该策略通过估计 Up 或 Down 的真实结算概率，并与 Polymarket 的实际可成交价格比较，寻找扣除成本后仍为正的方向机会。

这不是无风险套利。

### 核心公式

按每份合约计算：

```text
gross_edge =
    estimated_probability
    - expected_fill_price

net_ev_per_share =
    estimated_probability
    - expected_fill_price
    - fees_per_share
    - slippage_per_share
    - latency_risk_buffer
    - settlement_risk_buffer
```

允许使用总金额口径，但审计必须明确：

- per-share
- total-dollar

不能混用。

### 最低输入要求

方向策略至少需要：

- asset
- timeframe
- outcome
- Polymarket 当前价格
- 目标规模 expected fill price
- 多交易所 `fast_price`
- 多交易所 `consensus_price`
- settlement reference / Chainlink（当市场规则匹配时）
- fresh exchange source count
- USD spot source count
- cross-source divergence
- Price to Beat
- 参考价格与 Price to Beat 的距离
- seconds_to_close
- reference message age
- CLOB book age
- 短周期波动率
- 短周期动量
- 订单簿不平衡
- 可成交深度
- 市场 fee schedule
- 预估滑点
- clock skew
- settlement source 状态

### 必须输出

- `strategy`
- `estimated_probability`
- `market_implied_probability`
- `market_price`
- `expected_fill_price`
- `gross_edge`
- `fees`
- `slippage`
- `latency_risk_buffer`
- `settlement_risk_buffer`
- `net_ev`
- `confidence`
- `seconds_to_close`
- `price_to_beat`
- `reference_price`
- `distance_to_price_to_beat`
- `decision`
- `reason`

### 概率语义

`estimated_probability` 只能表示模型估计。

禁止描述成：

- 确定概率
- 保证胜率
- 锁定利润
- 已验证胜率
- 历史胜率，除非确有真实历史统计

模型 confidence 不等于历史准确率。

### 默认时间窗口

方向策略必须按周期配置独立尾盘窗口。

Shadow 初始范围可为：

```text
5m：最后 15–90 秒
15m：最后 20–180 秒
1h：最后 30–300 秒
4h：最后 60–600 秒
```

这些只是初始 Shadow 参数，不是永久硬编码。

禁止把 paired_lock 的宽窗口直接用于方向策略。

### 必须 fail closed 的情况

以下任一条件成立时，方向策略禁止 ACCEPT：

- 参考资产 unsupported
- Price to Beat 缺失
- settlement source 无效
- reference quorum 未满足
- 没有 fresh USD spot source
- settlement reference 缺失或未验证
- cross-source divergence 超过阈值
- reference data stale
- CLOB book stale
- clock skew 超阈值
- 目标规模深度不足
- net EV 未过线
- 不在策略时间窗口
- 市场已结束
- 市场不可交易

---

## 6. 主策略 B：低价彩票正 EV

策略名称：

```text
low_price_lottery_ev
```

这是项目主策略之一。

目标是寻找低价 outcome，通常约：

```text
$0.01–$0.05
```

具体范围必须配置化。

禁止仅因为 outcome 很便宜就买入。

### 核心公式

```text
net_ev_per_share =
    estimated_probability
    - expected_fill_price
    - fees_per_share
    - slippage_per_share
    - model_uncertainty_buffer
    - execution_risk_buffer
```

只有当模型估计真实概率明显高于实际可成交价格，并且扣除全部成本后仍超过最低 EV 阈值时，才允许 ACCEPT。

### 必须配置的风控

- maximum entry price
- maximum order size
- maximum notional per market
- maximum total lottery exposure
- maximum daily lottery loss
- minimum liquidity
- minimum net EV
- maximum slippage
- maximum order-book age
- maximum consecutive loss limit
- 可选 portfolio correlation limit

### 统计语义

该策略天然可能出现：

- 较低命中率
- 长连续亏损
- 正偏分布收益
- 高方差

不能用少量样本判断好坏。

必须统计：

- sample count
- average entry price
- realized hit rate
- expected hit rate
- average EV at entry
- realized PnL
- maximum drawdown
- losing streak distribution
- probability calibration buckets

在 completed simulations 或真实结算样本为 0 时：

```text
WIN RATE = N/A
SHARPE = N/A
COMPLETED PNL = N/A
```

---

## 7. 辅助策略 C：双边锁定套利

策略名称：

```text
paired_lock
```

这是低频辅助策略，不是项目主盈利模式。

它把同一市场的 Up 和 Down 视为一条组合机会。

禁止使用：

```text
model_probability > expected_fill_price
```

作为 paired_lock 的判断条件。

### 成本链

按相同目标份额：

```text
gross_cost =
    up_vwap_cost
    + down_vwap_cost

net_cost =
    gross_cost
    + up_fee
    + down_fee
    + execution_buffer

guaranteed_payout =
    fully_covered_equal_share_quantity

locked_profit =
    guaranteed_payout
    - net_cost

locked_roi =
    locked_profit / net_cost
```

### ACCEPT 条件

必须同时满足：

- Up/Down 属于同一 condition
- token ID 有效
- 当前 WS session 已收到 Up 完整快照
- 当前 WS session 已收到 Down 完整快照
- 双边订单簿 fresh
- 双边时间戳差异不超过阈值
- 双腿目标数量均有完整深度
- 双腿 FOK 深度模拟通过
- fee schedule 可用
- 双腿费用独立计算
- 双腿费用独立按官方规则舍入
- execution buffer 已加入
- `locked_profit` 超过最低阈值
- `expected_execution_value` 超过最低阈值
- 市场处于 paired_lock 时间窗口
- 市场未结束

### paired_lock 时间窗口

可使用更宽窗口，例如：

```text
20–7200 秒
```

因为它依赖双边净成本，而非方向判断。

### 执行压力模型

Shadow 可输出：

- `leg_1_fill_probability`
- `leg_2_fill_probability`
- `time_between_legs_us`
- `orphan_leg_loss`
- `expected_execution_value`

除非经过真实历史数据校准，否则这些只能标记为：

```text
配置型压力模型
```

禁止称为历史成交概率。

### 常见拒绝原因

- `net_cost_above_threshold`
- `locked_profit_below_threshold`
- `eev_below_threshold`
- `up_depth`
- `down_depth`
- `books_not_synced`
- `up_book_stale`
- `down_book_stale`
- `waiting_up_snapshot`
- `waiting_down_snapshot`
- `fee_schedule_unavailable`
- `outside_time_window`
- `market_expired`

长时间没有 paired_lock ACCEPT 属于正常现象。

禁止为了制造 ACCEPT 而降低 fee、buffer、freshness、sync、depth、EEV 标准。

---

## 8. 市场发现

必须使用官方 Gamma Series / Events 进行发现。

市场发现必须：

- 扫描配置资产与周期
- 解析 current 与 next
- 解析 event ID
- 解析 market ID
- 解析 condition ID
- 解析 Up/Down token ID
- 解析 end time
- 解析 fee schedule
- 拒绝格式不完整市场
- 验证 Up/Down CLOB
- 每个 asset/timeframe 最多保留一个 current 和一个 next

### 批量发现

优先使用 Gamma Series / Events 批量发现。

禁止针对每个 slug 发起低效的单独 Gamma 请求，除非确有必要且经过验证。

CLOB 验证可以并发，但并发必须有上限。

禁止伪并发：

```python
for item in items:
    future = pool.submit(validate, item)
    result = future.result()
```

正确做法是先提交，再统一等待完成。

### 扫描硬 deadline

完整扫描默认 deadline：

```text
45 秒
```

必须覆盖整个流程：

- Gamma 请求
- 解析
- 候选过滤
- CLOB 验证
- 输出构建
- 原子发布

必须使用 monotonic global deadline。

后续每个请求只能使用剩余时间预算。

### 扫描失败行为

Gamma 或 CLOB 故障时：

- 不得把有效旧配置替换为空列表
- 必须保留旧 `live_markets.json`
- 仅允许继续使用其中尚未结束的市场
- discovery 状态必须标记 degraded
- 必须记录 cache age
- 必须记录失败原因
- 零市场结果不得默认当作成功

### 原子发布

市场文件必须：

```text
写临时文件
→ flush
→ 必要时 fsync
→ atomic rename
```

禁止让 C++ 读取半写入 JSON。

每次扫描输出至少包含：

- `scan_id`
- `scan_generation`
- `generated_at`
- `source_status`
- `market_count`
- `scanner_version`

超时扫描或旧 scan 不得在新 scan 已成功后覆盖新结果。

---

## 9. C++ 低延迟行情引擎

低延迟行情链路由 C++ 实现。

可使用 Boost.Beast 或其他经批准组件。

### REST

REST 全量订单簿可用于：

- 预热
- 诊断
- 一致性检查
- 恢复辅助

REST 预热不能直接把市场置为 READY。

### WebSocket

引擎必须支持：

- 初始 JSON array
- 完整 `book` 快照
- `price_change`
- heartbeat
- EOF 检测
- 自动重连
- full resync
- 动态 subscribe
- 动态 unsubscribe
- current → next 热切换

### READY 规则

paired market 只有满足以下条件才能 READY：

- 当前 WS session 收到 Up 完整 book
- 当前 WS session 收到 Down 完整 book
- freshness 通过
- timestamp sync 通过
- token 属于当前 generation
- 市场 active 且未结束

完整快照之前收到的 `price_change`：

- 可以缓存或忽略
- 不能直接把订单簿标记 READY
- 不能参与正式评估

### 重连状态

发生 EOF、断连、订阅替换或 full resync 时：

```text
READY
→ NOT_READY
→ 清除旧 WS readiness
→ 收到新的 Up 完整快照
→ 收到新的 Down 完整快照
→ freshness/sync 检查
→ READY
```

旧 READY 状态禁止跨 WS session 保留。

### generation/session 隔离

所有 book state 与 event state 必须绑定：

- market ID
- condition ID
- token ID
- subscription generation
- WS session ID

旧 generation 或旧 session 的迟到消息必须丢弃。

### 订单簿完整性

以下情况应触发 resync 或 fail closed：

- crossed book
- negative size
- 不可能的 level mutation
- timestamp rollback
- stale book
- unknown token
- old generation
- old session
- hash mismatch
- REST consistency mismatch
- 缺少完整快照

---

## 10. 多交易所参考行情状态

C++ 引擎必须以统一接口维护多个参考行情源，而不是为每个交易所散落独立状态逻辑。

每条 reference source state 必须包含：

- asset
- source
- symbol
- market_type
- quote_currency
- price
- bid
- ask
- source timestamp
- local receive timestamp
- message age
- freshness threshold
- connection status
- support status
- outlier status

系统必须区分：

```text
fresh
stale
disconnected
not_received
unsupported
outlier
```

禁止把这些状态全部合并成 `N/A`。

聚合层必须输出：

- `fresh_exchange_source_count`
- `fresh_usd_spot_source_count`
- `consensus_price`
- `fast_price`
- `settlement_reference`
- `cross_source_divergence_bps`
- `reference_quorum_met`
- `reference_state`
- `reference_block_reason`

方向策略只有在 reference quorum 满足时才允许继续进入概率与 EV 计算。

若 quorum 未满足：

```text
decision = REJECT
reason = insufficient_reference_sources
```

若 settlement reference 缺失：

```text
decision = REJECT
reason = settlement_reference_unavailable
```

若多个来源分歧超过阈值：

```text
decision = REJECT
reason = cross_source_divergence_exceeded
```

paired_lock 不依赖 reference quorum，参考行情只能标记为 `REFERENCE ONLY`。

---

## 11. 手续费与成本

必须动态读取市场 fee schedule。

禁止 fee 缺失时默认按 0 处理。

若 fee schedule 缺失或格式错误：

```text
decision = REJECT
reason = fee_schedule_unavailable
```

paired_lock 双腿费用必须独立计算：

```text
up_fee_raw
up_fee_rounded
down_fee_raw
down_fee_rounded
total_fee
```

官方舍入规则必须先对每条腿独立执行，再求和。

审计必须记录：

- fee rate
- fee formula version
- raw fee
- rounded fee
- fee units/currency

execution buffer 必须独立、可配置、可审计。

禁止把 buffer 偷藏在 VWAP 或 fee 中。

---

## 12. Shadow 模式规则

当前生产模式为 Shadow / Dry Run。

必须永久满足：

```text
real_order_submissions = 0
real_orders = 0
real_fills = 0
realized_real_pnl = 0 或 N/A
```

Shadow ACCEPT 仅表示：

```text
当前机会通过了配置的策略与风控门槛
```

它不代表：

- 已下单
- 一定成交
- 一定盈利
- 已产生真实 PnL

### Completed Shadow simulation

只有具备完整生命周期与结算结果的模拟交易，才能计入 completed trade。

单次 evaluation 或 ACCEPT 不能计入 completed trade。

当 completed sample count 为 0：

- completed PnL = N/A
- win rate = N/A
- Sharpe = N/A
- average trade profit = N/A

---

## 13. 未来实盘门槛

实盘不在当前范围内。

禁止仅通过修改一个 mode flag 打开真实下单。

未来实盘实现必须具备：

- 用户明确授权
- 独立 live 配置
- 钱包与凭据安全
- balance 检查
- allowance 检查
- deterministic client order ID
- 幂等下单
- order acknowledgement
- fill confirmation
- partial fill 处理
- cancellation
- timeout
- retry policy
- orphan-leg 状态机
- emergency kill switch
- max exposure
- daily loss limit
- reconciliation
- real PnL accounting
- incident logging
- live-specific tests
- 小额灰度方案

paired_lock 实盘状态机至少需要：

```text
IDLE
PRECHECK
LEG_1_SUBMITTED
LEG_1_FILLED
LEG_2_SUBMITTED
COMPLETE
```

失败状态至少包括：

```text
LEG_1_REJECTED
LEG_2_REJECTED
PARTIAL_FILL
ORPHANED
HEDGING
EMERGENCY_EXIT
HALTED
```

在完整状态机完成并批准前，真实订单数必须保持 0。

---

## 14. 审计事件

Canonical Shadow 审计格式为 JSONL。

默认路径：

```text
logs/shadow-audit.jsonl
```

每行必须是完整 JSON object。

### 必须包含的事件身份字段

- `event_id`
- `event_type`
- `strategy`
- `market_id`
- `condition_id`
- `asset`
- `timeframe`
- `window`
- `generation`
- `session`
- `evaluation_sequence`
- `timestamp`

### 稳定事件身份

event identity 必须能区分：

- 重复评估
- 新 generation
- 新 WS session
- JSONL replay
- current → next 切换

生产者生成的 `event_id` 必须被：

- Shadow execution
- Web consumer
- acceptance checker

继续沿用。

下游禁止为同一事件重新生成不同 ID。

### 去重规则

重复事件必须：

- 不计入 evaluations
- 不计入 rejection totals
- 不计入 opportunity totals
- 不计入 PnL
- 计入 `duplicate_events`

`duplicate_events` 是观测指标，不是有效评估。

### 通用字段

- `decision`
- `reason`
- `books_ready`
- `books_fresh`
- `books_synced`
- `seconds_to_close`
- `real_order_submissions`
- `real_orders`

### 方向策略字段

- `outcome`
- `market_price`
- `expected_fill_price`
- `estimated_probability`
- `market_implied_probability`
- `gross_edge`
- `fees`
- `slippage`
- `latency_risk_buffer`
- `settlement_risk_buffer`
- `net_ev`
- `fast_price`
- `consensus_price`
- `settlement_reference`
- `fresh_exchange_source_count`
- `fresh_usd_spot_source_count`
- `cross_source_divergence_bps`
- `reference_quorum_met`
- `reference_state`
- `reference_price`
- `price_to_beat`
- `distance_to_price_to_beat`
- `reference_age_ms`
- `book_age_ms`

### paired_lock 字段

- `target_size`
- `up_vwap`
- `down_vwap`
- `up_cost`
- `down_cost`
- `gross_cost`
- `up_fee`
- `down_fee`
- `total_fees`
- `buffer`
- `net_cost`
- `guaranteed_payout`
- `locked_profit`
- `locked_roi`
- `up_depth_ok`
- `down_depth_ok`
- `leg_1_fill_probability`
- `leg_2_fill_probability`
- `time_between_legs_us`
- `orphan_leg_loss`
- `expected_execution_value`
- `up_age_ms`
- `down_age_ms`
- `book_skew_ms`

---

## 15. 机会持续时间

机会持续时间必须按完整资格条件计算。

paired_lock 机会只有在以下条件同时成立时才开始：

- books READY
- books fresh
- books synced
- 双腿深度足够
- fee 可用
- locked profit 过线
- EEV 过线

任意条件失效，机会结束。

禁止仅通过：

```text
best_ask_up + best_ask_down < 1
```

计算 opportunity duration。

方向策略也必须按该策略完整资格条件计算持续时间。

所有机会持续时间必须按 strategy 分组。

---

## 16. Web Dashboard

Web 是监控与审计界面，不是营销页面。

只允许展示 canonical audit 与 health state 中的真实数据。

### 禁止展示

禁止：

- 假订单数
- 假 PnL 曲线
- 假 equity curve
- 假 latency bar
- `SAFE`
- 无依据的 `VERIFIED`
- 把模型概率当套利评分
- 把 Shadow ACCEPT 当真实 PnL
- 用 100 作为通用策略分数
- 把 evaluation 当 completed trade

### 策略必须分开展示

Web 必须分为：

- DIRECTIONAL EV
- LOW-PRICE LOTTERY
- PAIRED LOCK

禁止再次合成一个通用 EDGE。

### 推荐全局计数

- configured markets
- active markets
- expired skipped
- discovered markets
- paired markets READY
- reference-ready markets
- not-ready markets
- waiting Up snapshot
- waiting Down snapshot
- total evaluations
- directional ACCEPT
- lottery ACCEPT
- paired-lock ACCEPT
- completed Shadow trades
- duplicate events
- full resyncs
- real orders
- real submissions

### paired_lock 展示

必须展示完整成本链：

```text
UP VWAP
DOWN VWAP
GROSS COST
UP FEE
DOWN FEE
TOTAL FEES
EXECUTION BUFFER
NET COST
GUARANTEED PAYOUT
LOCKED PROFIT
LOCKED ROI
EEV
DECISION
REASON
```

### directional 展示

必须展示：

```text
OUTCOME
ESTIMATED PROBABILITY
MARKET PRICE
EXPECTED FILL
GROSS EDGE
FEES
SLIPPAGE
RISK BUFFER
NET EV
SECONDS TO CLOSE
REFERENCE PRICE
PRICE TO BEAT
REFERENCE DIVERGENCE
DECISION
REASON
```

### 验证状态

当最终 decision = REJECT：

```text
VALIDATE = BLOCKED
APPROVE = BLOCKED
DEPLOY = BLOCKED
```

但以下指标仍应显示真实状态：

- DEPTH
- FRESHNESS
- BOOK SYNC
- LEG RISK

### Message age

没有真实端到端 latency 测量时，只能显示：

```text
MESSAGE AGE
```

不能显示：

```text
LATENCY
```

未知值显示 `N/A`。

### 参考行情

Web 必须按交易所逐源展示：

- source
- symbol
- market type
- price
- bid/ask
- message age
- status

并单独展示聚合状态：

```text
FAST PRICE
CONSENSUS PRICE
SETTLEMENT REFERENCE
FRESH SOURCES
USD SPOT SOURCES
CROSS-SOURCE DIVERGENCE
REFERENCE QUORUM
REFERENCE STATE
```

状态必须使用：

```text
FRESH
STALE
DISCONNECTED
NOT RECEIVED
UNSUPPORTED
OUTLIER
```

禁止在没有价格时显示 `STALE`。

paired_lock 页面中的全部外部交易所和 Chainlink 必须标记：

```text
REFERENCE ONLY
NOT USED FOR PAIRED-LOCK ACCEPTANCE
```

方向策略页面必须明确显示当前依赖的 reference quorum 是否满足。

---

## 17. 健康检查与验收命令

仓库必须提供机器可执行的：

```text
shadow-acceptance
```

至少验证：

```text
ready + not_ready = active_discovered
accepted + rejected = evaluations
sum(rejection_reasons) = rejected
real_orders = 0
real_submissions = 0
```

去重后：

```text
duplicates 不计入 evaluations
duplicates 单独统计
```

### 空状态行为

以下情况不得 PASS：

- active market = 0
- audit file 为空
- valid evaluation = 0
- audit 无法读取
- JSONL 格式错误
- 所有市场已过期
- real-order invariant 缺失

### 结果分类

推荐：

```text
PASS
FAIL
INCOMPLETE
```

建议退出码：

```text
0 = PASS
1 = invariant failure
2 = incomplete / insufficient valid data
3 = infrastructure / configuration error
```

禁止把 INCOMPLETE 当 PASS。

---

## 18. 市场状态计数

必须使用明确口径：

```text
configured_markets
active_markets
expired_skipped
discovered_active
paired_ready
not_ready
waiting_up_snapshot
waiting_down_snapshot
```

主恒等式：

```text
paired_ready + not_ready = discovered_active
```

禁止混淆：

- token book 数
- market 数
- configured market 数
- active market 数
- paired READY 数

如果统计的是 book，必须命名：

```text
ready_books
```

如果统计的是完整 Up/Down pair，必须命名：

```text
paired_ready
```

---

## 19. 故障与恢复

### Gamma 故障

Gamma 超时或失败时：

- 保留有效缓存市场
- 标记 DEGRADED
- 记录错误
- 记录 cache age
- 禁止发布空成功结果
- 只要仍有未结束市场，不能无故终止健康 C++ 引擎

### CLOB 验证失败

如果部分候选失败：

- 保留有效市场
- 记录 rejected count
- 记录 rejection reason
- 禁止无效 token pair 进入 READY

### 缓存配置生命周期

```text
FRESH_CONFIG
→ Gamma failure
CACHED_CONFIG_ACTIVE
→ 所有缓存市场结束
NO_ACTIVE_MARKETS
→ evaluation disabled
→ fail closed
```

### C++ 过期处理

C++ 必须：

- 跳过已结束市场
- unsubscribe 过期 token
- 停止评估过期市场
- 全部过期时 fail closed
- 禁止因 Gamma 故障继续用过期订单簿

### 扫描失败隔离

失败或超时 scan 禁止：

- 删除当前有效订阅
- 发布空文件
- 覆盖更新后的 scan
- 后台迟到任务继续写入旧结果

---

## 20. systemd 与 VPS 运行

生产 Shadow 服务由 systemd 管理。

职责包括：

- 连续市场发现
- C++ 引擎常驻
- 动态 token subscribe
- 自动重连
- full resync
- market rotation
- audit
- Web monitoring

systemd 服务必须：

- 使用绝对路径
- 需要 Python 时使用项目虚拟环境
- 启动前创建日志目录和文件
- 配置错误时明确失败
- 使用有上限的 restart delay
- 避免无控制 restart loop
- 保留日志
- 暴露 health state

可选环境文件：

```ini
EnvironmentFile=-/opt/poly-arb-bot/.env
```

禁止提交 secrets。

---

## 21. 日志与轮转

以下日志不得无限增长：

- `logs/shadow-audit.jsonl`
- bot logs
- Web logs
- scanner logs
- error logs

必须配置轮转。

JSONL 轮转必须保持每行完整。

如果使用 `copytruncate`，必须确认生产者兼容。

更推荐应用支持 reopen。

磁盘满必须显示 DEGRADED 或 FAILED。

---

## 22. 时间与时钟

持续时间和 deadline 必须使用 monotonic clock。

wall-clock 只用于：

- 外部事件时间
- audit timestamp
- UI 展示

服务器必须保持时间同步。

Health 建议暴露：

- NTP synchronized
- clock skew
- WS message age
- reference message age
- book skew

clock skew 超阈值时，依赖时间的策略必须 fail closed。

---

## 23. 测试要求

所有改动必须保留或提高测试覆盖。

必须覆盖：

- Python unit tests
- scanner tests
- Gamma parsing
- CLOB validation
- global deadline
- cached config fallback
- atomic write
- stale scan publication prevention
- C++ tests
- WS initial array parsing
- WS book snapshot
- pre-snapshot price_change
- reconnect
- session invalidation
- generation isolation
- market rotation
- fee formula
- fee rounding
- VWAP
- FOK depth
- paired-lock math
- directional EV math
- lottery EV math
- event ID stability
- duplicate JSONL
- acceptance invariants
- empty-state failure
- JavaScript parse
- Web field mapping
- Binance raw message parsing
- Coinbase WebSocket parsing
- Kraken WebSocket parsing
- Bybit WebSocket parsing
- OKX WebSocket parsing
- symbol normalization
- USD/USDT market type isolation
- median consensus calculation
- outlier detection
- reference quorum
- reference degraded/blocked states
- NOT_RECEIVED vs STALE semantics
- cross-source divergence rejection
- Bash syntax
- systemd validation

禁止为了让 release 通过而删除测试。

### 完成前验证

声称完成前，必须实际运行相关检查并报告真实结果。

涉及对应模块时，至少运行：

```text
Python tests
C++ build/tests
JavaScript parse
Bash syntax
shadow-acceptance
official REST integration
official WebSocket integration
```

Mock 测试不能替代官方实网集成成功。

---

## 24. 发布门槛

以下情况禁止声称已完成：

- Gamma 集成仍挂起
- scan 超 deadline
- 零市场被当成功
- live_markets.json 被错误清空
- fee/buffer 被关闭
- unsupported reference 被当有效
- 真实下单被打开
- invariant failure 被隐藏
- N/A 被错误当成 0

发布前必须确认：

- tests pass
- scanner 在 deadline 内结束，或正确保留缓存
- C++ 编译通过
- JavaScript 解析通过
- Bash 语法通过
- Shadow 服务能启动
- market file 有效
- audit file 有真实有效事件
- shadow-acceptance = PASS
- real orders = 0
- real submissions = 0

---

## 25. 代码修改纪律

修改前必须先检查现有实现。

遵循仓库现有模式，除非现有模式本身导致当前问题。

禁止无关重构。

建议保持组件边界：

- discovery
- CLOB validation
- market config
- C++ book state
- reference state
- strategy evaluation
- risk checks
- audit
- Shadow execution
- Web aggregation
- acceptance checker

禁止把所有职责塞进一个超大文件。

### 可共享与不可共享

允许共享：

- order-book access
- VWAP
- fee calculation
- market metadata
- audit identity

必须分离：

- directional probability
- lottery probability
- paired-lock cost
- acceptance thresholds
- rejection reasons
- performance metrics

---

## 26. 配置规则

所有关键阈值必须显式配置。

例如：

- strategy enable flags
- target size
- minimum net EV
- minimum locked profit
- minimum EEV
- maximum entry price
- execution buffer
- maximum slippage
- minimum liquidity
- book freshness
- book sync skew
- reference freshness
- clock skew
- time windows by strategy/timeframe
- maximum exposure
- maximum loss
- scanner deadline
- CLOB concurrency
- Gamma retry count
- enabled reference sources
- source-specific symbols
- source-specific freshness thresholds
- min fresh exchange sources
- min fresh USD spot sources
- require settlement reference
- max cross-source divergence bps
- outlier threshold bps
- allowed market types by strategy

禁止隐藏 magic number。

Audit 应记录：

- config version
- strategy config hash

---

## 27. 安全规则

禁止提交：

- private keys
- wallet seeds
- API secrets
- signing keys
- passwords
- session cookies
- production tokens
- `.env` 内容

禁止把秘密打印到日志。

禁止实现地理限制绕过。

禁止通过代理或 VPN 规避平台控制。

未来实盘必须符合平台规则与用户合法资格。

---

## 28. 禁止修改项

Agent 禁止：

- 把项目重新定义成 paired-lock-only
- 因 paired_lock 更容易验证而删除方向策略
- 把 `model_probability > price` 当 paired-lock
- 只看 top-of-book 判断双边套利
- 目标规模需要 VWAP 时只看 best ask
- 把 REJECT 计为 order
- 把 evaluation 计为 completed trade
- 把 Shadow ACCEPT 当 real PnL
- 展示 fake curve
- 展示 fake latency
- 伪造 reference price
- 把 Binance 作为唯一不可替代参考源
- 直接平均 USD spot、USDT spot 和 perpetual
- 用 perpetual 冒充 settlement reference
- price 为空时显示 STALE
- quorum 不满足时继续方向策略 ACCEPT
- fee 缺失时默认 0
- WS 重连后保留旧 READY
- 完整快照前应用增量并 READY
- 接受 stale/unsynced book
- 发布半写入 JSON
- 扫描失败时覆盖有效配置为空
- 使用过期缓存市场
- 通过一个 flag 打开 live orders
- 为制造机会降低风控
- 未验证就声称完成

---

## 29. 当前项目状态

当前目标状态：

```text
Mode: SHADOW / DRY RUN
Real order submissions: 0
Real orders: 0
Real fills: 0
```

当前已实现或应保持：

- Gamma Series / Events discovery
- 7 资产
- 4 周期
- current + next
- bounded CLOB validation
- C++ REST + WebSocket
- 完整 WS snapshot readiness
- 动态 subscribe/unsubscribe
- market hot rotation
- 支持资产参考行情
- multi-depth VWAP
- dynamic fee
- execution buffer
- paired-lock evaluation
- execution pressure model
- stable event ID
- JSONL deduplication
- Web monitoring
- shadow acceptance invariants
- 45 秒扫描 deadline
- cached market fallback
- expired market fail closed

下一阶段的战略优先级：

1. 修复 Binance 参考行情接收与解析
2. 接入 Coinbase USD spot
3. 接入 Kraken USD spot
4. 实现多源 consensus、outlier 与 quorum
5. 恢复并验证 `late_window_directional_ev`
6. 恢复并验证 `low_price_lottery_ev`
7. 保持现有 `paired_lock` 模块不被破坏

---

## 30. 完成定义

只有同时满足以下条件，任务才算完成：

- 请求行为已实现
- 策略边界正确
- 故障时 fail closed
- audit 字段完整
- Web 与 backend 语义一致
- 相关自动化测试通过
- 相关语法与编译检查通过
- 必要时完成官方集成验证
- 真实订单保持 0
- 文档同步更新
- 已知限制明确说明

仅仅 unit tests 通过，不足以证明涉及 Gamma、CLOB、RTDS、WebSocket、systemd 或 VPS 的功能已经完成。
