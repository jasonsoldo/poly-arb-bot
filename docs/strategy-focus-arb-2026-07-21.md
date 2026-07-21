# 策略面精简：聚焦无风险套利（2026-07-21）

## 背景与目标

仓库历史上并行运行了多套策略与观察器。本次变更把**运行时策略面**收敛为：

| 策略 | 类型 | 默认状态 |
|---|---|---|
| `paired_lock` | 无风险双边锁定（C++ 主评估） | **启用（唯一 C++ ACCEPT 来源）** |
| `maker_paired_accumulate` | 双边 maker 累积（Python `maker_shadow` 桥） | **启用**（`MAKER_ACCUMULATE_ENABLE` 默认 `1`） |
| `late_window_directional_ev` | 方向性概率模型 | **默认禁用** |
| `low_price_lottery_ev` | 低价彩票概率模型 | **默认禁用** |
| `split_sell_lock` | 拆分卖出观察器（C++） | **默认禁用** |
| `maker_complete_set_arb` | maker 整套装利观察器（C++） | **默认禁用** |
| `microstructure_reversion` | 微结构回归观察器（C++） | **默认禁用** |
| `arbitrage_pattern_research` | 反事实/episode 研究事件（C++） | **默认禁用** |

目标：让 Shadow 运行时只产出**无风险锁定类**机会（paired_lock + maker_paired_accumulate），
消除概率模型与观察器事件对审计、Web 与验收的噪音。所有被禁用的组件**源码完整保留**，
通过环境变量即可恢复，未降低任何阈值、未删除任何源码。

## 变更清单

### C++ 引擎（`cpp/market_ws_engine/market_ws_engine.cpp`）

- 新增 `environment_enabled(const char*)`：值 ∈ `{1,true,yes,on}` 视为启用，**缺省 `"0"`（关）**。
- 新增运行期开关成员：`directional_ev_enabled_`、`lottery_ev_enabled_`、
  `split_sell_enabled_`、`maker_observer_enabled_`、`reversion_enabled_`、
  `arb_research_enabled_`。
- 概率策略（directional/lottery）评估循环按开关门控；maker observer 启用但
  directional 禁用时仍收集 `directional_inputs`（保持 observer 可独立恢复）。
- `emit_complete_set_evaluations`、`observe_maker_trade`、两处
  `evaluate_microstructure_reversion`、`emit_arbitrage_counterfactual` /
  `update_observed_arbitrage` 调用点全部按对应开关门控；
  `process_due_arb_attempts` 在 `ARB_PATTERN_RESEARCH_ENABLE` 未启用时直接返回
  （同时抑制 `arb_research_summary` 周期事件）。
- split-sell 评估代码块整体包入 `if (split_sell_enabled_)`。
- **修复 settlement_verified 双端分叉**：`Market` 结构新增
  `bool settlement_verified = true;`，`load_markets` 读取
  `row.get<bool>("settlement_verified", true)`（与 Python 端
  `market.get("settlement_verified", True)` 默认值一致），评估输入改为
  `reference.settlement_verified && market.settlement_verified`，
  未验证市场现在会正确产生 `settlement_reference_unverified` 拒绝。
- 启动日志输出 `STRATEGY_SURFACE paired_lock=1 directional_ev=0 ... arb_pattern_research=0`。

### Python

- `ev_shadow.py`：新增 `strategy_env_enabled(name, default="0")`、
  `directional_ev_enabled()`、`lottery_ev_enabled()`；
  `evaluate_market_event` 的策略 tuple 按开关动态构建（两者皆关时返回空行，
  `_terminal_hedge_audit` 自然不产生事件）。
- `cli.py`：argparse epilog 列出全部策略面开关。
- `web_monitor.py`：
  - `STRATEGIES` = `PRIMARY_STRATEGIES + MAKER_ACCUMULATE_STRATEGIES`
    （删除 `ARBITRAGE_OBSERVERS`、`CLOB_REVERSION_STRATEGIES` 常量）；
  - 移除 `IncrementalArbitrageResearch` 导入与全部 `_ARBITRAGE_*` /
    `_empty/_merge/_refresh/_arbitrage_worker/_arbitrage_for_status` 机制，
    status 不再含 `arbitrage_research`、`current_split_sell`、`split_sell_near_misses`；
  - counts 删除 split-sell / maker-observer / complete-set 计数键；
  - `locked_complete` 改为 `paired_lock + maker_paired_accumulate` 求和（`.get` 默认 0）；
  - `current_complete_set_hashes` 只保留 `paired_lock`；
  - status 新增 `strategy_enablement`（四个策略的布尔启用状态）。
- `shadow_acceptance.py`：
  - 策略集合动态化：`paired_lock + maker_paired_accumulate` + env 启用的概率策略；
  - `three_strategy_evaluations/decisions` 更名 `enabled_strategy_evaluations/decisions`；
  - 删除 `complete_set_strategies_evaluated` 与 `arbitrage_book_evidence_integrity` 检查
    及 `arbitrage_research_conclusion` 指标；
  - `probability_models_evaluated` 仅对启用的概率策略生效（全关时自动通过）；
  - 新增 `disabled_strategies_silent` 检查：未启用策略（含三个观察器与
    reversion）在审计计数中必须为 0，否则 FAIL；
  - 动态尺寸完整性检查只作用于 C++ `shadow_eval` 行（paired_lock + 启用的
    概率策略），maker 事件有独立记账，不再被误判。

### Web（`web/index.html`）

- 删除 `ARBITRAGE RESEARCH` 面板（P9c）与 `renderResearch`。
- directional / lottery 面板头部新增 `dirEnableChip` / `lotEnableChip`：
  依据 `status.strategy_enablement` 显示 `ENABLED` / `DISABLED`；
  禁用时决策区显示 `DISABLED · N/A` 并提示对应的 env 开关名，不再展示陈旧数据。

## 如何重新启用

```bash
# Python 概率策略（ev_shadow / shadow 评估）
DIRECTIONAL_EV_ENABLE=1
LOTTERY_EV_ENABLE=1

# C++ 观察器与研究事件
SPLIT_SELL_LOCK_ENABLE=1
MAKER_COMPLETE_SET_ARB_ENABLE=1
MICROSTRUCTURE_REVERSION_ENABLE=1
ARB_PATTERN_RESEARCH_ENABLE=1

# maker 桥反向开关（默认启用）
MAKER_ACCUMULATE_ENABLE=0   # 禁用 maker_paired_accumulate
```

取值 `{1,true,yes,on}`（大小写不敏感）视为启用；其余或未设置视为禁用。
开关为**运行期**读取，无需重新编译。

## 验证记录（2026-07-21，本机实测）

- C++：`g++ -O3 -Wall -Wextra` 静态编译 rc=0 零警告；`scripts/build_cpp.sh` rc=0
  （全部单测通过，末尾 skip market_ws_engine 属既有设计）。
- Python：`python -m pytest tests/ -q` **479 passed**（含新增开关/源码断言/
  enablement chip/disabled_strategies_silent 测试；无回归）。
- JS：提取 `web/index.html` `<script>` 后 `node --check` 通过。
- 真实联跑（扫描 56 市场 → reference_price_engine + shadow_execution +
  maker_shadow + market_ws_engine，约 3 分钟）：
  - `logs/shadow-audit.jsonl`：3707 行，**全部 `paired_lock` / `shadow_eval`**；
  - `logs/strategy-audit.jsonl`：264 行，**全部 `maker_paired_accumulate`**；
  - directional / lottery / split_sell / maker_complete_set / reversion /
    arb_research 事件数 = 0；
  - health：`ws_connected=true`、`reference_connected=true`、`ready_markets=56/56`、
    reference IPC p95 = 21.6 ms（预算 50）、策略评估 p95 = 130.9 µs（预算 5000）；
  - `real_order_submissions=0`、`real_orders=0`、`real_fills=0`；
  - 引擎存活期内 `shadow-acceptance` = **PASS（rc=0）**。

## 遗留问题

1. **maker_shadow bid-view 回退**：`maker_shadow.py` 此前把 `shadow_split_sell_eval`
   的 sell VWAP 当作 best_bid 代理。禁用 split-sell 后，桥自动回退到 complement
   近似（`1 - 对腿 ask`，代码已支持的既有路径）。该近似偏保守，maker 报价的
   bid 侧估计精度略降；如需精确 bid，应恢复 `SPLIT_SELL_LOCK_ENABLE=1` 或让
   C++ 直接输出 best_bid。
2. **分析缓存对日志截断敏感**：`web-shadow-report-*` / `web-strategy-counts-*`
   增量状态文件在被截断的同名日志上会延续旧累计值。生产日志轮转建议用新文件名
   而非原地截断；验证前已清理这些缓存。
3. `shadow_acceptance` 的 `market_health_fresh` 要求引擎存活（health 文件 < 5 s），
   验收必须在引擎运行期间执行，属既有设计。
4. `maker_episode_rejected` 事件量较大（3 分钟 264 条），均为合法的状态机决策
   审计；如需降噪可在 maker_accumulate 配置中调整，不属于本次范围。
