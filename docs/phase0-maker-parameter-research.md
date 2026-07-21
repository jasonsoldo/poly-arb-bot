# Phase 0 参数研究：maker_paired_accumulate 阈值实测依据

日期：2026-07-20
状态：研究完成（只读行情，未下任何真实订单；`real_order_submissions = 0`）
对应设计：`docs/plans/2026-07-20-maker-paired-arb-design.md` §11 Phase 0

---

## 1. 方法与数据来源

| 编号 | 数据源 | 内容 | 样本窗（UTC） | 基数 |
|---|---|---|---|---|
| A | `logs/shadow-audit.jsonl` | paired_lock taker 评估（best ask、VWAP、fee、深度） | 2026-07-20 09:19–09:45（25.5 min） | 14,348 评估 |
| B | `logs/strategy-audit.jsonl` | maker_complete_set_arb 观察者 quote 与 trade-through | 同上（25.5 min） | 13,781 quote 评估；827 单腿 + 77 双腿 trade-through |
| C | `data/phase0-book-snapshots.jsonl` | 实地 REST `/books` 批量采集，每 ~5s 一轮，56→53 市场 | 2026-07-20 22:12–22:25（13.1 min） | 5,266 市场快照（100 轮） |
| D | `data/phase0-trade-flow.jsonl` | 实地 WS `last_trade_price` + book 快照 | 2026-07-20 22:16–22:29（13.2 min） | 3,181 笔成交（384 笔 SELL 侧）、7,358 条盘口更新 |

采集脚本（可复用）：`scripts/collect_orderbook_snapshots.py`、`scripts/collect_trade_flow_ws.py`。
分析脚本：`scripts/phase0_analysis.py`、`scripts/phase0_analysis2.py`。
统计输出：`data/phase0-stats.json`、`data/phase0-stats2.json`。

**标注约定**：【实测】= 直接来自 C/D 的当轮采集；【历史】= 来自 A/B 审计日志；
【推断】= 由实测/历史数据经假设推导；【配置型估计】= 非历史成交率，仅作参数先验。

**互补关系验证**（支撑历史推断）：二元市场中 `up_best_bid + down_best_ask − 1` 的实测分布
（C，n=5,198）：mean = 0.000015，p95 = 0，max = 0.08。互补关系在绝大多数快照中精确成立，
因此历史数据中的 `up_best_bid ≈ 1 − down_best_ask` 推断有效（局限见 §7）。

**观察者报价口径警示**：B 中的 `up_bid_quote/down_bid_quote` 不是盘口 best bid，而是
`floor_tick(fair − quote_half_spread(0.02))` 的模型报价（`cpp/strategy/complete_set_arb.hpp`），
仅在概率模型可用时产生。B 的 trade-through 统计是相对该模型报价的穿越，不能直接当作
"join best bid" 的成交率。

---

## 2. 研究问题 1：maker 可成交顶价分布（up_best_bid + down_best_bid）

### 2.1 【实测】REST 快照分布（C，n=5,198，13.1 min）

| 统计量 | mean | p5 | p25 | p50 | p75 | p95 | min | max |
|---|---|---|---|---|---|---|---|---|
| up_bid + down_bid | 0.9323 | 0.76 | 0.90 | 0.97 | 0.99 | 0.99 | 0.46 | 0.999 |
| 对 $1 的边际（1 − sum） | 0.0677 | 0.01 | 0.01 | 0.03 | 0.10 | 0.24 | 0.001 | 0.54 |

### 2.2 【历史推断】由 taker 审计推导（A，n=14,348，25.5 min）

`derived_bid_sum = 2 − (up_best_ask + down_best_ask)`：mean 0.9595，p50 0.97，p95 0.99；
`derived_bid_sum < 1` 比例 **97.6%**（对照：`ask_sum < 1` 仅 2.4% —— taker 几何几乎从不成立）。

### 2.3 分资产/周期【实测】（bid_sum 均值 / p50）

| 组 | 均值 | p50 | 组 | 均值 | p50 |
|---|---|---|---|---|---|
| BTC/5m | 0.990 | 0.99 | XRP/5m | 0.973 | 0.98 |
| BTC/15m | 0.990 | 0.99 | XRP/4h | 0.958 | 0.965 |
| BTC/1h | 0.985 | 0.983 | BNB/5m | 0.871 | 0.89 |
| BTC/4h | 0.981 | 0.98 | BNB/1h | 0.914 | 0.895 |
| ETH/4h | ~0.98* | 0.98* | DOGE/HYPE 各组 | 0.88–0.96 | — |

\* ETH/SOL 主干组与 BTC 类似（详见 `data/phase0-stats.json` C.by_asset_timeframe）。
主流资产（BTC/ETH/SOL/XRP）盘口紧、bid_sum 贴近 0.98–0.99；BNB/DOGE/HYPE spread 宽、
bid_sum 低至 0.87–0.95，边际大但成交确定性低（见 §4 流量数据）。

---

## 3. 研究问题 2：maker 双腿成交假设下的净成本与可锁定比例

每份额对：`net_cost_maker = up_bid + down_bid + buffer`（maker fee = 0，gas 摊销另计）。
锁定条件：`bid_sum + buffer < 1`。

### 3.1 【实测】可锁定比例（C，n=5,198）

| buffer/share | 全局可锁定比例 |
|---|---|
| 0.002 | 99.79% |
| 0.005 | 99.60% |
| 0.010 | 69.39% |

### 3.2 分组可锁定比例（buffer = 0.01 的保守档）

| 组 | 比例 | 组 | 比例 |
|---|---|---|---|
| BTC/15m | **0.5%** | XRP/5m | 58.9% |
| BTC/5m | 1.3% | BTC/4h | 78.0% |
| BTC/1h | 51.0% | BNB/1h、BNB/4h、XRP/4h、DOGE 各组 | ~100% |

解读：buffer=0.01 时 BTC 短周期几乎全军覆没（边际中位数恰为 0.01）；
buffer=0.005 时全局 99.6% 几何可行。**几何可行 ≠ 能成交**——必须乘以 §4 的
trade-through 概率。边际中位数（1−bid_sum）：BTC/15m = 0.01，BTC/1h = 0.017，
BTC/4h = 0.02，XRP/4h = 0.035，BNB/4h = 0.12。

### 3.3 【历史】观察者模型报价口径（B，仅供参考）

`pair_quote_cost`（模型报价对成本）mean 0.42，p75 0.95；`locked_edge_if_both_fill > 0`
比例 44.3%（edge 上限被 `maker_minimum_pair_edge` 截断于 0.05）。该口径与设计文档的
join/improve best bid 不同，仅证明"fair − 2×0.02 双边挂单"几何上近半可行。

---

## 4. 研究问题 3：trade-through 频率（maker 成交概率代理）

### 4.1 【实测】WS 成交流（D，13.2 min，3,181 笔）

全部市场合计 241.6 trades/min；其中 **SELL 侧（打 bid，即 maker 买单的成交来源）384 笔**：

| 组 | SELL/min | 组 | SELL/min |
|---|---|---|---|
| BTC/5m | 17.47 | ETH/15m | 0.76 |
| BTC/15m | 4.02 | DOGE/15m | 0.68 |
| ETH/5m | 2.73 | XRP/5m、SOL/5m | 0.38 |
| BNB/5m | 1.37 | BTC/1h、BNB/15m | 0.30 |
| DOGE/5m、HYPE/5m | 0.23 | BTC/4h、XRP/4h、SOL/4h、XRP/15m | 0.08 |

### 4.2 【实测】SELL 成交价 vs 同时刻 best bid（n=384）

| 口径 | 比例 |
|---|---|
| price < best_bid（严格穿越，strict 成交） | 0.78% |
| price == best_bid（touch，队列消耗） | 89.84% |
| price > best_bid（参考 bid 已过期/移动） | ~9.4% |

解读【配置型估计】：挂在 best bid 的 maker 单，其价格档位被 SELL 流触及的频率高
（BTC/5m 约 17 次/min），但 **touch ≠ 成交**——成交取决于队列位置（设计文档
`maker_shadow_fill_mode = strict/queue` 的区分在此得到实测支持：严格穿越仅 0.78%）。
`price > best_bid` 占 9.4% 反映 best bid 在成交间隙被撤/移动（与 §5 的 drift 一致）。

### 4.3 【历史】观察者模型报价的 trade-through（B，25.5 min）

单腿 trade-through 827 次，双腿 77 次（**双/单 ≈ 9.3%**）。分组单腿次数：
BTC/5m 162、ETH/5m 120、BTC/15m 83、SOL/5m 67、DOGE/15m 66。
trade-through 发生时 quote_age：p25 150ms、p50 767ms、p75 2.3s、p95 8.1s。
穿越时 trade size：p50 2.6、p75 8.2、p95 50 shares。
口径：相对 `fair−0.02` 模型报价，非 best bid；两腿穿越不要求同时，故 9.3% 是
"观察窗内两腿各被穿越过"的上限代理【配置型估计】。

### 4.4 【实测】leg1→leg2 成交间隔代理（orphan 时长先验）

同一市场先出现一侧 SELL、再出现另一侧 SELL 的间隔（n=299，13.2 min）：

| 统计量 | p25 | p50 | p75 | p95 | max |
|---|---|---|---|---|---|
| leg1→leg2 SELL 间隔（秒） | 1.27 | 5.37 | 16.4 | 88.3 | 608 |

分组 p50/p95（秒）：BTC/5m 2.5/20.6；ETH/5m 5.3/52.7；BTC/15m 28.8/147；
BNB/5m 11.3/111；ETH/15m 83/400；DOGE/15m 94/306。
**21%（81/380）的"第一腿 SELL"在 13 分钟窗内未等到对侧 SELL**（主要低流量市场）。
口径【配置型估计】：基于 taker SELL 流，不等于 maker 实际双腿成交间隔，但它是
`max_orphan_seconds` 唯一可实测的物理下界。

---

## 5. 研究问题 4：盘口不平衡、价差与深度

### 5.1 【实测】spread 分布（C，每条腿，n=10,396）

| 统计量 | mean | p25 | p50 | p75 | p95 |
|---|---|---|---|---|---|
| spread（ask−bid） | 0.0677 | 0.01 | 0.03 | 0.10 | 0.24 |

- **spread > 1 tick（0.01）的比例 = 69.4%** → 约七成快照有 improve 空间（`best_bid + 1tick < best_ask`）。
- BTC/ETH/SOL/XRP 主流组 spread p50 = 0.01–0.02（BTC/15m p50=0.01，贴死）→ 这些市场以 **join** 为主；
  BNB/DOGE/HYPE spread p50 = 0.03–0.12 → **improve** 为主。
- 结论：join/improve 必须两档都实现且按市场自适应，单一模式会错过一半以上市场。

### 5.2 【实测】深度分布（C）

| 指标 | p5 | p25 | p50 | p75 | p95 |
|---|---|---|---|---|---|
| bid size at best（每腿） | 5.0 | 10.0 | 28.5 | 100 | 370 |
| bid depth top3 | 26.5 | 60.8 | 150 | 334 | 1,455 |
| bid depth total | 338 | 9,563 | 30,782 | 70,766 | 156,794 |

p5 = 5.0 恰为 `min_order_size`（5 shares）——浅市场 best bid 档位经常只有最小挂单量。
单腿挂单 25–50 shares 在多数市场不超过 best bid 档位深度的中位数。

### 5.3 【实测】best bid 漂移（E，相邻两轮 ~5s，n=10,243 对）

| 指标 | 值 |
|---|---|
| 5s 内 best bid 移动 ≥1 tick 的比例（全局） | 24.4% |
| 5m 市场 | 42–58%（BTC/5m 57.8%） |
| 1h/4h 市场 | 6–23% |
| 移动时的幅度 | p50 = 2 ticks，p75 = 4 ticks，p95 = 11 ticks，max 27 ticks |

### 5.4 【历史】订单簿不平衡（A）

`up_book_imbalance` p25 −0.062、p50 ≈ 0、p75 0.003、p95 0.38——绝大多数时刻接近平衡，
imbalance 作为 side selection 信号区分度有限，仅在尾部（|imb|>0.3）有信息量。

---

## 6. 研究问题 5：阈值建议（设计文档 §2/§3/§6 参数初始值）

全部为 Shadow 初始值；标注【实测】依据的统计量或【推断】。

| 参数 | 建议初始值 | 依据 |
|---|---|---|
| `buffer_per_share` | **0.005** | 【实测】buffer=0.005 时 99.6% 快照几何可锁定；0.01 时 BTC 短周期只剩 0.5–1.3%。高于 taker 的 0.002 以覆盖双腿非同时成交风险 |
| `min_expected_locked_margin` | **0.005 /share** | 【实测】边际（1−bid_sum）p25=0.01、p50=0.03；阈值 0.005 保留主流市场大部分机会（BTC/15m 边际中位数恰 0.01，阈值 0.01 会拒掉其一半快照），同时与 buffer 同量级，强迫"边际至少再赚一份 buffer" |
| `min_hedge_exit_margin` | **−0.01 /share**（允许最多 1 分锁亏退出） | 【推断】spread p50=0.03，跨 spread taker 对冲成本中位约 1–3 分/股；设 0 会在 spread 走宽时永远对冲不出去，设 −0.01 限定最坏路径亏损上限。需 Shadow 校准 |
| `maker_leg1_quote_mode` | **improve 优先，spread ≤ 1 tick 自动降级 join** | 【实测】69.4% 快照 spread > 1 tick（improve 有空间）；BTC 主流组 spread 常贴死（join 唯一选择） |
| `max_spread_to_join`（join 判定上限） | **0.01**（spread 贴死才 join） | 同上；join 成交靠 touch 频率（BTC/5m 17 sells/min），improve 抢队首 |
| `maker_leg1_timeout_seconds` | 5m：**60s**；15m：**120s**；1h/4h：**300s** | 【实测】SELL/min：BTC/5m 17.5、BTC/15m 4.0、ETH/5m 2.7；低流量组 <0.4/min。60s 内 BTC/5m 预期 ~17 次 touch，DOGE/4h 预期 <1 次——统一超时无意义，按周期分档 |
| `maker_leg2_improve_interval_ms` | 5m：**1500ms**；15m：**2000ms**；1h/4h：**4000ms** | 【实测】5m 市场 42–58% 的 5s 间隔内 bid 移动 ≥1 tick，5s 间隔会错过约一半移动；1h/4h 仅 6–23%，4s 足够 |
| `maker_leg2_max_improves` | **5** | 【实测】移动幅度 p50=2、p95=11 ticks/5s；5 步 × 1–2 ticks 覆盖中位移动，极端移动由 `leg2_max_price` 硬顶截停（正确行为：宁可放弃不锁亏） |
| `maker_leg2_improve_step_ticks` | **1**（5m 市场可配 2） | 同上；步长 2 在 p50=2 ticks 的移动中一轮追上，代价是多付 1 tick |
| `maker_leg2_timeout_seconds` | 5m：**45s**；15m：**180s**；1h/4h：**300s** | 【实测】leg1→leg2 SELL 间隔 p95：BTC/5m 21s、ETH/5m 53s、BTC/15m 147s、ETH/15m 400s；按 p95 附近取值并留有余量，超时进 §3.3 分支 |
| `maker_max_orphan_seconds` | 5m：**90s**；15m：**240s**；1h：**360s**；4h：**600s** | 【实测】同间隔分布 p95–max；21% 低流量市场第一腿后 13min 无对侧 SELL，orphan 上限必须显著小于窗口剩余时间 |
| `MAKER_ACCUMULATE_FORCE_FLATTEN_SECONDS` | 5m：**240s**；15m：**600s**；1h：**900s**；4h：**1500s** | 【推断】必须 > orphan + leg2 timeout（设计 §6 硬约束），且给 EMERGENCY_FLATTEN 的 taker 卖出留深度 |
| `MAKER_ACCUMULATE_MAX_ORDER_SIZE` | **50 shares/腿**（初始 25） | 【实测】bid size at best p50=28.5、p75=100；50 股不超过多数市场 best 档深度中位数，降低排队不确定性 |
| `MAKER_ACCUMULATE_MIN_BOOK_DEPTH` | **10 shares**（挂单价档位最小深度） | 【实测】bid at best p5=5、p25=10；低于 10 的档位多为最小单噪声 |
| `max_orphan_giveback_per_share` | **0.03** | 【实测】5s bid drift p5=−0.03；orphan 90s 窗口内逆向漂移 3 ticks 属常态，小于此值的 giveback 会导致退出单永远不成交 |
| `MAKER_ACCUMULATE_MAX_EPISODES_PER_MARKET_WINDOW` | **3** | 【推断】5m 窗口 300s /（orphan 90s + leg2 45s）≈ 2–3 个串行 episode 上限 |
| `maker_leg1_max_extremity` | **0.45**（即 \|mid−0.5\| ≤ 0.45） | 【推断】价格 <0.05 的腿 tick 相对误差大且 SELL 流稀疏（低流量组 0.08/min）；先只排除极端价外，Shadow 后校准 |
| `MAKER_ACCUMULATE_MIN_COMPLETED_EPISODES` | **100**（沿用设计建议） | 【实测】按 BTC/5m SELL 流 17.5/min 与双/单穿越比 ~9.3% 粗估，主流市场每小时可产生数个终结 episode；100 个样本需多日多市场累计，低流量市场单独达标不现实——评估应按组合并计 |

### 关键取舍说明

1. **buffer 0.005 vs 0.01 是首要权衡**：0.01 在统计上杀死 BTC 短周期全部机会；0.005
   保留 99.6% 几何可行性。orphan 风险不靠 buffer 覆盖，靠 `max_orphan_seconds` +
   `min_hedge_exit_margin` + 熔断覆盖（职责分离，符合设计 §6）。
2. **join vs improve 不可二选一**：实测 69.4% 有 improve 空间、BTC 主流贴死只能 join，
   两档逻辑都是必需的。
3. **strict 成交口径会显著低估 fill**：实测严格穿越仅 0.78%，touch 89.8%——
   Shadow 必须实现 queue 模型（标注 `configured_queue_model`）才有现实的成交模拟，
   否则 Phase 2 统计会把策略误判为"不开仓"。
4. **BTC/15m 边际极薄（p50 = 0.01）**：该组在 buffer=0.005 下几何可行但 margin 阈值
   敏感；它是量最大、穿越最频繁的市场，值得保留并用 Shadow 数据决定取舍。

---

## 7. 局限性

1. **样本窗短且单一**：历史 25.5 min + 实测 13.1 min，均为 2026-07-20 单日、UTC 晚间
   （美盘午后）时段；不覆盖高波动事件（CPI、FOMC、周末薄流动性）。所有分布应视为
   初始先验而非长期稳定值。
2. **历史 best bid 为推断值**：A 中无 bid 字段，`derived_bid_sum = 2 − ask_sum` 依赖
   互补关系；互补关系虽经实测验证（gap p95=0），但 max 0.08 的离群快照存在，
   推断分布在尾部可能偏乐观。
3. **trade-through ≠ maker 成交率**：SELL/min 是打 bid 的事件频率，不是排队成交概率；
   实测 strict 穿越 0.78% 说明大部分 touch 消耗的是队首订单。本报告所有 fill 相关
   数字均为【配置型估计】，禁止当作历史成交率引用。
4. **WS `side` 语义依赖官方推送**：SELL/BUY 分类以 `last_trade_price.side` 为准；
   384/3,181 为 SELL，买卖不对称（大量买入流）本身是市场特征，但若 side 字段口径
   变化，§4 结论需重算。
5. **leg1→leg2 间隔是 taker 流代理**：它度量"对侧什么时候再来一笔 SELL"，不是
   "我的 maker 单什么时候成交"；真实 orphan 时长还取决于队列位置与撤单。
6. **best bid 参考点存在 staleness**：§4.2 的 touch/穿越判定使用该 token 最近一条
   WS book 快照的 best bid，9.4% 的 `price > best_bid` 即由快照间隔内的 bid 移动造成。
7. **观察者（B）数据口径不同**：模型报价 fair−0.02 与 best bid 无固定关系，其
   44.3% 正 edge 比例与 9.3% 双/单穿越比不能直接平移到 join/improve 策略。
8. **市场轮换**：采集窗口内 5m 市场多次到期轮换，快照跨市场拼接；分组统计已按
   asset/timeframe 聚合，单市场轨迹分析受限。

---

## 8. 复现清单

```bash
# 1. 刷新市场配置
python -m poly_arb_bot.cli scan-updown --output data/live_markets.json \
  --intervals 5m,15m,1h,4h --slug-window current,next
# 2. 采集（分段，追加模式）
python scripts/collect_orderbook_snapshots.py --duration 250 --interval 5 \
  --output data/phase0-book-snapshots.jsonl
python scripts/collect_trade_flow_ws.py --duration 250 \
  --output data/phase0-trade-flow.jsonl
# 3. 分析
python scripts/phase0_analysis.py    # -> data/phase0-stats.json
python scripts/phase0_analysis2.py   # -> data/phase0-stats2.json
```

全程只读行情；未提交任何真实订单。
