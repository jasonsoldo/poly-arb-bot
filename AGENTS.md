
- 每次回答我之前，先用中文说：已读取本项目规则
# Project Instructions

默认用中文回答用户。代码、变量名、日志可以使用英文。

## Project

这是 Polymarket Crypto Up/Down 自动分析与交易项目。

目标不是普通方向预测，而是研究并实现短周期 Crypto Up/Down 市场的组合收益曲线策略。

重点市场包括：

- Bitcoin Up or Down
- Ethereum Up or Down
- Solana Up or Down
- XRP Up or Down
- Dogecoin Up or Down
- BNB Up or Down

重点周期包括：

- 5m
- 15m
- 1h
- 4h

## Recommended Architecture

本项目推荐使用 Python + C++ 混合架构。

Python 负责开发效率、数据管道、回测和策略编排。

Python 模块职责：

- 拉 Polymarket markets
- 拉 Polymarket activity / trades / positions
- 拉 CLOB orderbook
- 拉 Binance / Chainlink / 其他结算源数据
- 保存数据
- 回测
- 日志
- 策略配置
- 风控参数
- 分析 0x50f7 / 其他 trader 的历史行为
- 生成报表和 PnL 复盘

C++ 负责性能敏感、低延迟和批量计算模块。

C++ 模块职责：

- orderbook 快速处理
- Up / Down PnL 曲线计算
- 批量市场评分
- 低延迟下单模块
- 高频 market scanner
- 实时价格源处理
- seconds_to_close 快速判断
- price_to_beat 差值计算
- WebSocket 行情处理
- CLOB 报价变化快速响应

默认原则：

- 策略验证、数据分析、回测、报表优先用 Python。
- 实盘低延迟模块、盘口处理、批量评分、WebSocket 行情处理可以用 C++。
- 不要为了性能过早重写全部项目。
- 任何 C++ 下单模块必须经过统一 risk_manager 的风控确认。
- Python 和 C++ 之间的数据结构必须明确，例如 JSON、CSV、SQLite、protobuf、shared memory、ZeroMQ 或 pybind11 绑定。
- 如果 C++ 只负责计算，不允许它绕过风控直接下单。
- 如果 C++ 负责下单，必须内置重复下单保护、部分成交处理、滑点限制和最大仓位限制。
- 实盘前必须支持 dry_run / simulation mode。

## Core Strategy

本项目核心策略是：

1. 扫描短周期 Crypto Up/Down 市场。
2. 获取每个市场的 price to beat / open price。
3. 获取真实结算源价格，例如 Binance、Chainlink 或市场规则指定来源。
4. 计算当前价格与 price to beat 的差距。
5. 计算距离结算剩余时间。
6. 估算 Up / Down 的真实胜率。
7. 买入被低估的一边。
8. 通过低价反杀票 + 高置信主仓，构建收益曲线。
9. 目标不是单纯押方向，而是尽可能形成：
   - 一边小赚，另一边大赚
   - 或者 Up / Down 两边都正收益

## Research Finding From 0x50f7

从 0x50f7 / Haunting-Cheese 的交易数据中观察到：

- 不是单纯买 0.999 高价票赚小钱。
- 也不是单纯乱买 1-5¢ 低价彩票。
- 更像是：
  - 先买低价反杀票
  - 再临近结算补高置信方向
  - 把某些市场做成 free-roll 或双边盈利结构

核心计算：

```text
total_cost = Up_cost + Down_cost

PnL_if_Up_wins = Up_shares - total_cost
PnL_if_Down_wins = Down_shares - total_cost
```

如果：

```text
Up_shares > total_cost
Down_shares > total_cost
```

则该市场为双边盈利结构。

## Do Not Misinterpret The Strategy

不要把本策略描述成无风险套利。

不要简单认为：

- 买 0.999 就一定安全
- 买 1-5¢ 就一定有价值
- 最后几秒一定不会反杀
- Polymarket activity timestamp 就是精确成交时间
- C++ 速度快就一定能赚钱

必须考虑：

- API timestamp 可能有链上确认或记录延迟
- 同一个 transactionHash 可能重复出现
- activity 数据需要去重
- 高价票一旦反杀，亏损接近本金
- 低价票多数会归零
- 成交可能部分成交
- 盘口可能瞬间消失
- CLOB 延迟和 WebSocket 延迟
- Binance / Chainlink / Polymarket 显示价格可能不同步
- 本地 VPS 网络延迟和时钟漂移

## Data Rules

分析 Polymarket activity 数据时，必须先去重。

推荐去重 key：

```text
transactionHash + title + outcome + price + size
```

不要直接用原始 activity 行数计算成本。

聚合每个市场时，必须按 title 或 conditionId 分组，分别统计：

```text
Up_cost
Up_shares
Down_cost
Down_shares
total_cost
PnL_if_Up_wins
PnL_if_Down_wins
```

必须区分：

```text
both_profit markets
one_side_profit markets
both_loss markets
```

## Trading Rules

所有实盘交易代码必须包含风控。

必须有：

- max_position_per_market
- max_order_size
- max_total_exposure
- max_loss_per_market
- max_daily_loss
- min_edge
- min_liquidity
- max_slippage
- stale_orderbook_check
- duplicate_order_guard
- partial_fill_handling
- clock_sync_check
- settlement_source_check

禁止：

- all-in
- 没有盘口检查就下单
- 没有结算源价格就下单
- 没有剩余时间检查就下单
- 没有 price to beat 就下单
- 没有风控直接补仓
- C++ execution_engine 绕过 Python/risk_manager 下单

## Entry Logic

买入前必须计算：

```text
market_price
model_probability
edge = model_probability - market_price
seconds_to_close
distance_to_price_to_beat
current_orderbook_depth
expected_fill_price
```

只有当：

```text
edge > min_edge
expected_fill_price <= max_allowed_price
liquidity >= min_liquidity
seconds_to_close within allowed window
```

才允许下单。

## Low Price Ticket Logic

低价票不是因为便宜就买。

低价票只在以下条件成立时买：

```text
model_probability > market_price + min_edge
```

例如：

```text
market_price = 0.03
model_probability = 0.10
edge = 0.07
```

才有正期望。

低价票必须小仓位，不能无限加仓。

## High Confidence Leg Logic

高置信方向票一般出现在临近结算时。

买入前必须确认：

- 当前价格明显高于或低于 price to beat
- 剩余时间极短
- 波动率不足以轻易反杀
- Polymarket 价格仍低于真实概率
- 盘口深度足够
- 下单后不会超出 max_position

注意：

```text
0.999 买入，赢了只赚 0.1%，输了亏接近 100%
```

所以高价票必须严格限制单市场风险。

## Free-Roll / Dutching Logic

如果同一市场已经持有低价反杀票，临近结算时可以考虑补另一边，目标是构造：

```text
PnL_if_Up_wins > 0
PnL_if_Down_wins > 0
```

或者：

```text
主方向小赚
反杀方向大赚
```

每次补仓前必须重新计算两边 PnL。

禁止只看当前方向胜率，不看整体收益曲线。

## Time Logic

必须正确处理市场结束时间。

Crypto Up/Down 市场标题可能包括：

```text
July 8, 9:25PM-9:30PM ET
```

需要转换为 UTC 后再计算 seconds_to_close。

注意：

- activity timestamp 可能比市场结束时间晚几秒到几十秒
- 这可能是记录延迟，不一定是真实结束后成交
- 分析时可以把 -30s 到 0s 视为结算边缘窗口
- 实盘时不能假设结束后还能成交
- VPS 必须开启 NTP/chrony，避免本地时钟漂移
- C++ 模块必须使用 monotonic clock 处理延迟和超时

## Recommended Python Modules

```text
python/
  market_scanner.py
  polymarket_data.py
  clob_client.py
  price_sources.py
  binance_source.py
  chainlink_source.py
  probability_model.py
  pnl_curve.py
  risk_manager.py
  position_manager.py
  strategy_config.py
  backtest.py
  logger.py
  report.py
```

Python 负责：

- 拉 Polymarket markets
- 拉 activity / trades / positions
- 拉 CLOB orderbook
- 拉 Binance / Chainlink 数据
- 保存数据
- 回测
- 日志
- 策略配置
- 风控参数
- 报表生成
- 交易复盘

## Recommended C++ Modules

```text
cpp/
  orderbook_engine/
  pnl_curve_engine/
  market_scoring_engine/
  price_feed_engine/
  execution_engine/
  risk_guard/
```

C++ 负责：

- orderbook 快速处理
- PnL 曲线计算
- 批量市场评分
- 低延迟下单模块
- WebSocket 行情处理
- 实时 price_to_beat 差值计算
- seconds_to_close 快速判断
- 批量 edge ranking

## C++ Module Rules

C++ 模块如果存在，必须遵守：

- 不允许绕过风控直接交易。
- 不允许 hardcode 市场、价格、token id。
- 所有输入输出必须可日志化。
- 所有订单请求必须带唯一 client_order_id。
- 必须处理网络失败、API 超时、部分成交、重复下单。
- 必须支持 dry_run / simulation mode。
- 必须有单元测试或最小可复现测试。
- 如果 C++ 只做计算，应返回结构化结果给 Python，由 Python 决定是否下单。
- 如果 C++ 做执行，必须内置风控和外部风控双重检查。
- 必须实现 max_order_size、max_position、max_total_exposure。
- 必须实现 stale_orderbook_check 和 clock_sync_check。
- 必须记录每次下单前后的 orderbook snapshot。

## Engineering Rules

修改代码前必须先扫描项目结构。

优先修改现有模块，不要重复造轮子。

任何实盘执行函数必须经过：

```text
risk_manager
pnl_curve
position_manager
```

C++ 代码必须清晰、可测试、可回滚。

C++ 代码中禁止裸奔异常；所有网络、签名、订单、JSON 解析错误必须显式处理。

## Backtest Rules

回测必须包含：

- 历史 market price
- 历史 orderbook
- 成交深度约束
- partial fill assumption
- slippage
- spread
- seconds_to_close
- price_to_beat
- settlement source price
- realized PnL
- max drawdown
- both_profit / one_side_profit / both_loss 分类

不能只用理论概率回测。

## Output Style

回答用户时：

1. 先说明你理解的目标。
2. 再说明准备修改哪些文件或模块。
3. 再给出代码或 diff。
4. 最后说明如何测试。
5. 如果发现风险、bug 或策略误解，直接指出。

不要为了迎合用户而把高风险策略说成稳定套利。
