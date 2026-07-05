# 三把锁指标（三锁）重建 — 设计文档

> 状态：设计定稿，待 codex 评审。范围为一次性做齐三个锁（趋势/资金/形态），不分阶段。
> 日期：2026-07-05（含一次架构修订：资金流数据改为请求时实时取数，不做历史落盘/回填）
> 影响范围：`EngineDataDiskClient`/`FQuantProvider` 资金流范围查询方法 / kline API 本地模式请求时合并 / 前端三锁计算单元 / K 线图可视化

## 背景

K 线图主图叠加指标里有一个"三锁"（又叫"三把锁"，源自"指南针"App）。当前 `frontend/src/components/EChartsCandlestick.tsx` 里的实现是错的——它用 MA30/60/90 三条均线互相"粘合" + 放量突破来判定，注释里还写着"东方财富公开公式"这种编造来源（`EChartsCandlestick.tsx:524`）。用户已确认这版是臆测的，要整体删除、换成正确实现。

正确公式的可信来源不是网上股民解读（互相矛盾、无官方公式），而是相邻仓库 `../fquant` 里一份经过 v3→v4 审查、带 14 个单测的实现。该实现是"对齐指南针 reference 截图反推"，做得严谨，是目前能拿到的最可信版本：

- RFC 设计文档：`/Users/wf2311/Projects/wf2311/fm/fquant/docs/rfc/2026-06-05-three-locks-indicator.md`
- 核心算法：`/Users/wf2311/Projects/wf2311/fm/fquant/web/src/components/threeLocks.ts`
- 单测：`/Users/wf2311/Projects/wf2311/fm/fquant/web/src/components/threeLocks.test.ts`

本设计以 `threeLocks.ts`（v4 实际代码）为算法权威，将其移植进 tickflow，并补齐 tickflow 缺失的资金流历史数据。

> 注：`fquant` RFC 正文第 0、一节（定义）与 v4 代码一致，但 RFC 第 2.2.1 节内嵌的 TypeScript 伪代码是过时的 v3 版本（MA20/60/120 + 放量口径），**不要**照它写。以 `threeLocks.ts` 为准。

## 三把锁定义（已对 `threeLocks.ts` 逐行核实）

三个锁各自独立计算，每根 K 线都能算出一组状态。"开锁"（`true`）= 条件满足 = 多头；数据不足时为 `null`，与"条件不满足"（`false`）区分。

### 趋势锁（Trend Lock）
- 定义：`MA5 > MA10 > MA20 > MA60` 四线多头排列（从短到长依次递减）。
- 数据不足：任一均线为 `null` → 锁为 `null`。因为 MA60 需要 60 根有效收盘价，实际等价于"有效 K 线 < 60 → null"。
- 依据：`threeLocks.ts:121-126`；单测 `testTrendNeeds60ValidCloses`（59 根 → null）、`testTrendAlignsWithMa5102060`。

### 资金锁（Capital Lock）
- 定义：**最近 3 个交易日**主力净流入之和 > 0。
- 实现口径（关键）：取升序排序后**末尾 3 行**（`sorted.slice(-3)`），统计其中 `main_net_inflow` 非空的天数 `validInflowDays`。若 `validInflowDays < 3` → 锁为 `null`（数据不足）；否则 `sum(main_net_inflow[-3:]) > 0` → `true`，`<= 0` → `false`。
- 注意：这是"末尾 3 个日历行里非空的个数"，**不是**"向前跳过 NULL 凑够 3 个有效日"。只要末尾 3 行任意一行缺资金流数据，资金锁即判 `null`。
- 依据：`threeLocks.ts:129-132`；单测 `testCapitalIsThreeDayCumulative`、`testCapitalNullWhenRecentInflowMissing`、`testCapitalLockedWhenAllThreeDaysNegative`。

### 形态锁（Pattern Lock）
- 定义：`close > MA20` **且** 近 3 日最高价 > 前 17 日最高价。
  - 近 3 日最高：`max(high[-3:])`（需 ≥ 3 根，`threeLocks.ts:136`）
  - 前 17 日最高：`max(high[-20:-3])`（4~20 日窗口，**排除**自身最近 3 日，需 ≥ 20 根，`threeLocks.ts:137`）
- 数据不足：MA20 为 `null`，或最新 `close` 非有限值，或两段窗口任一取不到有限最大值 → 锁为 `null`。窗口内任一 `high` 缺失会使该段 `max` 判为 `null`（`maxFinite` 语义，`threeLocks.ts:84-87`）。
- 判定用 `high` 而非 `close`，故上影线创高也算创高。
- 依据：`threeLocks.ts:135-141`；单测 `testPatternExcludesRecentThreeDaysFromPriorHigh`、`testPatternUsesUpperShadowHigh`。

### 综合信号与逐锁事件（v4）
- 逐锁独立事件：每个锁的状态变化（`on` 开锁 / `off` 破锁，含"首次有足够数据时的初始状态"）各自出一个小图标。配色（沿用 fquant v4，RFC `一` 节 line 72-74）：
  - 趋势锁 = 红 `#d23b3b`
  - 资金锁 = 粉 `#c46a7a`
  - 形态锁 = 橙 `#d99930`
- 综合 buy/sell：三锁**首次全开**（3/3）出 buy，全开后**首次破开**出 sell。
- 依据：`buildAllSignals`（`threeLocks.ts:177-235`）返回 `{ combined, perLock }`；`buildClusterSignals`（`threeLocks.ts:241-268`）在任一锁翻转时出簇状信号。

## 现状盘点（tickflow 侧，已核实）

| 事项 | 现状 | 证据 |
|---|---|---|
| K 线数据类型 | `OHLC` 接口已含 `ma5`/`ma10`/`ma20`/`ma60`、`close`、`high`（后端已算好，现有独立"MA"开关在用）；**无** `main_net_inflow` 字段 | `EChartsCandlestick.tsx:6-31`（MA 字段 16-19） |
| 现有错误三锁 — markPoint | `activeIndicators.includes('threelock')` 时用 MA30/60/90 粘合 + 放量突破生成"锁"标记；含编造注释"东方财富公开公式" | `EChartsCandlestick.tsx:524-570` |
| 现有错误三锁 — 均线绘制 | 额外画 MA30/60/90 三条 `lockLine` | `EChartsCandlestick.tsx:731-743` |
| 指标注册 | 三锁作为主图叠加指标已注册 | `EChartsCandlestick.tsx:284`（`OVERLAY_INDICATORS` 的 `{ key: 'threelock', label: '三锁' }`） |
| 可复用的可视化范式 | "神奇九转" td9 在 `buildOption` 内、`activeIndicators.includes('td9')` 时直接 push markPoint（roundRect 徽标 + circle 文字，带 `z/zlevel`） | `EChartsCandlestick.tsx:487-522` |
| markPoint 视觉语言 | 蜡烛 series 的 `markPoint.data = markPointData`，在 series 构造前收集完毕 | `EChartsCandlestick.tsx:433-481, 690-701` |
| 资金流数据（历史序列） | tickflow **未持久化**任何主力净流入历史序列 | 全仓库 enriched 列无 moneyflow 相关字段 |
| 资金流数据（可用数据源） | Provider 有 `get_moneyflow_daily(symbols, date)`，可取指定标的某日主力净流入 | `fquant_provider.py:1094-1124` |
| 盘后管道 enriched 步骤 | `run_now` 的 `compute_enriched` 步骤只吃 raw OHLCV + 除权因子 + instruments，**完全不涉及** moneyflow | `daily_pipeline.py:258-353`（进度回调 `_enriched_batch_progress` 在 line 291） |
| kline API enriched 响应 | `GET /api/kline/daily` 直接 `df.to_dicts()` 吐出 enriched 全部列 | `kline.py:295, 300-308` |

### ⚠️ 修正 1：`get_moneyflow_daily` 不是"按日期查全宇宙"，需传标的列表

背景描述称该接口"按日期批量查询该日全部标的（一次调用拿所有 symbols，不按标的逐个查）"。核实后**不完全属实**：

- 真实签名是 `get_moneyflow_daily(self, symbols: list[str], date: datetime | None = None)`（`fquant_provider.py:1094-1096`），**必须显式传标的列表**，不是仅凭日期就返回全市场。
- 数据路径分两种（`fquant_provider.py:1111-1120`）：
  - **磁盘模式**（`fquant_local`，据项目记忆为当前上线源）：对每个标的循环调用 engine 磁盘 `get_fund_daily(code, date)`，本质是**逐标的**读取（`engine_data_disk.py:146`）。
  - **HTTP 模式**：缺失的 code 走 `MoneyflowClient.get_daily(codes, date)`，把 codes 用逗号拼成**单个** GET 请求（`moneyflow_client.py:88-112`，拼接在 line 99）——这一路才接近"一次调用拿多标的"，但 ~5500 个 code 拼进一个 URL 可能超长，需在实现时分批（chunk）。

**对本设计的影响**：架构不变——管道本就有 `_resolve_universe(capset)` 解析出全市场标的池（`daily_pipeline.py:58-85`），新步骤用它作为 `symbols` 传入即可。但成本表述要改成"逐日 × 一个 universe 列表"，并在实现层按 provider 模式决定是否分批 / 是否按标的循环。

### ⚠️ 修正 2：源字段叫 `main_net`，不叫 `main_net_inflow`

fquant 算法与背景用的字段名是 `main_net_inflow`。tickflow 的 `get_moneyflow_daily` 产出的 DataFrame 列名是 **`main_net`**（`mapping.py:344-379`，主力净流入在 line 361；磁盘模式 `get_fund_daily` 返回的字典键同样是 `main_net`，`engine_data_disk.py:158`）。

**对本设计的影响**：源列是 `main_net`。为与 fquant 算法接口一致、并方便前端移植，落盘 / API / 前端统一采用目标列名 **`main_net_inflow`**，在管道写入时做一次 `main_net → main_net_inflow` 的重命名映射。本文档下文所有 `main_net_inflow` 均指此重命名后的列。

### ⚠️ 修正 3：本地模式下 kline API 读的是"即时重算 enriched"，不是落盘 parquet

背景把 kline API 当作"读 enriched 表 → 加一列即可"。核实后：`GET /api/kline/daily` 有多条路径，**当前上线的本地日K模式**（`is_local_daily_mode()` 为真）走的是**即时重算**分支——`provider.get_daily(...)` 取 raw 后当场 `compute_enriched(...)` 返回（`kline.py:199-245`，重算在 line 235），**不读**落盘的 `kline_daily_enriched`。只有非本地模式的 enriched 分支（`kline.py:247-308`）才 `repo.get_daily` 读落盘表。此外实时蜡烛注入 `_maybe_inject_live_candle` 用的是**固定字段白名单**（`kline.py:424-431`），当前不含 `main_net_inflow`。

**原方案（已废弃）**：往 `kline_daily_enriched` parquet 落盘 `main_net_inflow` + 写一次性历史回填脚本。**该方案对当前生产使用的本地磁盘模式完全无效**——因为本地模式的单股 K 线渲染路径（`kline.py:199-245`）根本不读落盘的 `kline_daily_enriched` 表，写了也用不上。用户已确认并拍板改用**实时取数**：不写 parquet、不做回填脚本、不改 `daily_pipeline.py`，改为在 `compute_enriched()` 被调用的请求时刻现取现拼。详见下方"已确认的解决方案"与"架构"。

### ✅ 已确认的解决方案：`EngineDataDiskClient` 一次性整档读取（已核实）

针对"实时取数会不会退化成逐日循环调用、成本重新变高"的顾虑，核实 `backend/app/data_providers/fquant/engine_data_disk.py` 后确认一个关键事实，使实时取数方案比预想的更便宜：

- `EngineDataDiskClient._read(dataset, symbol_or_code, asset_type)`（`engine_data_disk.py:50-59`）就是一次 `pl.read_csv(path)`，**读整份 CSV、不做任何日期过滤**。
- `get_fund_daily(code, date_iso, asset_type)`（`engine_data_disk.py:146-169`）内部调用 `self._read("fund", code, asset_type)` 拿到**该标的全部历史资金流记录**，然后才 `df.filter(pl.col("Date") == date_iso)` 筛出单日。也就是说，**该标的的资金流历史文件本来就是整档存在磁盘上的一个 CSV**，现有 `get_fund_daily` 只是"多读一次、只用一行"，天生适合改成一次性读区间。
- 结论：应新增 `EngineDataDiskClient.get_fund_range(code, start_iso, end_iso, asset_type=None) -> pl.DataFrame`，内部同样调 `self._read("fund", code, asset_type)` **一次**，按 `Date` 过滤到 `[start_iso, end_iso]` 区间后返回 `date`/`main_net_inflow`（源列 `Main` 重命名，同 ⚠️ 修正 2）两列。单标的一次磁盘读取即可覆盖整个 120 天窗口，不需要循环 120 次。

**调用链确认（`is_local_daily_mode()` 分支能否接上这个方法）**：
- `backend/app/data_providers/registry.py:11-13`：`_PROVIDERS["fquant_local"] = lambda: FQuantProvider(engine_mode="disk")`。
- `FQuantProvider.__init__`（`fquant_provider.py:176-184`）：`engine_mode == "disk"` 时 `self._engine = EngineDataDiskClient()`。
- `is_local_daily_mode()`（`data_mode.py:5-11`）判断 `get_active_provider_name("daily") == "fquant_local"`；`kline.py:200-202` 的 `provider = get_provider(get_active_provider_name("daily"))` 在该分支下必然拿到 `FQuantProvider(engine_mode="disk")` 实例，其 `self._engine` 就是上面确认的 `EngineDataDiskClient`。
- **确认接得上**：`kline.py` 本地模式分支里已经在用的 `provider` 变量，直接就是磁盘引擎的持有者，新增一个 `FQuantProvider.get_moneyflow_range(symbol, start, end)` 方法（内部转发 `self._engine.get_fund_range(...)`，`hasattr` 判断沿用 `get_moneyflow_daily` 已有写法 `fquant_provider.py:1112`）即可在该分支直接调用，不需要额外包装或新实例化。

**⚠️ 对协调者第 3 点问题的核实结论（与协调者原描述有出入，需明确指出）**：协调者问"非本地模式要不要也接入、保持两条路径行为一致"。核实后发现这**不是一个能简单对齐的对称问题**，原因：

1. `GET /api/kline/daily` 非本地模式实际有两条子路径（`kline.py:247-308`），而不是一条：
   - **主路径**（`kline.py:248-249`，`df = repo.get_daily(symbol, start, end)` 命中时）：直接读**已落盘**的 `kline_enriched` 视图，**不调用** `compute_enriched()`。这条是非本地模式下的常态路径（标的已有历史数据时都走这条）。给它加 `main_net_inflow` 必须往这张落盘表写列——等于要碰 `daily_pipeline.py`/`run_pipeline()`，正是用户刚否决的方案。**这条路径在本设计范围内无法处理，只能维持现状（无 `main_net_inflow`）**。
   - **回退路径**（`kline.py:250-293`，`df.is_empty()` 时，典型场景是 Free 用户首次查询未落盘的标的）：这条确实调用 `compute_enriched(raw, ...)`（`kline.py:281`），理论上可以在这里也插入一次实时取数。
2. 但即便是回退路径，实时取数在技术上也做不到"一次读区间"：非本地模式下这里用的 provider 是 HTTP 引擎（`EngineDataClient`），核实 `engine_data_client.py` **没有任何 `get_fund_*`/资金流相关方法**；唯一可用的资金流来源是 `MoneyflowClient.get_daily(codes, date_iso)`（`moneyflow_client.py:88-112`），这是**单日**查询，接口不支持日期区间。要覆盖 120 天窗口只能对同一 symbol 循环调用 120 次单日 HTTP 请求——在一次页面请求的同步路径里这样做延迟不可接受，不是"实时取数"方案能覆盖的场景。
3. 结论：**本设计只解决本地磁盘模式（`is_local_daily_mode()` 为真）这一条路径**，即当前生产实际在用的路径。非本地模式的两条子路径（无论主路径还是回退路径）都不在本次范围内解决，作为风险表中的开放项显式记录，不假装"两条模式行为一致"。

## 架构

分四层，自下而上（原方案的"daily_pipeline 新增落盘步骤"与"一次性历史回填脚本"两层已整体移除，替换为下面第 1-2 层的实时取数机制）：

### 1. Provider 层：新增资金流区间查询方法

- `EngineDataDiskClient.get_fund_range(code, start_iso, end_iso, asset_type=None) -> pl.DataFrame`（`engine_data_disk.py` 新方法，紧邻既有 `get_fund_daily` 定义）：内部调用 `self._read("fund", code, asset_type)` **一次**，读回整份历史 CSV 后按 `Date` 过滤到 `[start_iso, end_iso]` 区间，返回 `date`/`main_net_inflow`（源列 `Main` 重命名，同 ⚠️ 修正 2）两列。标的资金流文件不存在时，`_read` 已有的"路径不存在 → 返回空 df"降级（`engine_data_disk.py:52-54`）天然适用，新方法据此直接返回空 df。
- `FQuantProvider.get_moneyflow_range(symbol: str, start: datetime, end: datetime) -> pl.DataFrame`（`fquant_provider.py` 新方法，紧邻既有 `get_moneyflow_daily`）：`hasattr(self._engine, "get_fund_range")` 为真时（磁盘模式）转发给 `self._engine.get_fund_range(...)`；为假时（HTTP 模式）直接返回空 df——HTTP 引擎当前无区间查询能力，不在本次范围内补（见"风险与开放项"）。

### 2. kline API：本地模式请求时合并

在 `kline.py` 的 `is_local_daily_mode()` 分支（`kline.py:199-245`）里，`enriched = compute_enriched(raw, ...)`（`:235`）之后新增一步：

- 调用 `provider.get_moneyflow_range(symbol, start_dt, end_dt)`——`provider` 就是该分支里已经在用的变量（见上文"调用链确认"，不需要新实例化），得到该股在请求窗口 `[start_dt, end_dt]` 内的 `date`/`main_net_inflow` 序列。
- 按 `date` 左连接（left join）进 `enriched` DataFrame；请求窗口内缺失日期的 `main_net_inflow` 保持 `null`。
- **仅对 `asset_type == "stock"` 执行**此步骤——主力净流入概念不适用于指数（`index`）/ETF/港股（`hk`），且磁盘 `fund` 数据集大概率只覆盖 A 股个股；其余 `asset_type` 的 `main_net_inflow` 恒为 `null`，资金锁相应恒判"数据不足"，与三锁定义（本就面向个股）不矛盾。
- 合并后照旧 `.tail(days).to_dicts()`，`main_net_inflow` 随每行一起吐给前端，供 `threeLocks.ts` 消费。
- 实时蜡烛注入 `_maybe_inject_live_candle`（`kline.py:376-444`）的字段白名单不含 `main_net_inflow`；当日实时蜡烛该字段保持缺省 `null`，与"错误处理"章节一致（不强判真假，留作数据不足）。

### 3. 前端计算单元：新建 `frontend/src/lib/threeLocks.ts`

移植 `../fquant/web/src/components/threeLocks.ts` 的算法，**适配 tickflow 的 `OHLC` 类型**（不是照抄——fquant 用它自己的 `ThreeLocksKLinePoint` 接口）：

- 输入类型改用 tickflow `OHLC`（`EChartsCandlestick.tsx:6-31`），并给 `OHLC` 新增字段 `main_net_inflow?: number | null`。
- 保留并移植的导出：`computeThreeLocks`、`buildAllSignals`（`combined` + `perLock`）、`buildClusterSignals`、`sortKLinesByDateAsc`、`buildMovingAverageSeries`，以及类型 `ThreeLocksResult`/`LockKey`/`LockDirection`/`PerLockSignal`/`ThreeLockSignal`。
- 算法逻辑（趋势/资金/形态判定、排序、窗口）与 fquant 逐行对齐，见上文"三把锁定义"。
- tickflow 后端已算好 MA5/10/20/60，理论上可直接用 `OHLC.ma*`；但为与 fquant 移植测试对拍、避免口径漂移，`threeLocks.ts` 内部仍按收盘价自行计算 SMA（与 fquant 一致），MA 字段仅供 UI 信息栏显示用。

### 4. 可视化：改造 `EChartsCandlestick.tsx`

- **删除**现有错误三锁：markPoint 生成块（`EChartsCandlestick.tsx:524-570`，含编造注释）与 MA30/60/90 `lockLine` 绘制块（`EChartsCandlestick.tsx:731-743`）。保留 `OVERLAY_INDICATORS` 里的 `threelock` 条目（`:284`）。
- **新增**三锁 markPoint 生成：仿照 td9 的范式（`EChartsCandlestick.tsx:487-522`），在 `buildOption` 内、`activeIndicators.includes('threelock')` 时，调用 `buildAllSignals(data)` 得到 `combined` 与 `perLock`，push 进 `markPointData`（必须在蜡烛 series 构造 `:690-701` 之前完成，与 td9 注释 `:483-486` 同理）：
  - 逐锁 `perLock` 事件：小图标，颜色按锁取（趋势红 `#d23b3b` / 资金粉 `#c46a7a` / 形态橙 `#d99930`）；`on` 置于 low 下方、`off` 置于 high 上方（沿用 fquant v4 位置语义，RFC `一` 节 line 75-76）。
  - 综合 `combined`：buy = 蜡烛下方三把闭合锁簇；sell = 蜡烛上方绿色开锁。
  - 复用 tickflow 现有 markPoint 视觉规格：`symbol: 'roundRect'/'circle'`、`z:100, zlevel:10`、`fontFamily: 'JetBrains Mono, monospace'`，与 td9/涨停标签保持一致（`EChartsCandlestick.tsx:498-520`）。
  - `compact`（可见蜡烛过多）时降级为小圆点，与 td9 的 `compact` 分支一致。
- 三锁 markPoint 走 `buildOption` 内生成（与 td9 同一机制），**不**经 `markers` prop / `updateMarkPoints`（`:1066-1124`），避免与外部买卖点标记语义混淆。

## 错误处理

- **标的资金流文件不存在**（磁盘上无对应 `fund` CSV，如新股/北交所/未覆盖标的）→ `get_fund_range` 命中 `_read` 的"路径不存在"降级，返回空 df；合并后该股全部日期的 `main_net_inflow` 为 `null`。资金锁按"最近 3 日有效天数 < 3"判 `null`（数据不足），**不**让整个 `GET /api/kline/daily` 请求报错——OHLC/趋势锁/形态锁照常返回。
- **文件存在但请求窗口内部分日期缺行**（如停牌、上市不足 120 天）→ 左连接后对应日期 `main_net_inflow` 为 `null`，不报错，视为该日数据缺失。
- **磁盘读取异常**（CSV 损坏等）→ 沿用 `_read` 已有的 try/except 降级（`engine_data_disk.py:55-59`，异常时记 warning 并返回空 df），效果同"文件不存在"，不向上抛异常、不影响该请求其余字段。
- "数据不足"（`null`）与"确定不满足"（`false`）在前端要能区分：两者都不出对应锁图标，但内部状态不同——为将来可能的"数据不足"提示（如信息栏文字）留口子，不把 `null` 当 `false` 处理（对齐 `threeLocks.ts` 的 `LockState = boolean | null`）。
- 非 `stock` 的 `asset_type`（指数/ETF/港股）→ 按架构 §2 设计直接不查资金流，`main_net_inflow` 恒 `null`，资金锁恒"数据不足"，这是预期行为而非错误。

## 测试策略

### 后端
- `EngineDataDiskClient.get_fund_range` 单测（用临时目录构造 fixture CSV，不依赖真实 `TDX_DATA_DIR`）：
  1. 正常场景：fixture 覆盖请求区间，返回的 `date`/`main_net_inflow` 与源 `Date`/`Main` 逐行对应，且区间外日期被过滤掉；
  2. 标的资金流文件不存在 → 返回空 df，不抛异常；
  3. 请求区间内部分日期在文件中缺行（如停牌）→ 返回的 df 缺该日期行（由调用方 left join 后自然呈现为 `null`），不报错、不补 0。
- `FQuantProvider.get_moneyflow_range` 单测：mock `self._engine`，覆盖磁盘模式（`hasattr` 命中 → 转发）与 HTTP 模式（`hasattr` 不命中 → 直接返回空 df）两个分支。
- `kline.py` 本地模式合并逻辑单测：mock `provider.get_moneyflow_range` 返回的 df，断言合并进 `enriched` 后按 `date` 对齐正确、非 `stock` asset_type 跳过合并、`get_moneyflow_range` 抛异常或返回空 df 时接口仍正常返回其余字段（不 500）。

### 前端
- 移植 `../fquant/web/src/components/threeLocks.test.ts` 的 14 个用例，改造成 tickflow `OHLC` 数据类型后做对拍/移植测试，重点保住：降序输入排序、趋势 < 60 根 → null、资金锁末尾 3 日含 null → null、资金锁 3 日累计正/负、形态锁排除自身窗口、上影线创高、逐锁 `on/off` 事件、综合 buy/sell。
- `pnpm tsc --noEmit` + `pnpm build` 验证类型与构建（前端两条服务路径：改前端后需 `pnpm build` 重建 `dist/` 才在 `:8000` 静态托管生效）。

## 文件变更清单

| 文件 | 变更 |
|---|---|
| `backend/app/data_providers/fquant/engine_data_disk.py` | 新增 `get_fund_range(code, start_iso, end_iso, asset_type=None)`，一次性整档读取 + 区间过滤 + 列重命名 |
| `backend/app/data_providers/fquant_provider.py` | 新增 `get_moneyflow_range(symbol, start, end)`，磁盘模式转发 `get_fund_range`，HTTP 模式返回空 df |
| `backend/app/api/kline.py` | `is_local_daily_mode()` 分支 `compute_enriched` 之后新增合并步骤：调用 `get_moneyflow_range`、按 `date` left join、仅 `asset_type == "stock"` 执行 |
| `frontend/src/lib/threeLocks.ts`（新建） | 移植 fquant 三锁算法，适配 `OHLC` 类型 |
| `frontend/src/lib/threeLocks.test.ts`（新建） | 移植 fquant 14 个单测 |
| `frontend/src/components/EChartsCandlestick.tsx` | `OHLC` 加 `main_net_inflow` 字段；删除错误三锁（markPoint + MA30/60/90）；接入 `buildAllSignals` 生成新三锁 markPoint |
| 后端新建单测（`get_fund_range`/`get_moneyflow_range`/kline 合并逻辑） | 覆盖正常读取、文件不存在、部分日期缺失、非本地模式不受影响 |

**明确不改动**（与原方案的关键差异）：`backend/app/jobs/daily_pipeline.py`、`kline_daily_enriched` parquet schema、任何回填脚本——均维持现状，不新增。

## 风险与开放项

| 风险 / 待实现时确认项 | 说明 / 缓解 |
|---|---|
| 非本地模式两条子路径均未覆盖（架构 §2 后的核实结论） | 非本地模式的"落盘表命中"主路径（`kline.py:248-249`）和"回退现算"路径（`kline.py:250-293`）都不在本次范围内获得 `main_net_inflow`——前者需碰 `daily_pipeline.py`（用户已否决），后者受限于 HTTP moneyflow API 无区间查询、逐日循环延迟不可接受。三锁资金锁**只在当前生产使用的本地磁盘模式下可用**；若未来非本地模式重新启用，需要单独设计（可能仍需某种缓存/持久化，与本次"不持久化"的前提冲突，届时需重新评估）。 |
| 源列名 `main_net`（⚠️ 修正 2） | API/前端统一 `main_net_inflow`，`get_fund_range`/`get_moneyflow_range` 内部做一次重命名，勿直接透传 `main_net`。 |
| 资金锁口径是"末尾 3 行"非"跳空凑 3 有效日" | 与 fquant 一致（见定义）；末尾任一行缺值即判 null，属预期行为，测试须覆盖。 |
| 单请求内整档读取一次 CSV 的性能 | 每次单股 K 线请求都会完整读一次该标的资金流历史 CSV（无论请求窗口多长），比"只读需要的几行"多读一些数据；但单标的文件体积小（数百行数值型 CSV），且该分支本身已对该标的做一次 raw OHLCV 磁盘读取，量级相近，不构成新的性能瓶颈；无需额外缓存，若后续实测有性能问题再补内存缓存。 |
| 非 `stock` asset_type 恒无资金流 | 指数/ETF/港股不查资金流（架构 §2 显式排除），三锁资金锁对这些品种恒"数据不足"；与 fquant 原型本就面向个股一致，非缺陷。 |

## 附：与背景/协调者描述的核实差异汇总

实现前请知悉以下几处、已在正文对应小节详述：

1. `get_moneyflow_daily` 需传 `symbols` 列表、且磁盘模式为逐标的读取，并非"仅凭日期拿全宇宙"（⚠️ 修正 1，背景事实描述，本身仍成立）。**架构修订后本设计不再调用 `get_moneyflow_daily`**——三锁资金锁改走新增的 `get_moneyflow_range`（架构 §1），`get_moneyflow_daily` 保留给其它既有场景（如"今日"资金流展示）使用，不受本设计影响。
2. 主力净流入源列名是 `main_net`（非 `main_net_inflow`），需一次重命名映射（⚠️ 修正 2）。
3. 本地日K模式下 kline API 即时重算 enriched、不读落盘表（⚠️ 修正 3，已确认）；解决方案已从"落盘回填"改为"请求时经 `get_moneyflow_range` 实时取数"，详见架构 §1-2。
4. **协调者第 3 点问题的核实结论与协调者原描述有出入**：并非简单的"本地模式 vs 非本地模式，二选一是否要对齐"。非本地模式实际拆成"落盘表命中"与"回退现算"两条子路径，前者无法在不碰 `daily_pipeline.py` 的前提下获得该字段，后者受 HTTP moneyflow API 无区间查询能力限制、技术上不适合做请求时实时取数。因此**本设计明确只解决本地磁盘模式一条路径**，非本地模式两条子路径均作为开放项记录、不强行对齐，详见"现状盘点 → ⚠️ 修正 3 → ⚠️ 对协调者第 3 点问题的核实结论"小节与风险表首行。
</content>
</invoke>
