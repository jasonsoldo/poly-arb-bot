# Maker 侧配对套利（maker_paired_accumulate）设计文档

日期：2026-07-20
状态：设计评审稿（未实现，未改任何代码）
模式约束：纯 Shadow / Dry Run；`real_order_submissions = 0`，`real_orders = 0`，`real_fills = 0`

---

## 0. 背景与动机

### 0.1 为什么 taker paired_lock 已无机会

`taker` 双腿 FOK `paired_lock` 要求同一时刻：

```text
up_vwap + down_vwap + up_fee + down_fee + execution_buffer < 1
```

2026 费用时代，Polymarket 对加密短周期市场 taker 收取与 `p(1-p)` 成正比的手续费
（本仓库 C++ 实现：`fee = round(size * rate * price * (1 - price) * 1e5) / 1e5`，
在 $0.5 附近费用最高），加上 buffer 后，两腿 ask VWAP 之和几乎永远 ≥ 1。

2026-07 实测（25 分钟连续观察）：

```text
net_cost < 1 出现次数 = 0
最近距离 net_cost = 1.0066
```

结论：taker FOK 双腿同时成交的成本链在当前费用结构下不成立。`paired_lock` 保留为
低频辅助策略与基准观测，不得为它降低 fee / buffer / freshness / depth / EEV 标准
（AGENTS.md §7）。

### 0.2 为什么 maker 侧可能成立

同一市场的双边盘口存在结构性不对称：

1. **maker 手续费 = 0**：maker 挂单不支付 taker 费，两腿成本直接减少
   `up_fee + down_fee`。
2. **taker 费返佣**：加密市场 taker 费的 20% 按比例返还给成交 maker
   （具体规则以官方 fee schedule 与 rebate program 为准）。
3. **流动性奖励**：部分市场有额外 liquidity rewards（按挂单质量分配）。
4. **near-miss 转化**：`up_best_bid + down_best_bid < 1 < up_best_ask + down_best_ask`
   的 near-miss 场景（即 `1.0066` 这类状态）在 taker 口径下不可行，但如果两腿都以
   bid 侧 maker 价成交，毛成本 `up_bid + down_bid < 1`，存在正的锁定空间。

代价：**腿风险**。两腿不再同时成交，第一腿成交后第二腿可能迟迟不成交，期间持仓
是单方向敞口（orphan leg），结算时可能亏损。本设计的核心就是把这个腿风险显式
建模、限额、熔断，而不是假装它不存在。

### 0.3 与现有 `maker_complete_set_arb` 观察者的关系

仓库已有 `maker_complete_set_arb`，是**只读** quote-geometry / trade-through
观察者（见 `docs/plans/2026-07-17-strategy-simplification-design.md`），其事件不是
订单、不是成交、不是 completed trade、不是 PnL。

本策略 `maker_paired_accumulate` 是全新的**第四套独立 Shadow 执行候选策略**，拥有
完整状态机与仓位生命周期。二者关系：

- `maker_complete_set_arb` 保持只读研究，不改职责；
- `maker_paired_accumulate` 的 Shadow 状态机可复用观察者积累的 trade-through
  统计做参数校准，但**不共享 ACCEPT 指标**。

---

## 1. 策略定位

### 1.1 第四套独立策略

策略名称：

```text
maker_paired_accumulate
```

定位：与 `late_window_directional_ev`、`low_price_lottery_ev`、`paired_lock`
同权独立的第四套策略。策略优先级更新为：

1. `late_window_directional_ev`（主策略）
2. `low_price_lottery_ev`（主策略）
3. `maker_paired_accumulate`（主策略候选，Shadow 验证期）
4. `paired_lock`（低频辅助策略）

禁止事项（沿用 AGENTS.md §2）：

- 禁止用一个通用 `EDGE` / `SCORE` / `ACCEPT` 指标混合四套策略；
- 禁止用 `model_probability > expected_fill_price` 作为本策略 ACCEPT 条件
  （方向判断只属于方向策略）；
- 禁止把本策略的挂单统计计入 `paired_lock` 的 ACCEPT 或 PnL；
- 外部交易所参考行情对本策略仅为 `REFERENCE ONLY`，用于挂单择时辅助观测，
  不构成 ACCEPT 的必要条件（与 `paired_lock` 一致，见 §4.7）。

### 1.2 独立资产清单

`maker_paired_accumulate` 独立拥有：

| 资产 | 位置（规划） |
|---|---|
| 输入字段 | `MakerAccumulateInput`（新增 dataclass，不复用 `DirectionalInput`） |
| 评估逻辑 | `evaluate_maker_accumulate()`（新增，与 `evaluate_directional/lottery` 平级） |
| 状态机 | `MakerAccumulateStateMachine`（新增，见 §4） |
| 风控规则 | `maker_accumulate_*` 前缀配置（见 §6），不与其他策略共享限额 |
| ACCEPT 条件 | 见 §2.4、§3.3 |
| REJECT 原因 | 独立枚举（见 §2.5、§3.4），不得复用方向策略原因码 |
| 审计事件 | `strategy: "maker_paired_accumulate"`，独立 event_type 集合（见 §9） |
| Shadow 统计 | 独立 completed trade / orphan / 状态转换统计（见 §7.4） |
| Web 展示 | 独立面板 `MAKER PAIRED ACCUMULATE`（见 §10） |

---

## 2. 第一腿规则（LEG1 挂单决策）

### 2.1 进入前提（每场评估的 fail-closed 闸门）

以下全部满足才允许进入挂单评估，否则 REJECT：

- Up/Down 属于同一 condition，token ID 有效；
- 当前 WS session 已收到 Up 与 Down 完整 book 快照（READY 规则同 §9）；
- 双边 book fresh（`book_age_ms <= maker_max_book_age_ms`）；
- 双边时间戳差异 `book_skew_ms <= maker_max_book_skew_ms`；
- fee schedule 可用（用于计算返佣估计与对冲腿 taker 费，缺失即
  `fee_schedule_unavailable`）；
- 市场 active、未结束、可交易；
- `seconds_to_close` 处于本策略时间窗口（见 §6.5）；
- 当前无同市场未完成 episode（同一 condition 同时只允许一个 episode）；
- 组合风控未触发（敞口 / 日亏损 / 熔断，见 §6）。

### 2.2 挂哪一边（side selection）

第一腿选择**更可能被 taker 打到、且价格更便宜**的一侧。评估输入（全部来自真实
WS 订单簿，缺失即 REJECT）：

- `up_best_bid / up_best_ask / up_midpoint`
- `down_best_bid / down_best_ask / down_midpoint`
- `up_book_imbalance / down_book_imbalance`（`(bid_depth - ask_depth) / (bid_depth + ask_depth)`）
- `up_bid_depth_at_improve_level / down_bid_depth_at_improve_level`
- 价格偏离 $0.5 程度：`|midpoint - 0.5|`

选择规则（按顺序打分，全部阈值配置化）：

1. **偏离过滤**：`|midpoint - 0.5|` 越大的腿，其 maker 价越便宜、锁定空间越大，
   但成交等待越久；`|midpoint - 0.5|` 超过 `maker_leg1_max_extremity` 的腿排除
   （过深价外，成交概率过低）。
2. **不平衡偏向**：`book_imbalance` 偏向 ask 侧更重（imbalance 更负）的腿，
   说明卖压更大、我们的 bid 更可能被打到，优先。
3. **返佣预期**：taker 费 ∝ `p(1-p)`，靠近 $0.5 的腿返佣更高；在其余条件接近时
   作为 tie-breaker。
4. 两腿综合得分差 < `maker_leg1_side_min_score_gap` 时，选 bid 更深（排队更稳）的腿。

选出的腿记为 `leg1_outcome ∈ {Up, Down}`，另一腿为 `leg2_outcome`。

### 2.3 挂什么价（price improvement 规则）

硬约束：**永不跨 spread**。maker 买单价格必须满足：

```text
leg1_quote_price <= leg1_best_ask - min_tick
```

即禁止以 ≥ 对手 best ask 的价格"挂单"（那是 taker 行为，属于 `paired_lock` 或方向
策略的领域）。

两档配置化模式：

- **join**：`leg1_quote_price = leg1_best_bid`（排进现有 bid 队列尾部）；
- **improve**：`leg1_quote_price = leg1_best_bid + min_tick`（改善一档，排到队首），
  仅当改善后仍满足 `leg1_quote_price <= leg1_best_ask - min_tick`，且改善增加的
  成本不超过 `maker_leg1_max_improve_cost_per_share`。

默认模式 `maker_leg1_quote_mode = improve`（Shadow 初始值），join 用于改善空间
不足时。若 `best_ask - best_bid <= min_tick`（spread 已贴死，无法改善且不跨
spread），则 join；若 join 后预估排队位置对应的成交概率低于
`maker_leg1_min_fill_probability`（配置型压力模型，标注
`configured_fill_model`，不得称历史概率），REJECT `leg1_queue_too_deep`。

### 2.4 第一腿 ACCEPT 条件（开仓判定）

开仓不等价于锁定利润。第一腿挂单的 ACCEPT 是**预期口径**，必须同时满足：

```text
leg1_quote_price + leg2_best_bid + buffer_per_share
    < 1 - min_expected_locked_margin          # 两腿都按 maker bid 成交仍有边际
leg1_quote_price + leg2_best_ask + hedge_taker_fee_per_share + buffer_per_share
    < 1 - min_hedge_exit_margin               # 第二腿被迫 taker 对冲时仍不亏损超过上限
orphan_leg_max_loss_estimate <= max_orphan_leg_loss_per_episode
```

其中：

- `buffer_per_share`：独立可配置执行缓冲，禁止藏进价格或费用；
- `hedge_taker_fee_per_share`：按 fee schedule 对第二腿 taker 对冲价计算的真实
  taker 费（`p(1-p)` 公式），用于最坏路径核算；
- **预期返佣不计入 ACCEPT 判定**（保守口径，见 §5.3）；返佣只在展示与事后统计中
  作为 `ESTIMATED REBATE` 出现；
- 两个边际阈值 `min_expected_locked_margin`、`min_hedge_exit_margin` 均显式配置。

### 2.5 第一腿 REJECT 原因（独立枚举）

```text
books_not_ready
waiting_up_snapshot
waiting_down_snapshot
up_book_stale
down_book_stale
books_not_synced
fee_schedule_unavailable
market_not_tradable
market_expired
outside_time_window
episode_already_active
leg1_no_improve_room            # spread 贴死且 join 队列过深
leg1_queue_too_deep
leg1_extremity_exceeded         # |mid-0.5| 超上限
expected_margin_below_threshold
hedge_exit_margin_below_threshold
orphan_loss_estimate_exceeded
book_depth_insufficient
portfolio_exposure_exceeded
daily_loss_limit_reached
orphan_circuit_breaker_open
clock_skew_exceeded
```

---

## 3. 第二腿动态规则（LEG2 追单逻辑）

### 3.1 第二腿最高可接受价（动态顶价）

第一腿按成交均价 `leg1_avg_price`、成交数量 `leg1_filled_size` 成交后，第二腿
（maker 买 `leg2_outcome`）的最高可接受价实时计算：

```text
leg2_max_price =
    (guaranteed_payout_per_share_pair
     - leg1_avg_price
     - buffer_per_share
     - min_realized_margin)                # 要求两腿完成后仍剩的最小实现边际
```

对同份额配对，`guaranteed_payout_per_share_pair = 1`，即：

```text
leg2_max_price = 1 - leg1_avg_price - buffer_per_share - min_realized_margin
```

不变量（每份额）：`leg1_avg_price + leg2_avg_price + buffer_per_share <= 1 - min_realized_margin`。
任何时候第二腿报价 `> leg2_max_price` 都是不允许的（宁可超时也不锁定亏损）。

若第二腿挂单期内第一腿有部分成交/价格更新，`leg2_max_price` 按最新 `leg1_avg_price`
重算（数量加权平均价）。

### 3.2 追价策略（improve loop）

第二腿从 maker 模式开始：`leg2_quote = leg2_best_bid + min_tick`（改善一档，不跨
spread）。之后按配置的节奏追价：

```text
maker_leg2_improve_interval_ms      # 每次改善间隔
maker_leg2_max_improves             # 最大改善次数，例如 5
maker_leg2_improve_step_ticks       # 每次改善 tick 数，例如 1
```

第 `k` 次改善：

```text
leg2_quote_k = min(
    leg2_best_bid + improve_step_ticks * min_tick,   # 跟随盘口改善
    leg2_best_ask - min_tick,                        # 永不跨 spread
    leg2_max_price,                                  # 永不锁亏
)
```

三个上限取最小值；若 `leg2_max_price` 是最紧的那个且已低于等于当前 best bid，
说明锁定边际已被行情吃掉，直接放弃 maker 路径，进入 §3.3 的放弃分支。

### 3.3 超时与放弃策略

第二腿总时限 `maker_leg2_timeout_seconds`（配置化，且必须 `<` 收盘强制平仓窗口，
见 §6.5）。超时或改善次数耗尽时的分支（按顺序评估）：

1. **转 taker 对冲腿（HEGDE）**：若当前
   `leg1_avg_price + leg2_best_ask + hedge_taker_fee + buffer < 1 - min_hedge_exit_margin`
   仍成立，则以 taker 价买第二腿完成配对（付出 taker 费，锁定小额利润或微亏）。
   该腿在 Shadow 中按真实 ask VWAP 模拟成交。
2. **方向性退出（HEDGING_DIRECTIONAL_EXIT）**：对冲已不划算时，按 §4 状态机把
   第一腿持仓当作方向敞口处理：挂 maker 卖单退出，卖价不低于
   `leg1_avg_price - max_orphan_giveback_per_share`；超时未成交则 taker 卖出，
   实际亏损计入 episode PnL 并触发连续 orphan 统计。
3. **EMERGENCY_FLATTEN**：进入收盘强制平仓窗口或风控触发时，无条件 taker 卖出
   第一腿（Shadow 按真实 bid VWAP 模拟），亏损计入，触发熔断计数。

每次 episode 只允许上述分支执行一次；禁止无限 improve 循环。

### 3.4 第二腿 REJECT / 终止原因（独立枚举）

```text
leg2_max_price_below_bid          # 锁定边际被行情吃掉
leg2_improves_exhausted
leg2_timeout
hedge_margin_below_threshold
directional_exit_timeout
emergency_flatten_window
market_expired_mid_episode
books_lost_mid_episode            # WS 断连/resync，episode 强制进入退出分支
fee_schedule_lost_mid_episode
```

---

## 4. Orphan-leg 状态机

### 4.1 状态定义

```text
IDLE
  │  leg1 ACCEPT（§2.4），挂出 maker 单
  ▼
LEG1_WORKING
  │  对手价穿越挂单价（Shadow 成交规则见 §7.2）
  ▼
LEG1_FILLED ──────────────┐
  │  立即挂第二腿          │ 第一腿部分成交：达到
  ▼                        │ maker_leg1_min_fill_ratio
LEG2_WORKING               │ 也进入 LEG2_WORKING，
  │  第二腿成交            │ 未成交部分同时撤单
  ▼                        │
COMPLETE                   │
                           │
失败路径：                  │
LEG1_WORKING  --撤单/超时--> LEG1_CANCELLED --> IDLE（无仓位，仅统计）
LEG1_FILLED/LEG2_WORKING --§3.3 分支2--> HEDGING_DIRECTIONAL_EXIT --> CLOSED_WITH_LOSS
任意持仓状态 --§3.3 分支3/风控--> EMERGENCY_FLATTEN --> CLOSED_WITH_LOSS
```

终态：`COMPLETE`、`LEG1_CANCELLED`、`CLOSED_WITH_LOSS`。
每个终态都产生一条 completed episode 记录；只有 `COMPLETE` 计入 locked-profit
样本，其余计入 orphan / 退出样本（§7.4）。

### 4.2 状态转换触发条件

| 转换 | 触发条件 |
|---|---|
| IDLE → LEG1_WORKING | §2.1 前提全过 + §2.4 ACCEPT |
| LEG1_WORKING → LEG1_FILLED | Shadow 成交规则判定成交（§7.2），数量 > 0 |
| LEG1_WORKING → LEG1_CANCELLED | `maker_leg1_timeout_seconds` 超时未成交；或盘口恶化使 §2.4 条件不再成立；或进入强制平仓窗口 |
| LEG1_FILLED → LEG2_WORKING | 立即（同一评估周期内） |
| LEG2_WORKING → COMPLETE | 第二腿按 Shadow 成交规则成交，且不变量 §3.1 成立 |
| LEG2_WORKING → HEDGING_DIRECTIONAL_EXIT | §3.3 分支 2 |
| 任意持仓态 → EMERGENCY_FLATTEN | §3.3 分支 3，或风控（§6）触发，或 WS session 失效（books_lost_mid_episode） |

### 4.3 每态风控上限

| 状态 | 上限（全部配置化） | 触发动作 |
|---|---|---|
| LEG1_WORKING | `maker_leg1_timeout_seconds` | 撤单 → LEG1_CANCELLED |
| LEG1_FILLED + LEG2_WORKING | `maker_max_orphan_seconds`（最大裸腿时长，按 timeframe 独立配置，且 `<` 强制平仓窗口） | 进入 §3.3 分支 |
| LEG1_FILLED + LEG2_WORKING | `maker_max_orphan_loss_usd`（最大裸腿亏损：按当前 bid 市价计算第一腿浮亏） | 立即 EMERGENCY_FLATTEN |
| HEDGING_DIRECTIONAL_EXIT | `maker_directional_exit_timeout_seconds` | taker 卖出收尾 |
| 任意 | `seconds_to_close <= maker_force_flatten_seconds` | EMERGENCY_FLATTEN |

裸腿浮亏口径（保守）：`leg1_filled_size * (leg1_avg_price - leg1_best_bid)`，
每评估周期用真实订单簿重算；不得用 midpoint 或 reference price 代替。

### 4.4 状态机与 WS session / generation 绑定

episode 状态绑定 `market_id + condition_id + generation + ws_session_id`。
发生重连、full resync、current→next 切换时：

- 旧 session 的挂单状态全部作废；
- 持仓中的 episode 无条件进入 EMERGENCY_FLATTEN 评估（Shadow 下用恢复后的第一本
  fresh book 模拟退出）；
- 迟到消息（旧 generation / 旧 session）丢弃，不得驱动状态转换。

---

## 5. 成本链

### 5.1 名义成本链（每份额，COMPLETE 路径）

```text
gross_cost          = leg1_avg_price + leg2_avg_price
maker_fees          = 0                        # maker 腿手续费恒 0
hedge_taker_fee     = 0（纯 maker 完成）/ 实际值（§3.3 分支1 对冲腿）
gas_cost_per_share  = 配置的 gas 摊销           # 缺失时不得默认 0，按配置值
buffer_per_share    = 独立执行缓冲
net_cost            = gross_cost + hedge_taker_fee + gas_cost_per_share + buffer_per_share
guaranteed_payout   = 1（同份额配对）
locked_profit       = guaranteed_payout - net_cost
locked_roi          = locked_profit / net_cost
```

### 5.2 返佣与流动性奖励（ESTIMATED，不入账）

```text
estimated_rebate_per_share =
    rebate_share_ratio * taker_fee_formula(leg_fill_price)   # 每条 maker 成交腿
estimated_liquidity_reward   = 按官方 rewards 规则的配置化估计
```

铁律：

- 返佣与奖励**一律标注 `ESTIMATED REBATE` / `ESTIMATED REWARD`**；
- 未实际收到（链上/账户确认）前**不得计入已实现利润**，不得计入 completed PnL，
  不得用于 ACCEPT 判定（§2.4 明确保守口径）；
- 审计与 Web 中返佣单独成列：`estimated_rebate`（预期）与
  `realized_rebate`（确认后，Shadow 阶段恒 0 或 N/A）分列展示；
- Shadow 阶段统计"含返佣预期利润"与"不含返佣保守利润"两条线，评估策略时以
  **保守线**为准。

### 5.3 locked 与 at-risk 的区分

任意时刻 episode 的敞口必须拆成两部分展示与入账：

```text
LOCKED 部分  = min(leg1_filled_size, leg2_filled_size)  # 已配对份额，利润已锁定
AT-RISK 部分 = |leg1_filled_size - leg2_filled_size|     # 裸腿份额，承担方向风险
```

- LOCKED 部分 PnL 按 §5.1 锁定口径计算；
- AT-RISK 部分按 §4.3 浮亏口径逐周期重估，计入 orphan 风险统计；
- Web 与审计中禁止把 AT-RISK 浮盈当作 locked profit。

---

## 6. 风控

全部阈值显式配置，环境变量前缀 `MAKER_ACCUMULATE_*`，并入
`PortfolioLimits` 同级的独立配置块（不与其他策略共享额度）：

| 配置 | 含义 |
|---|---|
| `MAKER_ACCUMULATE_MAX_NOTIONAL_PER_MARKET` | 单市场最大名义敞口 |
| `MAKER_ACCUMULATE_MAX_TOTAL_EXPOSURE` | 全市场总敞口（含 LOCKED + AT-RISK） |
| `MAKER_ACCUMULATE_MAX_AT_RISK_EXPOSURE` | 全市场裸腿敞口上限（更紧） |
| `MAKER_ACCUMULATE_MAX_DAILY_LOSS` | 每日亏损上限，触发后当日不再开新 episode |
| `MAKER_ACCUMULATE_MAX_CONSECUTIVE_ORPHANS` | 连续 orphan（CLOSED_WITH_LOSS）熔断次数，触发后 `orphan_circuit_breaker_open`，冷却 `MAKER_ACCUMULATE_CIRCUIT_COOLDOWN_SECONDS` |
| `MAKER_ACCUMULATE_MAX_EPISODES_PER_MARKET_WINDOW` | 单市场单窗口最大 episode 数 |
| `MAKER_ACCUMULATE_WINDOW_MIN/MAX_SECONDS` | 工作窗口：仅在 paired_lock 同级宽窗口内工作（初始 20–7200 秒，按 timeframe 配置） |
| `MAKER_ACCUMULATE_FORCE_FLATTEN_SECONDS` | 收盘强制平仓窗口：`seconds_to_close <=` 该值时禁止开新仓、持仓强制 EMERGENCY_FLATTEN；必须大于 `maker_max_orphan_seconds + maker_leg2_timeout_seconds` |
| `MAKER_ACCUMULATE_MAX_ORDER_SIZE` | 单腿最大份额 |
| `MAKER_ACCUMULATE_MIN_BOOK_DEPTH` | 挂单价档位最小深度 |

附加规则：

- **时间窗口**：仅在本策略窗口内开新 episode；窗口外已有 episode 只走退出路径；
- **收盘前**：强制平仓窗口内任何状态都向 EMERGENCY_FLATTEN 收敛，禁止把裸腿带进
  结算；
- **熔断**：连续 orphan 达到上限 → 熔断 → 冷却期内只平仓不开仓；
- **日亏损**：达到上限后当日 `decision = REJECT, reason = daily_loss_limit_reached`；
- 所有限额消耗按 episode 入账时刻计算，禁止事后调整。

---

## 7. Shadow 先行

### 7.1 原则

本策略必须先有完整 Shadow 状态机模拟，任何实盘讨论之前：

```text
real_order_submissions = 0
real_orders = 0
real_fills = 0
```

### 7.2 挂单成交模拟规则（关键）

用真实 CLOB WS 订单簿驱动，禁止用 reference price 或随机模型代替：

- 我方 maker 买单价格为 `p` 时，**仅当该腿 best ask 穿越 `p`（`best_ask <= p`）**
  才判定成交；
- 保守口径（默认 `maker_shadow_fill_mode = strict`）：仅当 `best_ask < p`（严格穿越）
  判定成交；`best_ask == p`（touch）不成交，除非启用队列位置模型；
- 队列位置模型（可选，`maker_shadow_fill_mode = queue`）：按挂单价档位深度估计
  排队位置，仅在估计队列耗尽时成交；该模型输出必须标注
  `configured_queue_model`，不得称历史成交概率；
- 成交数量上限：`min(挂单剩余, 穿越价位真实盘口深度)`，支持部分成交；
- 盘口数据 stale / session 失效期间，禁止判定成交，episode 按 §4.4 处理；
- taker 对冲/退出腿按真实对侧 VWAP 模拟（复用现有 VWAP/FOK 深度代码路径）。

### 7.3 模拟保真度要求

- 挂单报价、改善、撤单全部产生审计事件（§9），可完整 replay；
- clock skew 超阈值时状态机 fail closed；
- book 快照不完整（waiting snapshot）期间禁止开仓；
- 模拟中的每一决策只能使用当时真实可用的 book 状态（禁止未来函数）。

### 7.4 统计样本要求

以下统计按 strategy = `maker_paired_accumulate` 独立分组：

```text
episode_count（按终态分组：COMPLETE / LEG1_CANCELLED / CLOSED_WITH_LOSS）
leg1_fill_rate               = LEG1_FILLED / LEG1_WORKING episodes
leg2_completion_rate         = COMPLETE / LEG1_FILLED episodes
average_pair_cost            = COMPLETE episodes 的 gross_cost 均值
average_locked_profit        = COMPLETE episodes 保守口径（不含返佣）
average_estimated_rebate     = ESTIMATED 口径单独列
orphan_rate                  = CLOSED_WITH_LOSS / LEG1_FILLED episodes
average_orphan_loss
max_orphan_loss
orphan_duration_distribution
realized_shadow_pnl          = COMPLETE 锁定利润 + CLOSED_WITH_LOSS 实际亏损之和
```

评估门槛（配置化）：

- `MAKER_ACCUMULATE_MIN_COMPLETED_EPISODES`：≥ N 个终结 episode（初始建议 N=100，
  其中 COMPLETE 与 CLOSED_WITH_LOSS 都不得为 0）才能进行策略评估结论；
- 样本不足时：所有比率与均值显示 `N/A`，禁止用少量样本下结论（同 §6 彩票策略
  语义）；
- 关键判定：`realized_shadow_pnl > 0` 且 `average_orphan_loss` 可被
  `average_locked_profit` 覆盖（即 orphan 成本被锁定利润吸收），才允许进入评审。

---

## 8. 与 AGENTS.md §13 实盘门槛的衔接

本策略在 §13 全部实盘门槛完成并获用户明确授权前：

```text
real_order_submissions ≡ 0
real_orders ≡ 0
real_fills ≡ 0
```

未来若要实盘，除 §13 通用门槛外，本策略还须具备（设计预留，当前不实现）：

- 两条腿独立的真实订单状态机（本设计 §4 的 Shadow 状态机即其骨架）：
  `IDLE → PRECHECK → LEG1_SUBMITTED → LEG1_FILLED → LEG2_SUBMITTED → COMPLETE`，
  失败态 `LEG1_REJECTED / LEG2_REJECTED / PARTIAL_FILL / ORPHANED / HEDGING /
  EMERGENCY_EXIT / HALTED`；
- deterministic client order ID（含 episode ID + leg 序号），幂等下单；
- post-only 订单标志保证 maker 身份（防止意外 taker 成交）；
- 返佣到账对账（`estimated_rebate` vs `realized_rebate` 月度 reconciliation）；
- orphan-leg 真实对冲通道与 emergency kill switch；
- 小额灰度方案：单市场、单 timeframe、`MAKER_ACCUMULATE_MAX_NOTIONAL_PER_MARKET`
  置最小值起步。

---

## 9. 审计事件字段清单

沿用 canonical JSONL（`logs/shadow-audit.jsonl`），每行完整 JSON object。
event identity 规则不变：`event_id` 由生产者生成，下游（Shadow execution、
Web consumer、acceptance checker）沿用，禁止重新生成；重复事件只计入
`duplicate_events`。

### 9.1 事件类型（strategy 恒为 `maker_paired_accumulate`）

| event_type | 含义 |
|---|---|
| `maker_episode_opened` | IDLE → LEG1_WORKING（含第一腿报价决策全字段） |
| `maker_quote_updated` | 挂单改善/跟随（leg1 或 leg2，含旧价新价与原因） |
| `maker_leg_filled` | Shadow 成交判定（含成交规则口径 strict/queue） |
| `maker_leg1_cancelled` | 第一腿超时/主动撤单 |
| `maker_episode_state_change` | 任意状态转换（含 from/to/reason） |
| `maker_episode_completed` | COMPLETE 终态（完整成本链） |
| `maker_episode_closed_with_loss` | 亏损终态（完整退出成本链） |
| `maker_episode_rejected` | 评估未开仓（REJECT + blocking_reasons） |

### 9.2 身份字段（每个事件必须包含）

`event_id, event_type, strategy, episode_id, market_id, condition_id, asset,
timeframe, window, generation, session, evaluation_sequence, timestamp`

`episode_id = hash(market_id + condition_id + generation + session + episode_sequence)`，
同一 episode 的所有事件共享。

### 9.3 通用字段

`decision, reason, blocking_reasons, state_from, state_to, books_ready, books_fresh,
books_synced, seconds_to_close, clock_skew_ms, config_version, config_hash,
real_order_submissions(=0), real_orders(=0), real_fills(=0)`

### 9.4 第一腿字段

`leg1_outcome, leg1_quote_mode(join/improve), leg1_quote_price, leg1_best_bid,
leg1_best_ask, leg1_midpoint, leg1_book_imbalance, leg1_queue_depth_ahead,
leg1_estimated_fill_probability(configured_fill_model), side_selection_score_gap,
expected_pair_cost, expected_margin, hedge_exit_margin, orphan_loss_estimate,
up_age_ms, down_age_ms, book_skew_ms`

### 9.5 第二腿字段

`leg1_avg_price, leg1_filled_size, leg2_outcome, leg2_quote_price, leg2_max_price,
leg2_best_bid, leg2_best_ask, improve_attempt, max_improves, leg2_elapsed_ms,
min_realized_margin`

### 9.6 成本链字段（completed/closed 事件）

`gross_cost, maker_fees(=0), hedge_taker_fee, hedge_taker_fee_raw,
hedge_taker_fee_rounded, fee_rate, fee_formula_version, gas_cost_per_share,
buffer_per_share, net_cost, guaranteed_payout, locked_profit, locked_roi,
locked_size, at_risk_size, estimated_rebate(标注 ESTIMATED REBATE),
estimated_liquidity_reward(标注 ESTIMATED REWARD), realized_rebate(Shadow 恒 0),
exit_path(maker_complete/taker_hedge/directional_exit/emergency_flatten),
exit_vwap, orphan_seconds, orphan_max_drawdown, episode_realized_pnl`

### 9.7 参考行情字段

`fast_price, consensus_price, settlement_reference, reference_state` —— 全部标注
`REFERENCE ONLY, NOT USED FOR MAKER-ACCUMULATE ACCEPTANCE`。

---

## 10. Web 展示面板设计

新增独立面板 `MAKER PAIRED ACCUMULATE`，与 DIRECTIONAL EV / LOW-PRICE LOTTERY /
PAIRED LOCK 并列（遵守 §16：只展示 canonical audit 与 health state 真实数据）。

### 10.1 全局计数（绑定真实字段）

```text
EPISODES OPENED
EPISODES COMPLETED
EPISODES CANCELLED (LEG1)
EPISODES CLOSED WITH LOSS
LEG1 FILL RATE（样本不足显示 N/A）
LEG2 COMPLETION RATE（同上）
ORPHAN RATE（同上）
ACTIVE EPISODES（按状态分组：LEG1_WORKING / LEG2_WORKING / EXITING）
AT-RISK EXPOSURE（实时，USD）
LOCKED EXPOSURE（实时，USD）
REALIZED SHADOW PNL（保守口径，不含 ESTIMATED REBATE）
ESTIMATED REBATE（单列，标注 ESTIMATED，不计入 PNL）
CIRCUIT BREAKER 状态
DAILY LOSS REMAINING
REAL ORDERS = 0 / REAL SUBMISSIONS = 0
```

### 10.2 单 episode 明细（当前活跃 + 最近终结）

```text
EPISODE ID / STATE / SECONDS TO CLOSE
LEG1 OUTCOME / QUOTE / AVG FILL / SIZE
LEG2 OUTCOME / QUOTE / MAX PRICE / IMPROVE k/N
GROSS COST / NET COST / LOCKED PROFIT / LOCKED ROI（COMPLETE 才显示，
    否则显示 N/A，禁止把预期当锁定）
AT-RISK SIZE / ORPHAN 浮亏 / ORPHAN SECONDS
EXIT PATH（终态事件）
DECISION / REASON / BLOCKING REASONS
```

### 10.3 禁止项（沿用 §16）

- 禁止把 `estimated_rebate` 并入利润曲线；
- 禁止把 AT-RISK 浮盈显示为锁定利润；
- 禁止把 episode opened 当 completed trade；completed sample 为 0 时
  WIN RATE / SHARPE / PNL 全部 `N/A`；
- 参考行情区照常逐源展示，并标注 `REFERENCE ONLY`；
- 没有真实端到端延迟测量时只显示 `MESSAGE AGE`，不显示 `LATENCY`。

---

## 11. 分阶段实施计划

### Phase 0：参数研究（只读，不改策略）

- 用现有 `maker_complete_set_arb` 观察者与 paired_lock near-miss 审计数据，统计
  `up_best_bid + down_best_bid` 分布、trade-through 频率、bid 侧深度分布；
- 产出：`maker_leg1_*`、`maker_leg2_*`、`min_expected_locked_margin` 等初始配置值
  的依据报告。

### Phase 1：Shadow 状态机

- 实现 `MakerAccumulateInput` / `evaluate_maker_accumulate()` /
  `MakerAccumulateStateMachine`（§2–§4）；
- 实现真实 WS book 驱动的成交模拟（§7.2，默认 strict 口径）；
- 实现 §9 全部审计事件；
- 测试：状态机转换、成交模拟（touch 不成交/strict 穿越成交/部分成交）、
  orphan 超时、强制平仓窗口、熔断、session 失效、event ID 稳定、重复 JSONL 去重、
  acceptance invariants、空状态 FAIL。

### Phase 2：统计验证

- VPS 连续运行，累计 ≥ `MAKER_ACCUMULATE_MIN_COMPLETED_EPISODES` 终结 episode；
- 校验 §7.4 关键判定（保守口径 realized_shadow_pnl > 0 且 orphan 成本可被覆盖）；
- 校验 `shadow-acceptance` PASS（含新策略计数恒等式扩展：
  `maker_episodes_opened = completed + cancelled + closed_with_loss + active`）；
- 校准 queue 模型与 configured_fill_model（校准前保持 configured 标注）。

### Phase 3：评审

- 设计评审 + 数据评审：保守利润线、orphan 分布、极端行情 episode 复盘；
- 评审通过前禁止任何实盘代码路径。

### Phase 4：实盘预研（仅当用户明确授权）

- 按 §8 清单实现真实订单状态机、post-only、对账、kill switch、灰度方案；
- 未授权前 `real_order_submissions / real_orders / real_fills` 恒 0，
  `shadow-acceptance` 持续校验该不变量。

---

## 12. 已验证的仓库事实（本设计依据）

本设计写作前实际检查了以下实现，文档中的字段名与机制与之对齐：

- `poly_arb_bot/ev_strategies.py`：`DirectionalInput` / `EvDecision` /
  `evaluate_directional` / `evaluate_lottery` / `decision_audit` 的独立策略模式与
  审计字段结构；
- `poly_arb_bot/strategy_shadow_lifecycle.py`：`PortfolioLimits.from_env()`
  环境变量配置模式、`StrategyShadowLifecycle` 的 positions/completed/orphan 状态
  管理、`maker_quotes` 观察者状态、config_version/config_hash 机制；
- `cpp/market_ws_engine/market_ws_engine.cpp`：`paired_lock` shadow_eval 事件
  完整成本链字段（up_vwap/down_vwap/up_fee/down_fee/net_cost/locked_profit/
  locked_roi/leg fill probabilities/expected_execution_value）、taker 费公式
  `round(size * rate * p * (1-p) * 1e5) / 1e5`、event identity 字段
  （event_id/run_id/evaluation_sequence/generation/session）、
  `real_order_submissions:0` 硬编码不变量；
- `poly_arb_bot/web_monitor.py`：策略计数分组、`maker_complete_set_arb` 只读
  观察者展示模式；
- `docs/plans/2026-07-17-strategy-simplification-design.md`：三策略独立与
  maker 观察者只读定位的既定决策。
