# 港股 / 美股扩展可行性评估（2026-07-03）

- **目标**：系统评估 panel 各功能（个股分析、回测、监控、告警、盘口、实时行情、市场概览等）从"仅 A 股"扩展到**港股（HK）**和**美股（US）**的可行性。
- **方法**：逐功能排查 A 股硬编码/假设，核实 HK/US 数据源覆盖，按改造难度评级。
- **范围**：**只调研，不写代码**。评级口径：`低`=改配置/加分支即可、`中`=需适配逻辑或补数据、`高`=需新数据源或重写、`概念性不适用`=功能本身是 A 股独有概念。

---

## 一、结论摘要（TL;DR）

| 市场 | 总体可行性 | 一句话 |
|---|---|---|
| **港股（HK）** | **中等，多数功能可低-中成本适配** | 数据层已覆盖 HK 标的清单、TDX 日/分钟线、Sina/Tencent 实时；但 fstore 财务、港股指数成分、可用五档盘口仍是缺口。主要工作是**去 A 股硬编码**（涨跌停、交易时间、板块前缀）+ 补**港股前复权口径**。涨停梯队/情绪面等 A 股概念功能对 HK 不适用。 |
| **美股（US）** | **高成本，需独立立项** | 代码库**零美股痕迹**，`AssetType` 无 `us`，无任何美股数据源。需要引入全新数据源（日线/实时/财务/复权）、新交易时区（美东，含夏令时）、新符号体系（无数字代码），几乎是平行搭一套数据管道。 |

**建议**：港股作为**下一步增量**（复用现有数据源，逐功能去硬编码）；美股作为**独立项目**（先解决数据源，再谈功能移植）。

---

## 二、数据源可用性矩阵

| 能力 | A 股（现状） | 港股（HK） | 美股（US） |
|---|---|---|---|
| **标的清单 instruments** | ✅ fstore base_infos asset_type=1 | ✅ fstore `base_infos WHERE asset_type=3` 实测 2931 行，`max(ssdate)=2026-06-05`；`FQuantProvider(engine_mode="disk").get_instruments("hk")` 返回 2931 只，样本含 `00700.HK/09988.HK/02577.HK/06088.HK` | ❌ 无 |
| **日线 daily** | ✅ TDX wide/day + fstore | ✅ TDX 磁盘 HK 分桶（`hk00/hk01/hk80`，`_tdx_name` 已支持，`get_daily(asset_type="hk")` 已透传），覆盖 1999→今 | ❌ 无 |
| **分钟 minute** | ✅ TDX minutes | ✅ TDX `minutes/YYYY/YYYYMMDD/hkNNNNN.csv` 实测存在；2026-07-03 样本 `hk00700/hk09988/hk02577/hk06088.csv` 均可由 `EngineDataDiskClient.get_minutes(..., asset_type="hk")` 读取价格/成交量 | ❌ 无 |
| **实时 realtime** | ✅ Sina/Tencent | ✅ Tencent HK quote 实测可用：`SinaTencentClient.get_quotes(["00700.HK","09988.HK","02577.HK","06088.HK"])` 返回 4 行，含 name/last_price/prev_close/open/high/low/volume/amount | ❌ 无 |
| **复权 adj_factor** | ✅ TDX xdxr → ex_factor + raw 重建 | ❌ **口径缺失**：现 xdxr 前复权是 A 股除权除息逻辑（`asset_type=="stock"` 才重建，`fquant_provider.py`）；HK 拿到的是**未复权 raw**。港股需另立复权口径 | ❌ 无 |
| **财务 financial** | ✅ fstore | ❌ 当前 fstore 财务表无港股记录：`financial_report_income_statement/balance_sheet/cash_flow/annual/quick/forecast` 对 `00700/09988/02577/06088` 均 0 行，provider `get_financial()` 也均空 | ❌ 无 |
| **盘口 depth** | ✅ Tencent 五档 | ⚠️ **部分**：Tencent HK quote 可用，但 `get_depth()` 对 4 个样本只返回当前价/零量，2-5 档价格与量均为 0；不能视为可用五档盘口。且现 depth 服务框定在"真假涨停"（A 股概念，见下） | ❌ 无 |
| **指数/板块 universes** | ✅ fstore chengfen_gu | ❌ 当前 fstore 未提供恒生类成分：`chengfen_gu_items` 中 `HSI/HSCEI/HSTECH`、`index_name ILIKE '%恒生%'`、四个港股 `stock_code` 样本均 0；provider `get_universe_constituents("HSI")` 返回 0 | ❌ 无 |

> **数据核实结果（2026-07-03）**：HK 的 ① base_infos 清单 ✅ ② TDX 分钟线 ✅ ③ 财务表 ❌ ④ Tencent 实时 ✅ / 五档盘口 ⚠️退化不可用 ⑤ 指数成分 ❌。实测样本：`00700.HK`、`09988.HK`、`02577.HK`、`06088.HK`。

---

## 三、逐功能 A 股硬编码清单 + HK/US 适配难度

### 1. 符号 / 交易所辨识 —— HK：低｜US：高

**现状**：`AssetType = Literal["stock","index","etf","hk"]`（`app/data_providers/base.py:10`，**已含 hk、无 us**）。
- `symbols.py:48-66` `code_to_symbol`：A 股按 6 位数字前缀分 SH/SZ/BJ；HK 走 asset_type=3→`.HK`。
- `kline.py:18-23` `_asset_type_for_symbol`：`.HK`→"hk"，其余按前缀。

**HK（低）**：符号体系已内建，基本无需改。
**US（高）**：美股无数字代码（AAPL/TSLA 等字母 ticker），`code_to_symbol` 的"数字前缀分交易所"完全不适用；需新增 `.US`/`.O`/`.N` 后缀体系与字母 ticker 处理，`AssetType` 扩 `us`。

### 2. 涨跌停 / 连板信号 —— HK：概念性不适用｜US：概念性不适用

**现状**（`app/indicators/pipeline.py:534-556`）：板块前缀硬编码 limit_pct——
```
300/301(创业板)→0.20, 688/689(科创)→0.20, .BJ(北交所)→0.30, ST→0.05, 其余→0.10
```
据此产出 `signal_limit_up/limit_down/连板数/炸板/跌停翘板` 等。

**HK/US（概念性不适用）**：港股**无涨跌停**（有市场波动调节机制 VCM，非固定百分比）；美股**无日内涨跌停**（有全市场熔断，非个股）。所有涨停/跌停/连板/封板率/炸板信号对 HK/US **无意义**，应在非 A 股标的上**关闭**（返回空/NA），而非套 10% 规则算出假信号。改造点：pipeline 计算前按 asset_type 分流，非 A 股跳过整段 limit 信号。

### 3. 交易时间 / 日历 —— HK：中｜US：高

**现状**（`app/services/quote_service.py:658-663`、`depth_service.py:585`）：
```python
morning = 9:15 <= t <= 11:35;  afternoon = 12:55 <= t <= 15:05
return now.weekday() < 5 and (morning or afternoon)   # 本地时间，无时区，无节假日历
```

**HK（中）**：港股 9:30-12:00 / 13:00-16:00，与 A 股不同但**同在 Asia/Shanghai 时区、工作日重叠度高**；只需按 asset_type 换时段表。港股节假日与内地不同（需港股交易日历）。
**US（高）**：美东 9:30-16:00 = 北京 **21:30-04:00（跨夜）**，且**含夏令时**（切换 22:30-05:00）。`now.weekday()<5` 用本地时间判断对跨夜时段直接失效；需引入市场时区 + 夏令时 + 美股节假日历，是重写。

### 4. 前复权口径 —— HK：中｜US：高

**现状**：A 股用 TDX xdxr（除权除息）重建 raw 前复权序列，仅 `asset_type=="stock"` 触发（F1 修复后 HK 明确**绕开**该分支）。
**HK（中）**：港股有拆股/合股/分红等 corporate actions，但口径与 A 股 xdxr 不同；当前 HK 返回**未复权价**，个股分析/回测的长周期收益会失真。需接港股复权因子源或明确"HK 用未复权"并在 UI 标注。
**US（高）**：需美股 split/dividend adjusted 数据源，全新。

### 5. 个股分析 —— HK：中｜US：高

**现状**：`stock_analyzer.py` 吃 enriched 指标（含涨跌停/换手率/连板等 A 股列）+ AI prompt。
**HK（中）**：技术指标（MA/MACD/RSI/BOLL/量价）**市场无关**，可直接用；但依赖的 enriched 里涨跌停/连板列对 HK 为空，AI prompt（`stock_analyzer.py`）措辞偏 A 股需软化。换手率依赖流通股本（HK 口径不同，需核实 base_infos 是否提供）。
**US（高）**：指标逻辑可复用，但数据源/复权/财务全缺。

### 6. 回测 —— HK：中｜US：高

**现状**（`app/services/backtest.py:87,244-248`；`app/backtest/strategy.py`）：撮合支持 `close_t` / `open_t+1`，默认 T+1（`open_t+1`）。**未见对涨跌停"一字板无法成交"建模**——即回测在这一轴上本就市场中性。
**HK（中）**：港股 **T+0**（当日可回转），默认 `open_t+1` 撮合语义仍可跑，但若要精确模拟 T+0 需加撮合模式。费用结构不同（港股印花税/交易征费 vs A 股佣金），`fees_pct` 需按市场配。基准指数需换恒生（现基准是 000300/000905/399006/000688，`development-roadmap` 已列宽基）。
**US（高）**：T+0、无涨跌停、费用/基准全不同 + 数据源缺。

### 7. 监控 / 告警 —— HK：中｜US：中（逻辑层）

**现状**：`strategy/monitor.py`、`monitor_rules.py`、告警走 `notify_adapter`/`webhook_adapter`（飞书/钉钉/企微/MeoW）。监控规则引擎基于 enriched 列 + 实时行情。
**HK（中）**：告警**通道层市场无关**（可直接用）；监控规则若引用涨跌停/连板类列对 HK 失效，纯价格/均线/量能规则可用。触发时机绑定 A 股交易时段（见 #3）。
**US（中）**：同上，但受制于 #3 交易时区重写 + 数据源。告警通道本身可复用。

### 8. 盘口（depth / 真假涨停）—— HK：概念性不适用（sealed 部分）｜US：不适用

**现状**（`app/services/depth_service.py`）：depth 服务框定为"**真假涨停/跌停 sealed**"——读涨跌停名单→拉五档→判断封板真伪，是**A 股涨停板专属概念**。
**HK/US**：无涨停板→"真假涨停"整个服务**不适用**。五档盘口数据本身（若 Tencent HK 提供）可作为独立行情展示，但需与"sealed 判断"解耦。

### 9. 市场概览 / 情绪 / 盘后复盘 —— HK：高（需重做）｜US：高

**现状**（`app/services/market_recap.py:41-84`、`market_overview_builder.py`）：AI prompt 明写"**15 年 A 股经验**"，核心指标是**涨跌家数/涨停跌停炸板/连板梯队/封板率/情绪温度**——全是 A 股情绪面概念。
**HK/US（高）**：这套情绪面框架**不能移植**，需为港股/美股重新定义市场结构指标（如涨跌家数可用，但连板/封板/情绪梯队要删/换）。属重做，不是适配。

### 10. Screener / RPS 轮动 —— HK：中｜US：高

**现状**：`screener.py` 内置策略含"涨停追涨"等 A 股策略；RPS 轮动基于概念/板块（`ext_gn_ths` 同花顺概念，A 股专属）。
**HK（中）**：通用技术面 screener 策略（均线/突破/超跌）可复用；涨停类策略不适用；概念板块轮动缺港股概念数据源。
**US（高）**：数据源缺 + 板块体系不同。

### 11. 扩展数据（龙虎榜/解禁/两融/概念等）—— HK：低（大部分不适用）｜US：不适用

**现状**：C3 接的东财数据源（龙虎榜/解禁/股东户数/两融/大宗/研报/新闻）+ 概念/行业，**全是 A 股参考数据**。
**HK/US**：这些数据源本身是 A 股的，对 HK/US 无对应；不是"适配"而是"另找港美股参考数据源"，优先级低。

---

## 四、难度评级汇总

| 功能 | 港股 HK | 美股 US | HK 主要工作 |
|---|---|---|---|
| 符号/交易所辨识 | 🟢 低 | 🔴 高 | 已内建，微调 |
| 涨跌停/连板信号 | ⚫ 概念不适用 | ⚫ 概念不适用 | 按 asset_type 关闭该段 |
| 交易时间/日历 | 🟡 中 | 🔴 高 | 换时段表 + 港股日历 |
| 前复权口径 | 🟡 中 | 🔴 高 | 接港股复权源或标注未复权 |
| 个股分析 | 🟡 中 | 🔴 高 | 指标复用，prompt 软化 |
| 回测 | 🟡 中 | 🔴 高 | 费用/基准/T+0 配置化 |
| 监控/告警 | 🟡 中 | 🟡 中(+数据源) | 通道复用，规则去涨停列 |
| 盘口(真假涨停) | ⚫ 概念不适用 | ⚫ 概念不适用 | sealed 与 depth 解耦 |
| 市场概览/情绪 | 🔴 高(重做) | 🔴 高 | 重定义市场结构指标 |
| Screener/RPS | 🟡 中 | 🔴 高 | 通用策略复用 |
| 扩展参考数据 | 🟢 低(多不适用) | ⚫ 不适用 | 另找港美股数据源 |

**数据源前提**（决定一切）：HK 🟡 行情主链路可用（标的/日线/分钟/实时），但财务、港股指数成分、可用五档盘口缺口已实测确认；US 🔴 零覆盖，需从头引入。

---

## 五、分阶段建议

### 港股（推荐作为下一步增量）
1. **P0 数据核实（已完成）**：fstore base_infos 港股 2931 行；TDX 港股分钟线可读；Tencent HK quote 可读但五档盘口退化；fstore 港股财务与恒生类指数成分当前不可用。**后续边界**：先做行情/技术分析/回测适配，财务与指数成分需另补数据源。
2. **P1 个股行情 + 技术分析（已落地）**：`MarketProfile` 统一市场画像；个股 K 线按 `asset_type` 计算，HK 跳过涨跌停/连板信号并返回 `adjustment="none"`；个股分析在本地模式下可按需拉 HK 日 K 计算技术指标；实时/盘口轮询时段已覆盖 HK 午后至 16:00。**边界**：仅覆盖单股行情与技术分析。
3. **P-next HK 批量 enrich（中）**：回测/筛选/监控依赖批量 enriched 表，需把 HK universe 纳入批量管道后再开放这些功能。
4. **P2 港股复权口径（中）**：当前 P1 明确标注 HK 未复权；后续需接港股复权因子或继续在 UI/报告中明确"未复权"。
5. **P3 功能适配（中）**：回测/监控按市场配置化（基准、费用、prompt）。
6. **不做**：涨停梯队/真假涨停/情绪复盘/A 股参考数据 —— 概念不适用，港股需另设。

### 美股（独立立项，先解决数据源）
- 现阶段**不建议**在 panel 内增量适配——`AssetType` 无 us、零数据源、跨夜时区+夏令时、字母 ticker，属平行搭建数据管道。应作为**独立项目**：先确定美股数据源（日线/实时/财务/复权/符号），再评估功能移植。本轮不展开。

---

## 六、关键风险与未决项

| 项 | 说明 |
|---|---|
| **数据核实结论** | 第二节 5 项已实测：标的清单/分钟线/实时 quote 可用；财务、恒生类成分缺；Tencent HK depth 退化为不可用五档。 |
| **港股复权口径** | 当前 HK 返未复权价，长周期分析/回测收益失真；需先定口径再谈回测。 |
| **概念性不适用 ≠ 适配** | 涨跌停/连板/真假涨停/情绪梯队是 A 股概念，对 HK/US 是"关闭或重做"，不要试图套用 A 股规则算假信号。 |
| **美股时区陷阱** | `now.weekday()<5` 本地时间判断对美股跨夜时段失效；夏令时切换是额外复杂度。 |
| **告警/指标是市场无关的** | MA/MACD/RSI/BOLL、通知通道（飞书/钉钉等）可直接复用，是移植中成本最低的部分。 |

---

## 附：本评估核对的关键代码位置

- `app/data_providers/base.py:10` — `AssetType` 含 hk、无 us
- `app/data_providers/fquant/symbols.py:48-66,149-163` — 符号/asset_type 映射（hk=3）
- `app/data_providers/fquant/sina_tencent_client.py:20` — 实时含 HK 前缀
- `app/data_providers/fquant/engine_data_disk.py` `_tdx_name` — TDX HK 分桶
- `app/data_providers/fquant_provider.py:164-173` — capabilities；`:287` get_daily 透传 asset_type
- `app/indicators/pipeline.py:534-556` — 涨跌停 limit_pct 板块前缀硬编码
- `app/services/quote_service.py:658-663` / `depth_service.py:585` — 交易时段硬编码（无时区/日历）
- `app/services/backtest.py:87,244-248` — 撮合 close_t/open_t+1（T+1）
- `app/services/market_recap.py:41-84` — A 股情绪面 prompt/指标（15 年 A 股经验、连板梯队）
- `app/services/depth_service.py:1-19` — depth=真假涨停 sealed（A 股概念）
