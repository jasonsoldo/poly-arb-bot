# 真实市场 Shadow 盈利准入设计

日期：2026-07-29

## 1. 背景

当前 Dashboard 在 `CALIBRATION_UNTHROTTLED` 模式下显示：

- 2081 个 completed Shadow 样本；
- 模拟净 PnL 为负；
- 研究采样绕过组合限仓；
- 研究采样 PnL 与可部署配置表现容易被混淆。

2026-07-29 的 `716f2f0` 已隔离概率校准 cohort，但校准准确不等于被 EV
规则选中的交易具有正期望。全局 Brier、命中率或模型概率不能代替按真实可成交成本
计算的样本外净收益。

本设计不承诺盈利，也不开放真实下单。目标是建立一个 fail-closed 的证据链：

1. 用当前 cohort 的已结算数据发现候选；
2. 冻结准入规则；
3. 用全新的限仓 Shadow 样本验证；
4. 只有样本外净收益通过预先确定的门槛，才报告 Shadow 盈利证据。

## 2. 非目标

- 不启用真实订单、真实成交或真实资金；
- 不通过修改历史记录把已有亏损曲线变成盈利；
- 不把 calibration 样本当作可部署 PnL；
- 不为了增加 ACCEPT 数量降低费用、深度、freshness、reference quorum 或风险标准；
- 不在本阶段重建完整概率模型；
- 不改变 `paired_lock` 的独立 ACCEPT 语义；
- 不把外部参考价格用于 `paired_lock` 准入。

## 3. 成功定义

系统必须区分两个结论：

### 3.1 初步正收益

冻结配置后的全新限仓 Shadow 样本同时满足：

- completed 独立市场的总净 PnL 大于 0；
- 单位风险平均净收益大于 0；
- 所有账本、真实订单和数据完整性不变量通过。

该状态只能描述为“初步正收益”，不能描述为已证明盈利。

### 3.2 Shadow 盈利证据 PASS

除满足初步正收益外，还必须：

- 连续运行不少于 48 小时；
- 完成不少于 300 个全新独立 `market_id`；
- 每个启用的 profitability cohort 至少完成 50 个独立市场；
- 按市场结束时间块计算的单侧 95% 置信下界大于 0；
- 最大回撤不超过验证起始 Shadow capital 的 10%；
- `real_order_submissions = 0`；
- `real_orders = 0`；
- `real_fills = 0`。

样本不足或置信下界未过线时返回 `INCOMPLETE`，不得返回 `PASS`。

## 4. 核心架构

保持三套既有策略独立。新增盈利准入只作用于：

- `late_window_directional_ev`
- `low_price_lottery_ev`

数据处理分为两条隔离的数据流。

### 4.1 研究流

研究流记录全部满足基础数据完整性的模型候选，不建立可部署 Shadow 仓位。它在市场
结算后生成反事实结果，用于：

- 概率校准；
- 候选 cohort 发现；
- 负期望分组诊断；
- 后续模型研究。

研究流允许持续积累数据，即使 profitability gate 拒绝交易。这样不会因 fail closed
造成数据死锁。

研究流必须明确标记：

```text
risk_mode = CALIBRATION_RESEARCH
portfolio_limits_enforced = false
deployable_pnl = false
```

### 4.2 限仓 Shadow 流

限仓 Shadow 流只有在下列条件全部成立时才建立模拟仓位：

1. 原策略的全部独立 ACCEPT 条件通过；
2. profitability gate 存在且有效；
3. 当前候选匹配一个允许的 cohort；
4. 尾盘时间窗口已启用；
5. 组合限仓、日亏损和连续亏损限制已启用；
6. 当前配置、概率模型和准入表 hash 完全匹配。

限仓流必须明确标记：

```text
risk_mode = PORTFOLIO_LIMITS_ENFORCED
portfolio_limits_enforced = true
deployable_pnl = true
```

Dashboard 的“可部署 Shadow PnL”只能读取这条流。

## 5. 真实成本与账本口径

每个 completed 样本必须从 canonical audit 和 lifecycle ledger 对账，至少验证：

- 唯一 `event_id`；
- `market_id`、`condition_id`、token 和 outcome 一致；
- strategy、config hash、probability model ID 一致；
- 入场时的目标规模 ask-side VWAP；
- 订单簿深度足以覆盖目标规模；
- 市场实际动态 fee schedule；
- taker fee 按该市场官方公式与舍入规则计算；
- slippage、latency buffer、settlement buffer 单独记录；
- 市场最终结算结果；
- realized simulated PnL 可由记录字段重新计算。

禁止使用网页 midpoint、last trade 或单档 best ask 冒充目标规模可成交价格。

统一输出两个收益口径：

```text
net_pnl_usd
net_return_per_dollar_risked
```

`net_pnl_usd` 只按模拟可成交现金流计算：结算 payout 或真实 bid-side 模拟退出收入，
减去 ask-side VWAP 入场成本、动态费用和实际模拟退出费用。`latency_risk_buffer`、
`settlement_risk_buffer` 和模型不确定性 buffer 只用于入场 EV 门，不得再次作为现金成本
从 realized simulated PnL 中扣除。

不允许在同一统计中混用 per-share、total-dollar 和 return-on-risk。

该口径仍是基于可执行订单簿快照的 Shadow 模拟，不能证明真实订单一定能按观测 VWAP
成交。最终状态必须继续标记 `SHADOW / NOT REAL MONEY`。

## 6. 数据切分与候选发现

### 6.1 独立样本

统计单位为独立 `market_id`。同一市场内的重复 evaluation、重复候选或多个审计心跳
不得被当成多个独立样本。

同一市场出现多个可模拟入场时，使用冻结的、确定性的首次合格入场规则；其他事件仅作
研究观察，不进入盈利样本数。

### 6.2 时间切分

按市场结束时间排序并按完整市场分组切分。任何 `market_id` 不得同时进入发现集和
验证集。

当前 cohort 的数据只用于：

- 账本验证；
- 负收益归因；
- 候选 cohort 发现；
- 冻结准入规则。

最终盈利结论只能来自规则冻结后产生的新市场。不得在看到验证结果后修改准入规则并
继续沿用同一验证窗口。

### 6.3 Cohort 维度

只使用已经存在且可审计的字段构造候选，初始维度为：

- strategy；
- asset；
- timeframe；
- outcome；
- calibration input probability bucket；
- expected fill price bucket；
- seconds-to-close bucket。

不新增一次性特征或复杂模型。若一个更粗的 cohort 已满足证据要求，不继续细分。

### 6.4 候选准入

候选必须在发现数据中具备：

- 至少 50 个独立已结算市场；
- 平均净收益大于 0；
- 时间块单侧 95% 置信下界大于 0；
- 成本和结算字段完整；
- 单个市场贡献不超过该 cohort 正收益总额的 25%；
- 结果不依赖无法在实时路径获得的未来信息。

候选最小样本数、置信水平和收益下界作为冻结值写入准入表并参与 hash；不得在验证期间
动态修改。修改任何一个值都必须旋转 profitability cohort version 并开始新的验证窗口。

如果没有候选通过，发布空的有效准入表，系统进入 `NO_TRADE`。

时间块置信下界使用确定性 block bootstrap：按 UTC 4 小时边界把 `market_id` 分组，
有放回抽样 10,000 次，净收益均值分布的第 5 百分位为单侧 95% 下界。随机种子由
profitability cohort version 与发现窗口身份计算，保证 Python、验收工具和重复运行
得到同一结果。

## 7. Profitability Gate 文件

新增一个原子发布的 JSON 文件：

```text
data/profitability-gates.json
```

文件至少包含：

- schema version；
- generated at；
- expires at，初始验证表固定为激活后 72 小时；
- source audit identity；
- discovery window start/end；
- strategy config hash；
- probability model ID；
- frozen calibration map content hash；
- profitability cohort version；
- cohort dimensions；
- 每个 cohort 的独立样本数；
- 平均净收益；
- 置信下界；
- decision；
- rejection reason。

文件写入流程必须为：

```text
write temporary
→ flush
→ fsync when supported
→ atomic replace
```

C++ 引擎和 Python parity 路径必须执行相同校验。以下任一情况均 fail closed：

- 文件缺失；
- JSON 或 schema 无效；
- 文件过期；
- strategy config hash 不匹配；
- probability model ID 不匹配；
- frozen calibration map content hash 不匹配；
- cohort version 不匹配；
- 当前候选无法匹配允许 cohort；
- 样本数或置信门槛不足。

统一拒绝原因为：

```text
profitability_gate_unavailable
profitability_cohort_not_eligible
```

拒绝事件仍保留研究流观测，不建立限仓 Shadow 仓位。

## 8. 配置与生命周期隔离

新增盈利准入版本必须参与方向和彩票的 canonical strategy hash。冻结准入表后：

1. 关闭 `SHADOW_CALIBRATION_MODE`；
2. 显式启用方向策略尾盘窗口；
3. 恢复全部组合风控；
4. 旋转 profitability cohort version；
5. 重启 C++ engine、shadow lifecycle 和 Web；
6. 从新 hash 开始统计可部署 Shadow PnL。

历史研究记录保留且不可修改，但不计入新 hash 的可部署 PnL。

概率校准映射与 profitability gate 是两个独立门：

- 概率校准回答“模型概率如何修正”；
- profitability gate 回答“经修正后、被选中的真实可成交子样本是否有净正收益证据”。

任一门不可用都不得 ACCEPT。

验证开始时必须把当时有效的概率校准映射冻结为带内容 hash 和 72 小时过期时间的验证快照。
profitability gate 绑定该快照的内容 hash；整个验证窗口内不得替换该快照。新产生的研究
预测继续写入 canonical ledger，但只在本轮验证结束后用于构建下一轮校准映射。这样可以
避免滚动校准在验证期间改变策略，也避免使用验证结果反向影响同一验证窗口。

## 9. Dashboard

Dashboard 必须分开展示：

### 9.1 Calibration Research

- 独立已结算市场数；
- 反事实 PnL；
- probability calibration；
- cohort 发现状态；
- 明确标签 `RESEARCH ONLY / NOT DEPLOYABLE PNL`。

### 9.2 Portfolio-Limited Shadow

- 当前 strategy/config/gate hash；
- 已运行时间；
- completed 独立市场数；
- 每个启用 cohort 的样本数；
- 总净 PnL；
- 单位风险平均净收益；
- 最大回撤；
- 置信下界；
- `PASS / FAIL / INCOMPLETE`；
- real order invariants。

历史配置 PnL 继续可查看，但不得合并到当前配置主指标。

## 10. 失败处理

以下数据不得进入候选发现或验证：

- 无法解析的 JSONL；
- 缺失稳定身份字段；
- 重复事件；
- market/outcome/settlement 无法匹配；
- 目标规模深度不足；
- fee schedule 缺失或格式错误；
- config/model/gate hash 不匹配；
- 非当前验证窗口；
- 无法重算 realized simulated PnL。

分析器必须报告排除数量和原因。只要排除会改变验收结论，结果必须为
`INCOMPLETE` 或 `FAIL`，不能静默忽略。

准入表生成失败时保留上一份未过期、hash 完全匹配的文件；不得发布半写文件或空成功。
上一份文件过期后系统必须拒绝新的限仓 Shadow 入场。

## 11. 验收状态

### PASS

必须同时满足第 3.2 节全部条件及现有 shadow acceptance 不变量。

### FAIL

以下任一条件成立：

- 样本量已经达到门槛，但总净 PnL 小于等于 0；
- 单位风险平均净收益小于等于 0；
- 最大回撤超过验证起始 Shadow capital 的 10%；
- 账本或成本重算不一致；
- 同一市场跨发现/验证集合；
- real-order invariant 失败；
- 数据损坏使结果不可信。

### INCOMPLETE

基础设施和安全不变量通过，但：

- 未运行满 48 小时；
- 独立市场少于 300；
- 任一启用 cohort 少于 50；
- 净 PnL 为正但 95% 置信下界小于等于 0。

`INCOMPLETE` 不得被 Dashboard 或 CLI 显示成 PASS。

## 12. 测试策略

### 12.1 单元测试

- ask-side VWAP 和深度；
- 动态 fee 与舍入；
- realized simulated PnL 重算；
- `market_id` 去重；
- 确定性首次合格入场；
- 时间切分无市场泄漏；
- 时间块置信下界；
- UTC 4 小时 block bootstrap 的确定性；
- gate schema/hash/model/version/expiry；
- frozen calibration snapshot hash 和验证期间不可变性；
- 空准入表进入 `NO_TRADE`；
- 缺失 gate fail closed；
- 研究流与限仓流 PnL 隔离。

### 12.2 集成测试

- 用固定 JSONL fixture 重放完整发现、冻结和验证过程；
- Python 与 C++ 对同一候选给出相同 gate decision；
- 配置版本旋转后旧 gate 和旧 completed PnL 被排除；
- Web 只把限仓流计入可部署 PnL；
- 概率校准不可用时仍继续研究观测但不建立限仓仓位。

### 12.3 回归验证

运行仓库完整测试发现命令：

```text
python -X utf8 -m unittest discover -s tests -p "test_*.py" -v
```

以及项目已有的 C++ strategy/engine 测试和 `shadow-acceptance`。最终验证输出必须保存：

- 测试命令及退出码；
- 当前 commit；
- strategy/config/gate hash；
- acceptance JSON；
- real-order invariants。

## 13. 发布顺序

1. 实现只读盈利分析和账本重算；
2. 用当前数据生成诊断报告，不改变入场；
3. 实现 gate 文件、Python/C++ parity 和 fail-closed 测试；
4. 实现研究流与限仓流统计隔离；
5. 更新 Dashboard；
6. 冻结候选准入表并旋转配置版本；
7. 关闭 calibration unthrottled，启动全新限仓 Shadow；
8. 达到验收窗口后运行机器可执行验收；
9. 根据证据报告 PASS、FAIL 或 INCOMPLETE。

每一步都保持 Shadow-only。真实订单数必须永久为 0。
