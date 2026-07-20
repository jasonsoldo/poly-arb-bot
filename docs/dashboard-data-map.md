# Dashboard 数据映射文档（任务 B：真实数据映射）

> 目的：为 `web/index.html` 按参考图（CLAUDE QUANT 终端风仪表盘，881×1062）重写提供**真实数据绑定依据**。
> 硬约束（AGENTS.md §16）：只展示 canonical audit 与 health state 中的真实数据；禁止假订单数、假 PnL 曲线、假 equity curve、假 latency bar；未知值一律 `N/A`；三策略分区展示；paired_lock 完整成本链；外部行情在 paired_lock 区域标注 REFERENCE ONLY。
>
> 数据来源文件：
> - `poly_arb_bot/web_monitor.py`（`build_status()` L612–1187，`build_report_empty()` L116–132，HTTP 路由 L1190–1223）
> - `poly_arb_bot/ev_strategies.py`（方向/彩票评估与 `decision_audit` 审计字段）
> - `poly_arb_bot/shadow_acceptance.py`（验收不变量）
> - `poly_arb_bot/reference_layer.py`（`ReferenceState` / `aggregate_reference` / `reference_state_for_asset`）
> - `web/index.html`（现有 390 行前端）

---

## 1. `GET /api/status` 返回 JSON 完整字段树

`build_status(data_dir, log_file, state_file)` 每次请求同步构建（大文件分析走后台线程，见 `analytics_refreshing`）。顶层字段如下。

### 1.1 顶层标量

| 字段 | 类型 | 含义 | 为空情况 |
|---|---|---|---|
| `ts` | int | 服务器 unix 秒 | 不会为空 |
| `mode` | str | 固定 `"DRY RUN"` | 不会为空 |
| `system_status` | str | `ONLINE` / `DEGRADED` / `BLOCKED`。health 缺失/过期/WS 断开→BLOCKED；reference 文件过期→DEGRADED | 不会为空 |
| `analytics_refreshing` | bool | 报告/策略计数/套利研究任一在后台重建 | 不会为空 |
| `analytics_status` | str | `READY` / `REBUILDING` | 不会为空 |

### 1.2 `counts` —— 全局计数（对象，键始终存在）

| 字段 | 类型 | 含义 / 来源 |
|---|---|---|
| `raw_signals` | int | `signals` 长度（过滤到已发现市场后） |
| `model_edges` | int | 模型 edge 超过 `min_edge` 的 signal 数 |
| `risk_passed` | int | 通过 `_signal_block_reason` 的 signal 数 |
| `executed_orders` | int | state 中 status∈{filled,partially_filled,submitted} 的决策数；Shadow 下**必须恒为 0**（验收不变量） |
| `risk_decisions` | int | `client_order_ids` 记录数 |
| `shadow_attempts` | int | status==dry_run 数 |
| `shadow_evaluations` / `total_strategy_evaluations` | int | 全策略 evaluations 合计 |
| `probability_strategy_evaluations` | int | 方向+彩票 evaluations |
| `paired_evaluations` / `split_sell_evaluations` / `split_sell_accepts` / `maker_evaluations` / `maker_quote_candidates` | int | 各 complete-set 策略计数 |
| `maker_quote_geometry_candidates` / `maker_trade_events` / `maker_single_leg_trade_throughs` / `maker_both_leg_trade_throughs` | int | 来自 `shadow-health.json` |
| `complete_set_evaluations` | int | paired+split_sell+maker 合计 |
| `fok_passed` | int | 报告 FOK 通过数 |
| `shadow_accepts` | int | max(策略计数 accepts, 报告 accepts) |
| `model_accepts` | int | 方向+彩票 ACCEPT 合计 |
| `simulated_opened` / `active_shadow_positions` / `unique_opportunities` / `active_opportunities` | int | Shadow 生命周期计数 |
| `simulated_complete` | int | `report.performance.completed` |
| `locked_complete` | int | paired_lock+split_sell_lock 完成数 |
| `session_strategy_evaluations` / `session_paired_evaluations` / `session_split_sell_evaluations` / `session_split_sell_accepts` / `session_maker_quote_candidates` | int | 当前引擎 session 计数（来自 health） |

### 1.3 `shadow_report`（= 顶层 `report`，由 `IncrementalReport` 生成；`build_report_empty()` 定义空结构）

| 字段 | 类型 | 含义 | 为空情况 |
|---|---|---|---|
| `markets_seen` | int | 见过的市场数 | 0 |
| `evaluations` | int | 有效评估数（去重后） | 0 |
| `fok_passed` | int | FOK 深度模拟通过数 | 0 |
| `accepts` | int | ACCEPT 总数 | 0 |
| `invalid_json` | int | 坏行数 | 0 |
| `future_events` | int | 未来时间戳事件数 | 0 |
| `duplicate_events` | int | 重复 event_id 数（观测指标，不计入 evaluations） | 0 |
| `accepted_evaluations` / `rejected_evaluations` | int | 接受/拒绝评估数；不变量：二者之和 = evaluations | 0 |
| `rejection_reasons` | object {reason: count} | 拒绝原因分布；不变量：sum = rejected_evaluations | `{}` |
| `opportunity_duration_ms` | {p50, p95, max} | 机会持续时间分位数 | 各项可为 `null` → N/A |
| `source_age_ms` | {latest, p50, p95, p99, max, samples} | CLOB 消息年龄（**MESSAGE AGE，不是 LATENCY**） | 各项可为 `null` |
| `performance` | object | 见下 | completed=0 时 pnl/win_rate/sharpe 为 `null` → N/A |
| `performance_by_strategy` | {strategy: performance} | 按策略拆分，key 含 3 主策略 + split_sell_lock + maker_complete_set_arb + microstructure_reversion | 同上 |
| `equity_curve` | array [{equity, ...}] | **模拟** completed 交易的权益曲线；空数组常见，绝非真实资金曲线 | `[]` |
| `trade_ledger` | array [{market_id, ts, pnl, strategy, ...}] | 已完成 Shadow 交易台账 | `[]` |
| `asset_latest_pnl` | {asset: {pnl, strategy, timeframe, market_id} \| null} | 各资产最近一笔完成模拟 PnL | 值为 `null` → N/A |
| `current_strategy_config_hash` / `current_strategy_config_hashes` / `current_paired_config_hash` | str/object | 当前配置 hash（用于过滤历史配置） | 可缺省 |
| `excluded_other_strategy_config` | int | 被排除的历史配置完成数 | 0 |

`performance` 子结构（`build_report_empty` 的 `empty_performance`）：
`completed:int, wins:int, losses:int, simulated_pnl:float|null, win_rate:float|null, sharpe:float|null, sharpe_samples:int`。
**completed=0 时 simulated_pnl/win_rate/sharpe 全为 null，前端必须显示 N/A（AGENTS.md §6/§12）。**

### 1.4 `strategy_counts` / `session_strategy_counts`

`strategy_counts`: `{strategy: {evaluations, accepts, rejections, model_evaluations, latest_model_evaluated, unique_opportunities, active_opportunities}}`，key = 6 个策略名。由 JSONL 增量扫描得出，event_id 去重。
`session_strategy_counts`: `{strategy: {evaluations, accepts, rejections}}`，来自 `shadow-health.json` 的当前引擎 session。

### 1.5 `strategy_latest` / `strategy_recent`

- `strategy_latest`: `{strategy: 最新一条 shadow_eval 事件（完整审计字段，见 §1.11）}`，策略无事件时缺省。
- `strategy_recent`: `{strategy: {by_asset: {asset: count}, rejection_reasons: {reason: count}}}`，基于最近 1000+1000 行事件窗口。

### 1.6 `current_pair` —— paired_lock 最新评估成本链（**重写核心**）

从最新 paired_lock 事件抽取（`pair_fields`，L842–852）。无事件时 `{}`，所有字段前端须 N/A：

| 字段 | 含义 |
|---|---|
| `market_id` | 市场 ID |
| `up_vwap` / `down_vwap` | 双腿 VWAP |
| `gross_cost` | up+down 成本 |
| `up_fee` / `down_fee` | 双腿独立费用（官方舍入后） |
| `buffer` | 执行 buffer |
| `net_cost` | 总净成本 |
| `guaranteed_payout` | 保证兑付（等份额） |
| `locked_profit` | 锁定利润 |
| `expected_execution_value` | EEV（配置型压力模型，非历史概率） |
| `decision` / `reason` | ACCEPT/REJECT + 原因 |
| `sizing_mode` | 应为 `real_market_dynamic_v1` |
| `requested_max_size` / `dynamic_target_size` / `market_minimum_size` / `executable_depth_size` / `slippage_limited_size` / `capital_limited_size` | 动态 sizing 链 |
| `shadow_capital_usd` / `capital_budget_usd` / `dynamic_fee` / `dynamic_buffer` / `dynamic_all_in_cost` / `dynamic_all_in_price` / `dynamic_expected_profit` / `dynamic_maximum_loss` / `size_binding_constraint` | 动态成本与约束 |

注意：paired_lock 事件（C++ 侧审计）还含 `locked_roi`、`total_fees`、`up_depth_ok`/`down_depth_ok`、`leg_1_fill_probability`、`leg_2_fill_probability`、`time_between_legs_us`、`orphan_leg_loss`、`up_age_ms`/`down_age_ms`/`book_skew_ms`、`up_fill`/`down_fill`、`fok`、`books_synced`、`source_age_ms` 等（见 `_strategy_score` L553–583 引用），这些在 `strategy_latest.paired_lock` 与 `shadow_markets` 的原始事件中可用，但**未被 `current_pair` 白名单抽取**——重写时若需展示 LOCKED ROI / 双腿 fill probability，应改从 `strategy_latest.paired_lock` 读取（属真实审计字段，合规）。

### 1.7 `current_split_sell` / `split_sell_near_misses`

split_sell_lock 最新事件白名单字段（L863–871）：`up_sell_vwap, down_sell_vwap, combined_bid_vwap, gross_proceeds, up_fee, down_fee, total_fees, execution_buffer, net_proceeds, split_collateral_cost, locked_profit, locked_roi, observed_break_even_bid_sum, observed_profit_threshold_bid_sum, profit_threshold_shortfall, required_gross_improvement_per_share, required_gross_improvement_bps, expected_execution_value, decision, reason, market_id, asset, timeframe, window, seconds_to_close`。
`split_sell_near_misses`: 最多 8 条 `split_sell_profit_below_threshold` 近似机会。

### 1.8 `market_matrix` / `market_reference_states` —— 市场与 reference 状态

- `market_matrix`: `{asset: {interval: {count, markets[], reference_ready, reference_blocked}}}`，7 资产 × 4 周期全键存在。`markets[]` 每项 = live_markets 市场条目 + `slot`(current/next) + `reference`（同下）。
- `market_reference_states`: `{market_id: {market_id, asset, interval, settlement_source, fast_price, consensus_price, settlement_reference, fresh_exchange_source_count, fresh_usd_spot_source_count, cross_source_divergence_bps, reference_quorum_met, reference_state, reference_block_reason}}`。由 `reference_layer.reference_state_for_asset` 生成；`reference_state` ∈ `REFERENCE_READY` / `REFERENCE_BLOCKED`；quorum 未满足时 `reference_block_reason` ∈ `insufficient_reference_sources` / `required_usd_spot_source_unavailable` / `settlement_reference_unavailable` / `cross_source_divergence_exceeded`；价格字段可为 `null` → N/A。

### 1.9 `reference_prices` —— 逐源行情（venue-status.json + 服务端新鲜度修正）

| 字段 | 类型 | 含义 |
|---|---|---|
| `updated_at_ms` | int | 文件更新时间 |
| `stale` | bool | 文件年龄 > 10s |
| `assets` | {asset: {...}} | 见下 |
| `engine_latency_us` | float\|null | 引擎处理耗时（单位 **us**，processing_time，非网络延迟） |
| `binance_btcusdt` / `chainlink_btcusd` / `divergence_usd` / `divergence_bps` | 顶层汇总，stale 时置 `null` | 遗留汇总字段 |

`assets.{asset}`：`supported`、`binance`/`chainlink`（价格，非 FRESH 时置 null）、`binance_status`/`chainlink_status`（FRESH/STALE/DISCONNECTED/NOT_RECEIVED/UNSUPPORTED/OUTLIER）、`binance_stale`/`chainlink_stale`、`divergence_bps`（缺一源时为 null）、`sources`。
`assets.{asset}.sources.{source_name}`：`{symbol, market_type, quote_currency, price, bid, ask, source_timestamp, received_at, message_age_ms, status}`——**这是逐源展示（含 coinbase/kraken/bybit/okx）的正确数据源**；非 FRESH 时 `price` 置 `null`，只显示状态（禁止无价格显示 STALE 之外的误导值；NOT_RECEIVED 语义由生产者保证）。

### 1.10 `shadow_health` / `engine_session` / `shadow_execution`

`shadow_health`（shadow-health.json + 服务端补充）：`updated_at, age_seconds, stale(>5s), ws_connected, ws_session_id, subscription_generation, ready_markets, waiting_up_snapshot, waiting_down_snapshot, full_resyncs, resyncs(int), run_id, engine_started_at, session_strategy_counts, paired_config_hash, split_sell_config_hash, maker_config_hash, inventory_config_hash, reference_connected, reference_protocol_errors, strategy_audit_queue, strategy_audit_backpressure, reference_ipc_receive_age_ms_p95, reference_ipc_receive_age_samples, clob_to_strategy_evaluation_us_p95, clob_to_strategy_evaluation_samples, maker_*` 计数。键缺失时前端 N/A。
`engine_session`: `{run_id, started_at, age_seconds, strategy_counts, evaluations}`。
`shadow_execution`：`{state(IDLE…), processed[], audit_offset, real_order_submissions, real_orders, real_fills}`——后三者**必须恒为 0**（缺失时为 `null`，前端显示 N/A 而非 0 以外的假设）。

### 1.11 `events` / `shadow_markets` / `signals` / `snapshot`

- `events`: 最近 100 条合并审计事件（shadow-audit.jsonl + strategy-audit.jsonl，按 ts 倒序，过滤未来事件）。方向/彩票事件字段见 `ev_strategies.decision_audit`（L215–263）：`event_id, event_type, strategy, market_id, condition_id, asset, timeframe, window, generation, session, evaluation_sequence, outcome, market_price, expected_fill_price, estimated_probability, market_implied_probability, gross_edge, fees, slippage, latency_risk_buffer, settlement_risk_buffer, model_uncertainty_buffer, execution_risk_buffer, net_ev, fast_price, consensus_price, settlement_reference, fresh_exchange_source_count, fresh_usd_spot_source_count, cross_source_divergence_bps, reference_quorum_met, reference_state, settlement_source, settlement_source_verified, reference_source_statuses[{source,symbol,market_type,quote_currency,price,effective_age_ms,status,role,accepted_for_quorum,rejection_reason}], valid_reference_sources, rejected_reference_sources, reference_price, price_to_beat, distance_to_price_to_beat, seconds_to_close, book_age_ms, reference_age_ms, clock_skew_ms, liquidity, minimum_liquidity, target_depth_ok, momentum_bps_30s, order_book_imbalance, confidence, decision, reason, blocking_reasons[], real_order_submissions=0, real_orders=0`。
- `shadow_markets`: 每个已发现市场的最新 paired_lock 事件列表。
- `signals` / `snapshot`: 旧版 signal 快照（live_snapshot.json），仅在 market_ids 内保留。

### 1.12 其余顶层字段

| 字段 | 含义 |
|---|---|
| `shadow_lifecycle` | `{open_positions, historical_open_positions_excluded, active_positions, settlement_pending, orphaned_positions, positions[], complete_set_inventory[], inventory_cohorts{current,legacy,unknown:{positions,cost,quantity,maximum_loss,next_close_seconds}}, maker_quotes[], completed_ids, historical_completed_excluded, pending_predictions, completed_predictions, portfolio_rejections, current_risk_halts, would_halt_reasons, calibration_bypasses, calibration_mode, portfolio_limits_enforced, risk_mode, portfolio_limits, config_version, config_hash, real_order_submissions, real_orders, real_fills}` |
| `probability_calibration` | `{late_window_directional_ev, low_price_lottery_ev}` 各含 `{samples, expected_up_rate, realized_up_rate, brier_score, log_loss, origin_accepted, origin_rejected, calibration_buckets{bucket:{samples,expected_up_rate,realized_up_rate}}}`；样本 0 时比率为 null → N/A。语义 CALIBRATION ONLY |
| `probability_observations` | `{pending, settled, orphaned, by_strategy{...{pending,settled}}, semantics:"CALIBRATION_ONLY_NOT_ORDERS_OR_PNL"}` |
| `arbitrage_research` | `{funnels{paired_lock,split_sell_lock,maker_complete_set_arb: {evaluations, depth_passed, fee_passed, latency_survived, independent_episodes, shadow_attempts, leg_1_book_executable, both_legs_book_executable, orphaned, invalidated, completed, positive_completed}}, repeatable_patterns[], counterfactual_patterns[], semantics:"RESEARCH_ONLY_NOT_ORDERS_OR_PNL", no_repeatable_arbitrage, conclusion}` |
| `clob_readiness` | `{discovered_markets, paired_markets_ready, not_ready, waiting_up_snapshot, waiting_down_snapshot}`；不变量：ready+not_ready=discovered |
| `dynamic_sizing` | `{active_positions, active_capital_usd, maximum_loss_usd, invalid_active_positions, invalid_active_position_reasons{}, invalid_active_position_details[], semantics:"REAL_MARKET_BOOK_SIZED_SHADOW_NOT_ORDERS"}` |
| `strategy_score` | `{total(0-100, blocked 时 0), blocked, components{eev,depth,freshness,book_skew,leg_risk}, metrics{expected_execution_value,depth_ratio,source_age_ms,book_skew_ms,leg_1/2_fill_probability}, checks{depth,freshness,book_sync,leg_risk,net_cost: PASS/FAIL/N/A}}`。**注意：这只是 paired_lock 最新事件的组合分，禁止当作三策略通用分（§16）** |
| `pipeline_steps` | `{ingest, clob_snap, replay, backtest, validate, approve, deploy}` ∈ PASS/BLOCKED/N/A；`deploy` 恒 BLOCKED；decision=REJECT 时 validate/approve=BLOCKED |
| `latency_rankings` | `{polymarket, binance, coinbase, kraken, chainlink, engine}` 各 `{latest, p50, p95, p99, samples, unit, metric}`。metric=`message_age`（引擎为 `processing_time`）——**UI 只能标 MESSAGE AGE / PROCESSING TIME，禁止标 LATENCY** |
| `latency_ms` | `{polymarket, binance, chainlink, engine}` 全 `null` 占位——**不可用作 latency 展示** |
| `rejection_reasons` | 报告级（或事件窗口）拒绝分布 {reason: count} |
| `blocked_reasons` | 旧 signal 层阻塞原因分布 |
| `risk_limits` | `{max_seconds_to_close, min_liquidity}` |
| `sources` | `{polymarket_clob:"configured", binance:"configured", chainlink:"validation-only"}` |
| `pnl_meter` | `{simulated_pnl, realized_pnl:0.0}` |
| `asset_latest_pnl` | 7 资产键，null → N/A |
| `equity_curve` / `trade_ledger` / `performance` / `performance_by_strategy` | 报告字段的顶层别名 |

### 1.13 shadow-acceptance 不变量（`shadow_acceptance.evaluate_status`，前端可复现展示）

23 项检查：analytics_ready、market_data_present、clob_websocket_connected、reference_ipc_connected、market_health_fresh、reference_protocol_integrity、strategy_audit_no_backpressure、low_latency_observed（p95 样本存在）、low_latency_budget（reference_ipc p95 ≤ 50ms 且 clob→strategy p95 ≤ 5000us）、websocket_stability_budget（WS reconnect ≤12/h、resync ≤60/h，引擎运行 ≥300s 后生效）、audit_data_present、market_readiness（ready+not_ready=discovered）、evaluation_decisions（accepted+rejected=evaluations）、evaluation_reasons（Σreasons=rejected）、real_execution_disabled（executed_orders=0 且 execution/lifecycle 三个 real 计数恒 0）、probability_observation_integrity、arbitrage_book_evidence_integrity、dynamic_sizing_integrity、event_deduplication（duplicates=0）、three_strategy_evaluations、three_strategy_decisions、probability_models_evaluated、complete_set_strategies_evaluated。结果 PASS / INCOMPLETE / FAIL（退出码 0/2/1）。

---

## 2. 现有 `web/index.html` 的轮询与渲染机制

- **端点**：唯一数据端点 `GET /api/status`（`web_monitor.py` L1205–1207，no-store）；`/` 与静态文件直接由 `web/` 目录伺服；无 WebSocket、无 SSE、无 POST。
- **轮询**：`refresh()` 用 `fetch('/api/status')`，`setInterval(refresh, 2000)`（**2 秒**）；失败时仅把引擎状态置 `OFFLINE / SHADOW`，其它面板保留旧值。
- **JS 结构**：单文件无框架。工具函数 `$ / fmt / money / esc / duration`；`equity()` 用 SVG polyline 画 `equity_curve`；`sourceState()` 渲染单个 reference 源（价格+message_age+状态）；`strategyCard()` 渲染策略卡（directional/lottery/paired/reversion 四卡）；`renderProbabilityCalibration()`、`renderArbitrageResearch()`、`render(d)` 主渲染（直接 `innerHTML` 拼接，按 id 定点更新约 20 个节点）。
- **布局**：CSS grid 12 个 panel 区：profile（PnL 大数字+win rate+Sharpe）、command（paired_lock 成本链+score checks）、matrix（资产×周期）、strategies（4 策略卡+7 步 pipeline）、probability（校准）、equity（SVG 曲线）、arb（研究漏斗+near-miss+patterns+counterfactuals）、replay（trade ledger）、references（4 源×7 资产）、rejections（条形图）、health（19 行 readiness/health）、pnl（2 条 meter）、latency（message age 条形）。
- **已知缺口**（重写时修正）：references 面板硬编码只渲染 binance/coinbase/kraken/chainlink 四列，未用 `sources{}` 通用结构（bybit/okx 不可见）；`current_pair` 缺 locked_roi/双腿 fill probability（需从 `strategy_latest.paired_lock` 取）；无方向/彩票最新事件的完整字段面板（outcome、estimated_probability、price_to_beat、distance、net_ev 分解只在 strategy 卡缩略显示）；无 shadow-acceptance 检查清单面板；无 market_reference_states 逐市场 quorum 展示。

---

## 3. 参考图面板逐个判定

参考图 "CLAUDE QUANT" 面板 → 真实数据支撑判定（✅ 有 / 🟡 部分有 / ❌ 无）。

| # | 参考图面板 | 判定 | 对应真实字段 / 缺口 |
|---|---|---|---|
| 1 | 顶部 header（品牌、MARKOV/KELLY/RL tabs、FIT/DARK 按钮、时钟） | 🟡 | 时钟可用 `ts`；品牌可保留视觉。MARKOV/KELLY/RL/LEARN 标签无任何后端对应 → 只能做纯装饰性静态文字或删除；不得暗示功能存在 |
| 2 | ticker 行情条（BTC/DOGE/XRP/ETH 滚动价格+涨跌） | 🟡 | 可用 `reference_prices.assets.{asset}.sources.*.price/status` 做逐资产 ticker；**涨跌幅（+/-%）无真实字段** → 不显示涨跌百分比，只显示价格+状态 |
| 3 | "al-mach VERIFIED / POLYGON ON-CHAIN" 身份卡 | ❌ | 无对应真实概念；`VERIFIED` 属 §16 禁止的无依据标签。替换为 `engine_session.run_id` + `system_status` + SHADOW MODE 标签 |
| 4 | PnL 大数字 `$78,492` + `+$1,868 WIN` + SETTLED | 🟡 | `performance.simulated_pnl`（completed=0 时 N/A）。✘ "WIN/SETTLED +2,181%" 无字段 → 不显示。可显示 `performance.completed/wins/losses`。必须标注 SIMULATED |
| 5 | TRADES 25,515 / WIN RATE 46.8% / AVG R:R 3.92 / SHARPE 4.21 | 🟡 | TRADES→`performance.completed`；WIN RATE→`performance.win_rate`（null→N/A）；SHARPE→`performance.sharpe`(+`sharpe_samples`)；**AVG R:R 无字段** → N/A 或删除 |
| 6 | EQUITY CURVE 折线（910→…） | 🟡 | `equity_curve`（SVG 可复用现有 `equity()`）。语义=**模拟 completed 交易**，空时显示空态文案；禁止美化成真实资金曲线 |
| 7 | LIQ RISK 0.5/10 + `SAFE` 徽章 | ❌ | 无清算风险概念；`SAFE` 属 §16 明令禁止。替换为 `dynamic_sizing.maximum_loss_usd` / `active_capital_usd` 等真实风险敞口数字 |
| 8 | K 线图 BTC/USD + 右侧订单簿档位梯 | ❌ | 无 OHLC/K 线数据，无档位数据出口（`build_status` 不含 order book levels）。**必须砍掉**；可替代为 `latency_rankings` 的 MESSAGE AGE 面板或 reference 逐源表 |
| 9 | 执行流程步骤条 Scan→Detect→Validate→Size→Fill→Settle | 🟡 | 有 `pipeline_steps{ingest,clob_snap,replay,backtest,validate,approve,deploy}` 七步真实状态（PASS/BLOCKED/N/A，deploy 恒 BLOCKED）。可**复刻步骤条视觉**但步骤名/状态必须映射到 pipeline_steps 的真实七步，不得编造 Fill/Settle 完成态 |
| 10 | KELLY SIZER（$6,558 / FULL 32.2% / EDGE +2.2I） | 🟡 | 无 Kelly 公式输出；但 paired_lock 有真实 sizing 链：`current_pair.dynamic_target_size / capital_budget_usd / dynamic_maximum_loss / size_binding_constraint / market_minimum_size`。可做成"POSITION SIZER (REAL MARKET BOOK SIZED)"，**禁止叫 Kelly**、禁止显示 % of bankroll 这类无依据数字 |
| 11 | LIVE REGIME PROBABILITY 流图 + TREND 14% / CHOP 35% / PANIC 51% + REGIME FLIP | ❌ | 无 regime 分类器、无概率时间序列。**砍掉**。可替代为 `rejection_reasons` 分布或 `probability_calibration` 校准桶图（真实） |
| 12 | POLYMARKET TRADE LOG（右侧滚动成交记录） | ✅ | `trade_ledger`（market_id/ts/pnl/strategy，空时 "NO CURRENT CONFIG COMPLETIONS"）；另可用 `events[:100]` 做评估流水 |
| 13 | ALPHA RADAR 雷达图 | ❌ | 无任何 alpha 因子分数。**砍掉** |
| 14 | SCORING ENGINE / IC OVER 30 WINDOWS + IC=0.40 公式 | 🟡 | 无 IC/因子相关性。但 `strategy_score{total,components{eev,depth,freshness,book_skew,leg_risk},checks}` 是真实 paired_lock 评分 → 可复刻"评分引擎"视觉展示这 5 个 component 与 checks；大数字 IC→`strategy_score.total`（blocked 时 0 并标注 BLOCKED）。仅限 paired_lock，不得泛化为三策略通用分 |
| 15 | TRANSITION MATRIX 3×3（.86/.09/.05 …） | ❌ | 无状态转移统计。**砍掉** |
| 16 | THE LOOP（BACKTEST→REFINE→SCORE 无限环图 + REV 57） | ❌ | 无回测循环计数；`pipeline_steps.backtest` 恒 N/A。**砍掉** |
| 17 | REGIME CHORD 弦图（TREND/CHOP/PANIC 流） | ❌ | 无 regime 数据。**砍掉** |
| 18 | 底部统计条（GATE PASS 1,235 / REV 57 / FILLED 14 …） | 🟡 | 可用真实计数重组：`counts.*`（evaluations/accepts/fok_passed）、`clob_readiness.*`、`shadow_health.resyncs`、`duplicate_events`、`real_orders=0` |
| 19 | 右上 L2 CHAINLINK / 深度数字列 | 🟡 | Chainlink 逐源状态有（`sources.chainlink`）；L2 深度无 → 只显示 Chainlink price/bid/ask/age/status |
| 20 | HOLD 4 BARS / NEXT CHOP / EDGE +0.9% 等预测标签 | ❌ | 无预测 horizon 数据；方向策略的 `estimated_probability` 属模型估计，可用于方向面板但**禁止包装成 regime 预测** |

---

## 4. 建议保留面板清单与真实字段绑定

按参考图视觉结构复刻，但内容全部换成以下真实面板（建议布局：顶部 header/ticker → PnL 大数字+equity → 三策略分区 → paired_lock 成本链 → reference 逐源 → readiness/acceptance → 底部统计条）。

### P1. Header 状态条（对应参考图 #1/#2）
- `system_status`（+SHADOW 标签）、`ts` 时钟、`engine_session.run_id/age_seconds`、`analytics_status`
- headstat 计数：`clob_readiness.discovered_markets`、`counts.session_strategy_evaluations`、`counts.shadow_accepts`、`shadow_report.duplicate_events`、`counts.executed_orders`（恒 0）
- ticker：逐资产 `reference_prices.assets.{a}.sources` 最优 FRESH 价格 + 状态（无价格显示状态文本，禁止涨跌%）

### P2. SIMULATED PnL 大数字（对应 #4/#5/#6）
- 大数字 `performance.simulated_pnl`（null→N/A，标注 SIMULATED / CURRENT CONFIG）
- 副指标：`performance.completed / wins / losses / win_rate / sharpe(+sharpe_samples)`、REAL PNL `$0.00`、REAL ORDERS `0`
- equity SVG：`equity_curve`（空→空态文案）
- 分策略小表：`performance_by_strategy.{三策略}.{completed,simulated_pnl,win_rate}`

### P3. 三策略分区卡（§16 强制，对应 #14 的视觉位）
每卡绑定：`strategy_counts.{s}.{evaluations,accepts,rejections,unique_opportunities,active_opportunities}`、`session_strategy_counts.{s}`、`strategy_latest.{s}.{decision,reason}`、`strategy_recent.{s}.rejection_reasons`。
- **DIRECTIONAL EV** 卡增加（`strategy_latest.late_window_directional_ev`）：`outcome, estimated_probability, market_price, expected_fill_price, gross_edge, fees, slippage, latency_risk_buffer+settlement_risk_buffer, net_ev, seconds_to_close, reference_price, price_to_beat, distance_to_price_to_beat, reference_quorum_met, reference_state, decision, reason`（§16 directional 展示清单全覆盖）
- **LOW-PRICE LOTTERY** 卡：`outcome, expected_fill_price, estimated_probability, net_ev, fees, slippage, model_uncertainty_buffer+execution_risk_buffer, decision, reason` + `probability_calibration.low_price_lottery_ev.{samples,brier_score,calibration_buckets}`；样本 0 时 WIN RATE/SHARPE/PNL=N/A（§6）
- **PAIRED LOCK** 卡：见 P4

### P4. PAIRED LOCK 成本链面板（§7/§16 强制，对应 #10/#14 视觉位）
- 主行（`current_pair`）：UP VWAP `up_vwap`、DOWN VWAP `down_vwap`、GROSS COST `gross_cost`、UP FEE `up_fee`、DOWN FEE `down_fee`、TOTAL FEES（up+down）、EXECUTION BUFFER `buffer`、NET COST `net_cost`、GUARANTEED PAYOUT `guaranteed_payout`、LOCKED PROFIT `locked_profit`、EEV `expected_execution_value`、DECISION/REASON
- LOCKED ROI：从 `strategy_latest.paired_lock.locked_roi` 取（current_pair 未含，属真实审计字段）
- 执行压力（标注"配置型压力模型"）：`strategy_latest.paired_lock.{leg_1_fill_probability, leg_2_fill_probability, time_between_legs_us, orphan_leg_loss}`
- 检查格：`strategy_score.checks{depth,freshness,book_sync,leg_risk,net_cost}` + `strategy_score.components`（REJECT 时 DEPTH/FRESHNESS/BOOK SYNC/LEG RISK 仍显示真实状态，VALIDATE/APPROVE/DEPLOY 显示 BLOCKED）
- sizing 链：`dynamic_target_size / market_minimum_size / capital_budget_usd / dynamic_maximum_loss / size_binding_constraint`（命名 POSITION SIZER，禁止 Kelly）
- 本面板内若出现任何外部交易所价格，必须标注 **REFERENCE ONLY / NOT USED FOR PAIRED-LOCK ACCEPTANCE**

### P5. 执行流程步骤条（对应 #9）
- 复刻步骤条视觉，绑定 `pipeline_steps` 七步真实状态：INGEST / CLOB SNAP / REPLAY / BACKTEST(N/A) / VALIDATE / APPROVE / DEPLOY(恒 BLOCKED)；decision=REJECT 时 VALIDATE/APPROVE=BLOCKED（§16）

### P6. REFERENCE 逐源状态面板（§4.5/§16，对应 #8/#19 视觉位）
- 表：资产 × 源，遍历 `reference_prices.assets.{a}.sources`（**动态列，含 binance/coinbase/kraken/bybit/okx/chainlink**，不硬编码四列）：`symbol, market_type, quote_currency, price, bid/ask, message_age_ms, status`（FRESH/STALE/DISCONNECTED/NOT RECEIVED/UNSUPPORTED/OUTLIER）
- 聚合行（每市场或选中市场，取 `market_reference_states.{id}`）：FAST PRICE / CONSENSUS PRICE / SETTLEMENT REFERENCE / FRESH SOURCES `fresh_exchange_source_count` / USD SPOT `fresh_usd_spot_source_count` / DIVERGENCE `cross_source_divergence_bps` / QUORUM `reference_quorum_met` / STATE `reference_state` + `reference_block_reason`
- MESSAGE AGE 条形：`latency_rankings`（标注 MESSAGE AGE / PROCESSING TIME，禁止 LATENCY 字样；null→N/A）

### P7. 市场 READINESS + 资产×周期矩阵（§18）
- `clob_readiness.{discovered_markets, paired_markets_ready, not_ready, waiting_up_snapshot, waiting_down_snapshot}` + 恒等式提示 ready+not_ready=discovered
- `market_matrix` 7×4 网格：count、reference_ready/reference_blocked；`asset_latest_pnl` 最近模拟 PnL 列

### P8. SHADOW-ACCEPTANCE 验收面板（§17，新增）
- 复现 `shadow_acceptance.evaluate_status` 关键不变量为检查格：`accepted+rejected=evaluations`、`Σreasons=rejected`、`ready+not_ready=discovered`、`real_orders=0 / real_submissions=0 / real_fills=0`、`duplicate_events`、`analytics_status`、latency p95（`shadow_health.reference_ipc_receive_age_ms_p95 / clob_to_strategy_evaluation_us_p95`）、WS 稳定（`ws_session_id / full_resyncs`）；空状态（0 市场/0 评估）时明确显示 INCOMPLETE 而非 PASS

### P9. 研究/审计面板（保留现有，对应 #12 扩展）
- `arbitrage_research.funnels/conclusion`（标注 RESEARCH ONLY）、`split_sell_near_misses`、`repeatable_patterns`、`counterfactual_patterns`
- `trade_ledger` 台账 + `events` 评估流水（trade log 视觉位）
- `probability_calibration` 校准面板（标注 CALIBRATION ONLY）
- `shadow_lifecycle`：positions/inventory_cohorts/risk_halts/portfolio_rejections

### P10. 底部统计条（对应 #18）
- `counts.{shadow_evaluations, shadow_accepts, fok_passed, unique_opportunities, active_opportunities}`、`shadow_report.duplicate_events`、`shadow_health.resyncs`、`dynamic_sizing.{active_positions, active_capital_usd, maximum_loss_usd}`、REAL ORDERS 0 / REAL SUBMISSIONS 0

### 明确砍掉（❌ 无真实数据，§16 禁止）
K 线图与 L2 档位梯、regime 概率流图/弦图/transition matrix、alpha radar、Kelly sizer（改名换义）、THE LOOP 回测环、LIQ RISK/SAFE 徽章、VERIFIED 徽章、涨跌百分比、AVG R:R、任何"历史胜率/成交概率"措辞（EEV/fill probability 一律标注"配置型压力模型"）。
