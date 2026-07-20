# Dashboard UI 视觉规范 — "CLAUDE QUANT" 终端风量化仪表盘复刻

> 来源参考图：`be5d82db-336c-4f7b-883c-6c2de42087c1.png`（881 × 1062 px，竖向密集仪表盘）。
> 本文档颗粒度对标"前端工程师可直接照写 CSS"。所有色值均从原图像素采样聚类得出。
>
> **项目硬约束（AGENTS.md §16，重写 `web/index.html` 时必须同时满足）：**
> 只展示 canonical audit / health state 真实数据；禁止假订单、假 PnL 曲线、假 equity curve、假 latency bar；
> 未知值一律显示 `N/A`；三策略分区展示；paired_lock 展示完整成本链；
> paired_lock 区域的外部交易所行情必须标注 `REFERENCE ONLY / NOT USED FOR PAIRED-LOCK ACCEPTANCE`；
> 没有端到端延迟测量时只显示 `MESSAGE AGE`，不显示 `LATENCY`。
> 本规范只定义**视觉语言**，数据绑定语义以 AGENTS.md 为准。

---

## 1. 精确色板

### 1.1 基底与中性色

| 用途 | 色值 | 说明 |
|---|---|---|
| 页面背景 | `#F0F2F4` | 冷调浅灰（顶栏通条、面板间隙"墙体"色） |
| 面板主体底色 | `#FDFDFC` | 微暖近白，所有卡片内部 |
| 面板次级底色（表头行/内嵌条） | `#F0F2F6` | 比页面背景略偏蓝 |
| 悬停/高亮行底 | `#F8FBFA` | 极浅绿白 |
| 边框（主） | `#000000` | **纯黑 1px**，全站统一 |
| 硬投影 | `#000000` | `box-shadow: 3px 3px 0 #000`，无模糊 |
| 主文字 | `#111113`（近黑；纯黑边框以外的正文可用 `#1A1B1E`） | 标题、大数字以外的正文 |
| 次级文字灰 | `#6E7276` | 小标签、单位、时间戳 |
| 弱提示灰 | `#9BA0A4` | 占位、禁用态 |
| 底部状态条 | 底色 `#A2A2A2`（深灰带），文字 `#000000` / 白 | 见 §6 |

### 1.2 强调色（像素采样聚类结果）

| 语义 | 色值 | 采样代表 | 用途 |
|---|---|---|---|
| 主绿（positive / 盈利 / TREND / 多头） | **`#0B9470`** | rgb(11,148,112) | 大数字、上涨 K 线、YES chip、TREND 带、ask 侧深度 |
| 主绿浅填充 | `#87C6B8` / `#C6E2E0` | 订单簿 bid 行底、进度条轨道填充 | 面积图浅色区、表格行底色 |
| 主红（negative / 亏损 / PANIC / 跌） | **`#E80A19`** | rgb(232,10,25) | LIVE 灯、下跌 K 线、PANIC 带、NO chip 边框 |
| 红浅填充 | `#EC828D` / `#F8D7D7` | 订单簿 ask 行底、矩阵红色单元格底 | 面积图浅色区、chip 底 |
| 主橙（warning / CHOP / 中性关注） | **`#E86D09`** | rgb(232,109,9) | SHARPE 数字、CHOP 带、橙色矩阵单元 |
| 橙浅填充 | `#EBBB8D` | 步骤条激活态底色（米色-浅橙） | 激活步骤、警示 chip 底 |
| 紫色（品牌 / 模式标识） | 深 `#5927B7`、浅填充 `#CDBCFF` / `#E2D1FF` | BANKROLL 按钮、∞ 环路图 | 品牌 chip、模式切换、装饰弧 |
| 米黄高亮 | `#FCF6D1` / `#FFF7DD` | 步骤条当前步骤底、注释标签底 | 当前步骤、内联标注 |
| 青色辅助 | `#0B9472`（与主绿同族，略偏青） | K 线涨色、雷达图扫描线 | 与主绿可互换，保持单色绿族 |

> 规则：**全站只有 4 个强调色族**（绿 / 红 / 橙 / 紫）。蓝、黄、品红只作极浅填充出现，禁止引入新强调色。

### 1.3 语义映射（本项目）

- ACCEPT / net_ev > 0 / locked_profit > 0 / FRESH / READY → 主绿 `#0B9470`
- REJECT（高严重度）/ 亏损 / STALE-DISCONNECTED / NOT_READY → 主红 `#E80A19`
- DEGRADED / buffer / 费用 / 中性统计（Sharpe、命中率）→ 主橙 `#E86D09`
- SHADOW / DRY RUN 模式标识、品牌元素 → 紫 `#5927B7`
- `N/A` → 弱提示灰 `#9BA0A4`

---

## 2. 字体风格

### 2.1 字族

```css
--font-mono: "JetBrains Mono", "IBM Plex Mono", "SFMono-Regular", Consolas, monospace;
--font-display: "Archivo Black", "Space Grotesk", "Inter", system-ui, sans-serif;
```

- **一切标签、表头、ticker、时间戳、chip、按钮**：等宽字体 + 全大写 + 加字距。
- **大数字（PnL、价格、百分比）**：超粗无衬线 display 字体（视觉上接近 Archivo Black / Helvetica Now Display Black），字距微负。
- 数字本身（表格数值、ticker 数值）用等宽字体保证列对齐。

### 2.2 字号层级（以 881px 宽参考图为基准，按 13px 根字号折算）

| 层级 | 规格 | 用途 |
|---|---|---|
| Hero 大数字 | `font: 800 44px/1.0 var(--font-display); letter-spacing: -0.02em;` | `$78,492`、主 PnL、主价格 |
| 次级大数字 | `700 20-24px var(--font-display)` | 面板内 KPI（`46.0%`、`3.92`、`0.40`） |
| 面板标题 | `700 10px var(--font-mono); letter-spacing: 0.14em; text-transform: uppercase;` | `EXECUTION CYCLE · #2 489` |
| 区块标签 / chip | `600 9px var(--font-mono); letter-spacing: 0.10em; uppercase` | `VERIFIED`、`LIVE`、`REFERENCE ONLY` |
| 表格正文 / ticker | `500 10-11px var(--font-mono)` | trade log 行、ticker 条目 |
| 微标签（轴、单位、时间戳） | `500 8-9px var(--font-mono); color: #6E7276; letter-spacing: 0.06em` | 坐标轴、`12MS`、UTC |
| 大数字下的小标题 | `600 8px var(--font-mono); uppercase; color:#6E7276; letter-spacing:0.12em` | `TRADES`、`WIN RATE` |

### 2.3 排版通则

- 标签一律 `text-transform: uppercase` + 正字距（0.08–0.14em），营造"终端打印"感。
- 大数字用 `letter-spacing: -0.01em ~ -0.02em` 收紧。
- 数字使用 `font-variant-numeric: tabular-nums`。
- 分隔符大量使用 `·`（中点）连接标题与元信息：`PANEL NAME · META · META`。
- 正负号显式：`+$1,868 WIN`、`-0.02%`，正绿负红。

---

## 3. 面板视觉语言（Neobrutalist / 打印终端风）

```css
.panel {
  background: #FDFDFC;
  border: 1px solid #000;
  border-radius: 0;            /* 禁止圆角 */
  box-shadow: 3px 3px 0 #000;  /* 硬偏移投影，无 blur */
  padding: 8px 10px;
}
```

### 3.1 标题栏

- 面板**没有独立色块标题栏**；标题就是面板内第一行文字：
  - 左侧：小方块符号 `▪` 或图标 + `面板名 · 元信息 · 元信息`（10px mono uppercase）。
  - 右侧：右对齐 chip 或元数值（如 `FILL 0.73%`、`SM CRYPTO`）。
- 标题行下方常有 `1px dashed #00000022` 或细实线分隔。
- 面板四角外侧有**裁切角标（crop marks）**：四角各一个 6×6px 的黑色直角 `⌐ ¬` 描边装饰（可用 `::before/::after` 或四角绝对定位 span 实现）。这是该设计最强识别特征之一。

### 3.2 Chip / 角标样式

```css
.chip {
  font: 600 9px var(--font-mono); letter-spacing: 0.10em; text-transform: uppercase;
  padding: 1px 5px; border: 1px solid currentColor; border-radius: 0;
  background: transparent;
}
.chip--filled-green { background: #0B9470; color: #fff; border-color: #0B9470; }
.chip--outline-green { color: #0B9470; background: #F0FAF6; }
.chip--filled-red   { background: #E80A19; color: #fff; border-color: #E80A19; }
.chip--outline-purple { color: #5927B7; background: #F4EFFF; }
```

- `VERIFIED`：绿色描边 chip，前置小绿点 `●`（6px 实心圆，带 1.5px 浅色光晕）。
- `LIVE`：**红底白字**实心 chip，前可带闪烁红点。
- `BANKROLL`（模式按钮）：紫描边、浅紫底 `#F4EFFF`。
- YES / NO（trade log）：YES = 绿描边小 chip；NO = 红描边小 chip，宽约 26px 居中对齐。

### 3.3 分区虚线

- 面板内部垂直/水平分区用 `1px dashed rgba(0,0,0,0.25)`。
- 表格行分隔用 `1px solid rgba(0,0,0,0.06)` 极浅实线。

### 3.4 状态灯

- 6px 实心圆点 + 同色浅底圆（12px，opacity 0.2），行内与 9px uppercase 标签并排。
- 状态色：FRESH/READY=绿，STALE=橙，DISCONNECTED/NOT_RECEIVED=红，UNSUPPORTED/OUTLIER=灰。

---

## 4. 整体布局网格

画布 881×1062，外边距约 4px，面板间距（gutter）约 6px。全部相对坐标（x, y, w, h，占整图比例）：

| 区域 | x | y | w | h | 内容 |
|---|---|---|---|---|---|
| 顶栏 | 0 | 0 | 100% | 1.7% | 品牌 + 模式 chips + 时钟 |
| 排行条 | 0 | 1.9% | 100% | 1.6% | `#1 GLOBAL · TOP 0.01% · BEATING …` |
| Ticker 条 | 0 | 3.7% | 100% | 1.8% | 滚动行情带（LIVE 红标开头） |
| 面板 A（PnL） | 0.5% | 6% | 53% | 21.5% | 大数字 PnL + KPI 行 + equity sparkline + risk 条 |
| 面板 B（K线+订单簿） | 54% | 6% | 45.5% | 21.5% | 左 2/3 K 线，右 1/3 L2 订单簿 |
| 面板 C（执行步骤条） | 0.5% | 28% | 63% | 6.5% | 6 步 chevron 流程 |
| 面板 D（Kelly） | 64.5% | 28% | 35% | 6.5% | 环形进度 + 三个数值 |
| 面板 E（regime 堆叠图） | 0.5% | 35% | 63% | 15% | 100% 堆叠面积图 + 内嵌标签 |
| 面板 F（regime 统计） | 64.5%→ 内嵌于 E 右 | 35% | — | — | 实际是 E 内右列 3 个大百分比（TREND/CHOP/PANIC） |
| 面板 G（trade log） | 64.5% | 35% | 35% | 15% | 表格式成交日志 |
| 面板 H（雷达图） | 0.5% | 51% | 33% | 17.5% | 极坐标雷达 + 右侧图例 |
| 面板 I（评分引擎） | 34% | 51% | 30% | 17.5% | 柱状+线图 + 大数字 `0.40` + 公式块 |
| 面板 J（转移矩阵） | 64.5% | 51% | 35% | 17.5% | 3×3 彩色单元格矩阵 |
| 面板 K（The Loop ∞） | 0.5% | 69.5% | 50% | 27% | 无限环路流程图 + 左 REV LOG + 右柱状图 |
| 面板 L（Regime 和弦图） | 51% | 69.5% | 48.5% | 27% | 弦图 + 左右大数字 |
| 底部状态条 | 0 | 98% | 100% | 2% | 深灰通条：计数 + 迷你图例 + 页码 |

> 本项目映射建议（数据语义按 AGENTS.md，不改视觉）：
> - 面板 A → Shadow 全局统计（real_orders 恒为 0，completed=0 时显示 N/A）
> - 面板 B → 选中市场 CLOB 订单簿 + Price to Beat 标记
> - 面板 C → 三策略评估管线状态（DISCOVER → BOOKS READY → REFERENCE QUORUM → EVALUATE → DECISION → AUDIT）
> - 面板 G → audit 事件流（按 strategy 着色）
> - 面板 J → 三策略 ACCEPT/REJECT 计数矩阵
> - 新增三策略分区：**DIRECTIONAL EV / LOW-PRICE LOTTERY / PAIRED LOCK** 三个等宽面板带，paired_lock 带内为完整成本链表格（UP VWAP → … → EEV → DECISION/REASON），其参考行情区右上角固定紫 chip `REFERENCE ONLY`。

---

## 5. 面板内部排版模式

### 5.1 大数字 + 小标签（KPI 组）

```
[TRADES]        [WIN RATE]      [AVG R/R]     [SHARPE]
 25,515          46.0%           3.92          4.21
```
- 标签 8px 灰色 uppercase 在上，数值 20–24px display 在下。
- 各 KPI 列左对齐等距分布；正负着色（盈绿、Sharpe 用橙）。
- Hero 数字（`$78,492`）44px，右侧紧跟一个**结果 chip 卡**（白底黑边硬投影小卡：上排 `▲ +$1,868 WIN` 绿字，下排两行 8px 灰标签 `SETTLED · SOL UP 5M` / `+2,181%`）。

### 5.2 进度条 / 风险条

```css
.meter { height: 10px; border: 1px solid #000; background: #F0F2F6; border-radius: 0; }
.meter > i { display: block; height: 100%; background: #0B9470; } /* 红/橙按语义 */
```
- 条旁右侧放状态 chip（如绿描边 `SAFE`）。本项目改为真实状态词（`READY` / `DEGRADED` / `BLOCKED`），禁止无依据的 `SAFE`。
- 迷你分段条（底部状态条内）：3–5 个 4×10px 色块并排（绿/橙/红），表示分类占比。

### 5.3 表格（trade log / audit 流）

- 无表头色块，首行即数据；行高约 18px。
- 列：`时间(灰 9px)` `YES/NO chip` `资产 周期(粗 10px)` `价格(右对齐)` `盈亏(右对齐, 正绿负红)`。
- 行分隔 `1px solid rgba(0,0,0,0.06)`；行 hover `#F8FBFA`。
- 末行（汇总行）上方加 1px 黑实线，数值加粗。

### 5.4 迷你图（sparkline / equity 曲线）

- 容器带 1px 黑边 + 内标签行（`EQUITY CURVE · 91D` + 右 chip）。
- 曲线：1.5px 主绿描边，线下 8% 透明绿填充；终点带 4px 圆点。
- 背景网格：极浅 `#00000008` 水平虚线 3–4 条。
- **禁止在没有真实 completed trades 时渲染任何曲线**——显示 `N/A` 灰字占位（AGENTS.md §12/§16）。

### 5.5 K 线 + 订单簿

- K 线：涨 `#0B9470`、跌 `#E80A19`，实体填充、影线 1px；右侧价格轴 8px 灰字。
- 当前价标签：米色底 `#FFF7DD` 黑边小旗标在轴上。
- 订单簿 L2：两列等宽数字（价格/数量），ask 行底 `#F8D7D7` 渐变深度条，bid 行底 `#C6E2E0`；中点价差行加深分隔。

### 5.6 步骤条（chevron 流程）

- 6 个连续箭头形（`clip-path: polygon()` 右尖角），灰白底 `#F0F2F6`；当前步骤米色底 `#FCF6D1` + 1px 黑边。
- 每步内部：上排橙色微小编号 `01`，下排 9px uppercase 步名；当前步名前加橙色小方块 `▪`。

### 5.7 彩色矩阵 / 大百分比单元格

- 单元格：浅填充底（绿 `#DDF0E8` / 橙 `#FBE3CC` / 红 `#F8D7D7`）+ 1px 黑边，硬投影可选。
- 格内：上方 9px 灰标签（`STAY`/`RARE`），下方 20px display 数字（`.86`），数字颜色取对应强调色。
- 行/列头：9px uppercase 灰，顶部列头带对应色 4px 顶边。

---

## 6. 顶栏、Ticker 与底部状态条

### 6.1 顶栏（高约 18px，底 `#F0F2F4`，底部 1px 黑线）

- 左：紫底白字方块 logo（16×16，`border-radius:3px`，此为全站唯一圆角元素）+ `CLAUDE QUANT` 11px 800 display + 其下 7px 灰字副标。
- 中：若干模式 chip——紫描边 `BANKROLL`、灰描边 `FIT`、黑底白字 `DARK`（深色实心小胶囊）。
- 右：绿点 + `LIVE · MAINNET` 9px 绿字；`12MS` 灰字（本项目改为 `MSG AGE xxMS`，无数据时 `N/A`）；大时钟 `10:55:11` 16px 800 display 黑字 + 右侧 `UTC` 微标 + `ROUND END 09:42` 7px 灰。

### 6.2 Ticker 条（高约 19px，白底，上下各 1px 黑线，横向滚动）

- 开头红底白字 `▼ LIVE` chip。
- 每条目：`+$1,418 · DOGE UP 5M` / `-$1,815 · XRP DN 5M`——数值 10px 700（正绿负红），资产标签 9px 灰，条目间以 `·` 或细竖线分隔。
- 本项目数据源：最近 audit ACCEPT/REJECT 事件滚动；无事件时显示 `NO EVENTS`，**禁止伪造**。

### 6.3 底部状态条（高约 21px，底 `#A2A2A2`，顶部 1px 黑线）

- 左侧大计数（`1,215` 黑 16px display + 下排 7px 灰标签）。
- 中部：左对齐 7px 灰说明文字 + 橙色大版本号 `REV 57`。
- 右侧：迷你分段色块图例（绿/橙/红 3 段小条 + 对应百分比）+ 页码 `14`。
- 本项目内容建议：evaluations 总数 / duplicate_events / real_orders=0 恒等式状态 / uptime。

---

## 7. 实现速查（CSS 变量）

```css
:root {
  --bg: #F0F2F4;          --panel: #FDFDFC;      --panel-2: #F0F2F6;
  --ink: #111113;         --ink-2: #6E7276;      --ink-3: #9BA0A4;
  --line: #000000;        --line-soft: rgba(0,0,0,.08);
  --green: #0B9470;       --green-soft: #C6E2E0; --green-bg: #F0FAF6;
  --red: #E80A19;         --red-soft: #F8D7D7;   --red-mid: #EC828D;
  --orange: #E86D09;      --orange-soft: #FBE3CC;
  --purple: #5927B7;      --purple-soft: #E2D1FF; --purple-bg: #F4EFFF;
  --cream: #FCF6D1;       --cream-2: #FFF7DD;
  --shadow: 3px 3px 0 #000;
  --font-mono: "JetBrains Mono", Consolas, monospace;
  --font-display: "Archivo Black", "Space Grotesk", sans-serif;
}
```

**全局禁令（视觉侧）：** 无圆角（logo 除外）、无模糊阴影、无渐变背景（订单簿深度条除外）、无第四种强调色、无 `SAFE`/`VERIFIED` 等无依据状态词、无装饰性假数据图表。
