# FQuantProvider 架构设计方案

> 版本：v1 · 范围：`tickflow-stock-panel/backend/app/data_providers/fquant_provider.py` 的二阶段架构升级。
> 状态：**只设计、不实现**。本文档不替代 `FQUANT_PROVIDER.md`（PoC 现状描述），二者关系见 §1.3。

---

## 1. 目标与非目标

### 1.1 目标（In-Scope）

| # | 目标 | 验收点 |
|---|------|--------|
| G1 | 把 PoC 版 `FQuantProvider`（只透传 fquant 自身 HTTP，仅 2 个 capability）升级到**直连三上游源**：fstore PostgreSQL、engine-data API、moneyflow API。 | 不再依赖 fquant HTTP 端口；DB/API 可单独故障降级。 |
| G2 | **消除 TickFlow 付费依赖**：TickFlowProvider 当前 `daily / adj_factor / minute / realtime / financial` 全打通的字段，必须能由 FQuantProvider 在三源覆盖范围内等价产出（financial 不再是 ❌）。 | §9 覆盖矩阵每一格不再为 ❌（除已说明的限制）。 |
| G3 | **接口契约零修改**：`backend/app/data_providers/base.py` 不可修改；`FQuantProvider` 必须严格满足 `MarketDataProvider` Protocol，输出 `MINUTE_COLUMNS / DAILY_COLUMNS / ADJ_FACTOR_COLUMNS / INSTRUMENT_COLUMNS` 等内部 schema。 | 同一个 `service` 调用方，可换 `get_provider("fquant")` 而无需改动。 |
| G4 | 单源故障**不阻断**其它源链路：DB 挂 → adj_factors/instruments 走 fallback；engine-data 挂 → daily 可降级到 fstore `day_klines`；moneyflow 挂 → realtime/minute 走 fallback。 | §7 错误降级表覆盖 ≥80% 失败模式。 |
| G5 | 符号格式归一对齐：输出统一带后缀的 `code.SH / SZ / BJ / HK / INDEX / ETF`；内部**统一** fquant 跟 fstore/engine-data 的多种代码口径。 | §5 单测涵盖每一种代码源到内部符号的归一。 |
| G6 | **只输出设计，不输出实现**：本文档只列模块骨架、接口签名、字段映射表、伪代码；不创建任何 `.py`。 | 文档中出现的函数/类代码块明确标注「伪代码/接口骨架」。 |

### 1.2 非目标（Out-of-Scope）

| # | 项目 | 备注 |
|---|------|------|
| N1 | 不修改 `base.py / normalizer.py / schemas.py / registry.py`。 | N1+N3 共同保证 service 层零改动。 |
| N2 | 不在本期实现 realtimequotes 的逐笔/盘口推送（§3.3 没有合适的"分钟级当前快照"源；fquant 自身也用 tencent/tdex 兜底）。 | 转发到 §10 路线 1。 |
| N3 | 不修改 `backend/scripts/test_fquant_provider.py`、`backend/docs/FQUANT_PROVIDER.md`、`backend/app/data_providers/fquant_provider.py`（PoC 文件本期也不删）。 | 旧 PoC 提供 backward-compatibility；升级文件建议命名为 `fquant_provider_v2.py` 或保留同名大改——决策见 §3.2。 |
| N4 | 不重做 fquant `kline / metadata` HTTP 客户端：保留旧 PoC 中对 `localhost:8088` 的 `_get()` 帮助函数，但仅作为离线 fallback，不当作主源。 | 见 §4.1「依赖选择」。 |
| N5 | 不引入 ORM / SQLAlchemy：直接用 `psycopg[binary]` 或 `psycopg2` 执行参数化 SQL；表多但查询简单。 | 见 §4.2。 |
| N6 | 不重写 `engine-data` 的 `chips / xdxr / wide` 数据模型与 fquant 的 chip 体系对齐：本期仅消费 xdxr→adj_factor、wide→daily 字段增强。 | `chips` 引擎在 NAS 慢，本期不直接接入 `get_realtime()`。 |

### 1.3 与现有 PoC 文档的关系

| 文档 | 角色 | 状态 |
|------|------|------|
| `backend/docs/FQUANT_PROVIDER.md` | PoC 现状描述（只连 fquant 自身 HTTP，capabilities=`{instruments, daily}`） | 保留，作为历史 |
| **`backend/docs/FQUANT_PROVIDER_DESIGN.md`（本文）** | 升级版架构设计（三源 + 8 capability 全实现） | **新增** |

新 PoC 文件落地时，建议把 `FQuantProvider.__init__` 与 `capabilities` 与本文档 §3.2 完全一致；老 PoC 的 `__init__.py`/工具函数如 `split_symbol / code_and_market_to_symbol` 在 §5 中复用（设计允许保持不动）。

---

## 2. 三个上游源能力清单

> 全部字段均经过 **实测**（DB 用 `psql \d / SELECT LIMIT N`；API 用 `curl -sS --max-time 8`）。

### 2.1 fstore PostgreSQL（`pve.wf:5432 / fstore`）

连接：`host=pve.wf port=5432 dbname=fstore user=postgresql password=<FSTORE_DATABASE_PASSWORD>`。

| 表 | 类型 | 关键字段（实测） | 覆盖能力 | 备注 |
|----|------|----------------|----------|------|
| `base_infos` | 字典表 | `code varchar(20)` `name varchar(255)` `symbol varchar(30)` `asset_type smallint`（1=A 股 / 3=港股 / 10=指数 / 20=ETF） `stype` `ssdate` `zgb` `ltgb` `ltsz` `zsz` `roe` `ltbl` `hsgt` `bk` `z50/z52/z53/tags` `price` | **instruments（全部 asset_type）** | 有 `uk_code_asset_type` 唯一索引；适合 instruments 全量初装。 |
| `day_klines` | 日 K 线 | `code, ktype smallint`（101=day） `fq smallint`（0=不复权/1=前复权/2=后复权） `tdate date` `open/close/high/low numeric(15,4)` `cjl bigint`（成交量） `cje numeric(20,4)`（成交额） `zf/zdf/hsl numeric(10,4)`（涨跌幅/振幅/换手率） | **daily（含三档复权）** | 唯一索引 `(code, ktype, fq, tdate)`，可一次性查一段连续日期。实测 600519 该表**未写入 7 月数据**（最后一条 2025-10-31），与 §7 降级策略直接相关。 |
| `daily_markets` | 行情指标表 | `id, code, name, type, tdate, price, zgj, zdj, jrkpj, zrspj, ztj, dtj, jjia, zded, zdfd, zhfu, hslv, weibi, dsyl, jsyl, ttmsyl, sjlv, zf05/zf10/zf20/zf60, mggjj, mgsy, mgjzc, jlil, lbi, zystb, fzl, mlil, zfy/z30/...` 全 130+ 列（**实测** 列名见输出，列名多为拼音缩写） | **adj_factor 表（间接，zj/zf 字段）+ 部分 daily 增强字段**（市盈率/换手率等） | 已 `SELECT * WHERE code='600519'` 返回 0 行。该表**未初始化 600519 数据**，与 §7 降级一致。 |
| `chuquan_chuxi` | 除权除息 | `code, t_date timestamptz, pgbl numeric, pgjg numeric, pxbl numeric（派息比）, sgbl numeric（送股比）, cqcxtype bigint, zfbl, zfgs, zfjg` | **adj_factor 增量化反推** | 唯一索引 `(code, t_date)`。实测 600519 含 2025-12-19（除权除息）、2026-05-28（股本变化）、2026-06-26（除权除息）三行；cqcxtype=1 表除权除息。 |
| `financial_report_income_statement` | 利润表 | `code, t_date, name, industry_code, industry_name, total_oper_income, operate_income, operate_cost, total_oper_cost, operate_expense, operate_expense_ratio, operate_profit, operate_profit_ratio, total_profit, parent_net_profit, parent_net_profit_ratio, invest_income, finance_expense, sale_expense, manage_expense, notice_date` | **financial（income）** | 唯一 `(code, t_date)`。 |
| `financial_report_balance_sheet` | 资产负债表 | `code, t_date, name, report_type, industry, total_assets, total_assets_ratio, total_liabilities, total_liab_ratio, total_equity, monetary_funds, monetary_funds_ratio, accounts_receivable, accounts_receivable_ratio, inventory, inventory_ratio, advance_receivables, advance_receivable_ratio, short_loan, short_loan_ratio, accounts_payable, accounts_payable_ratio, notice_date` | **financial（balance_sheet）** | 唯一 `(code, t_date)`。 |
| `financial_report_cash_flow` | 现金流量表 | `code, t_date, name, net_cash_flow, net_cash_flow_ratio, net_cash_operate, net_cash_operate_ratio, net_cash_invest, net_cash_invest_ratio, net_cash_finance, net_cash_finance_ratio` | **financial（cash_flow）** | 唯一 `(code, t_date)`。 |
| `financial_report_annual` | 年度核心指标 | `code, t_date, name, report_year, report_type, quarter, basic_eps, bps, weight_avg_roe, net_profit, total_income, net_cash_flow, gross_margin, yoy_income, qoq_income, yoy_profit, qoq_profit, notice_date, update_date, assign_desc, industry, market, sec_code, sec_type, yo_y_income/yo_y_profit` | **financial（metrics 汇总）** | 提供 EPS / BPS / ROE / 净利 同比环比。 |
| `financial_report_quick` | 季度快报 | `code, t_date, report_type, basic_eps, bps, weight_avg_roe, net_profit, total_income, total_income_sq, net_profit_sq, yoy_income, qoq_income, yoy_profit, notice_date, industry, market, sec_code, sec_type, yo_y_income/yo_y_profit` | **financial（季度版 metrics）** | 实测 600519 该表 0 行（5月后才开始入）。§7 降级必备。 |
| `financial_report_forecast` | 业绩预告 | `code, t_date, forecast_type, forecast_content, change_reason, change_range_lower/upper, predict_profit_lower/upper, pre_year_profit, predict_finance, notice_date`（核心字段为文本 / 区间字符串） | **financial（forecast，可选输出）** | 唯一 `(code, t_date, COALESCE(notice_date,'1990-01-01'), forecast_type, content_hash)`。 |
| `pool_zt` | 涨停板池 | `code, name, t_date, amount, first_board_time varchar, seal_amount, turnover_rate, industry, board_count, last_board_time, circulation_value, exchange, price, total_value, break_board_count, change_rate, board_statistics` | **instruments 之外的「事件元数据」**（可选暴露为辅助列） | 唯一 `(code, t_date)`。 |
| `chengfen_gu` | 成分股（聚合） | `code`（指数/板块代码） `t_date` `name` `cfg json`（`[{"n":..,"c":..}, ...]`） `asset_type` | index/ETF 板块构成（**非 Provider 契约，§10 路线 4**） | 唯一定位组件用。 |
| `chengfen_gu_items` | 成分股（明细） | `index_code, index_name, stock_code, stock_name, weight numeric(8,4), join_date, t_date, asset_type` | 同上 | 唯一 `(index_code, stock_code, t_date, asset_type)`。 |

**未在本期接入的源表（避免越界）**：`realtime_quotes / realtime_stock_movement / realtime_stock_pankou`（DB 中是上一交易日的快照，不能做"实时"；接它会误导上层——见 §7.4）/ `fund_navs / fund_base_infos`（不在 AssetType 内）/ `hsgt_*`（北向资金，子流程而非日 K 级补齐）。

### 2.2 engine-data API（`http://192.168.5.99:8099`）

路由：`GET /api/v1/{dataset}/{code}[?date=YYYYMMDD&limit=N]`
实测 dataset 白名单（来自 `engine_data_proxy.go`）：`{day, wide, minutes, trans, xdxr, chips}`。

| dataset | URL 形式 | 实测响应（节选 600519） | 覆盖能力 | 关键字段 |
|---------|---------|------------------------|----------|---------|
| `day` | `/api/v1/day/600519?limit=2` | `{"dataset":"day","code":"600519","count":2,"rows":[{"date":"2026-06-30","datetime":"2026-06-30 15:00:00","open":1187,"close":1185.49,"high":1195.67,"low":1176,"volume":3960700,"amount":4684236288,"adjustment_count":0,"up":0,"down":0}],"source":"day/sh600/sh600519.csv","truncated":true}` | **daily 主源**（factor-aware） | date / datetime / open / high / low / close / volume / amount / adjustment_count / up / down；`source` 与 `cache_id` 元数据。`truncated=true` 时应继续翻页（参见 §3.3）。 |
| `wide` | `/api/v1/wide/600519?limit=2` | `{"date":"...","open":1187,"close":1185.49,"close_volume":80000,"open_turnz":0.69,"change_rate":-0.7924...,"last_close":1194.96,"inner_amount":2612731393,"inner_volume":2210050,"outer_amount":2071281008,"outer_volume":1750750,"close_unmatched":0,...}` | **daily 增强字段**（开盘/收盘成交量、内盘外盘、上笔收盘） | 包含 `day` 的全部 + `close_volume / open_volume / inner_* / outer_* / change_rate / last_close / *_unmatched`。 |
| `minutes` | `/api/v1/minutes/600519?date=20260615&limit=3` | `{"date":"20260615","count":3,"rows":[{"price":1284.66,"volume":1402}, ...]}` | **minute** | 仅 price / volume；时间戳要客户端重建（参见 fquant `generatedMinuteTime`）。 |
| `trans` | `/api/v1/trans/600519?date=20260615&limit=3` | `{"date":"20260615","rows":[{"time":"09:25","price":1292.7,"volume":70600,"amount":91264620,"order_count":313,"direction":2}]}` | **realtime 衍生**（逐笔，可聚合成"买/卖/中性"） | direction 实测为 **数字 `0/1/2`**：0=中性 / 1=买 / 2=卖——与 fquant side 的 `engineTradeDirection` 行为已对齐，参见 `engine_stock_data.go:283`。 |
| `xdxr` | `/api/v1/xdxr/600519?limit=3` | `{"date":"2025-12-19","fenhong":239.57,"fenshu":0,"houliutong":0,"houzongguben":0,"peigu":0,"peigujia":0,"qianliutong":0,"qianzongguben":0,"songzhuangu":0,"suogu":0,"xingquanjia":0,"category":1,"name":"除权除息"}` | **adj_factor 主源** | fenhong=每10股派现；fenshu=每10股送股；category=1=除权除息 / 5=股本变化；**唯一可计算 ex_factor 的来源**。 |
| `chips` | 实测超时（8s 内未返回） | — | 暂不接入（§1.2-N6） | 留 §10 路线 3。 |

通用响应结构（实测）：

```
{
  "dataset": "<dataset>",
  "code":    "<code>",
  "cache_id": "sh600519",
  "date":    "20260615",   // 仅在指定 ?date= 时填充；day/xdxr 留空
  "count":   <int>,
  "rows":    [ {...}, ... ],
  "source":  "<dataset>/<path>/<file>.csv",
  "truncated": true|false
}
```

**字段含义核对**（来源 `engine_stock_data.go`）：`up/down` 涨跌停统计；`adjustment_count` 累计复权次数；`*_volume` 主买主卖成交量；`*_unmatched` 撤单量。

### 2.3 moneyflow API（`http://pve.wf:8090`）

路由：`GET /api/v1/moneyflow/{daily,minute}/stocks?codes=600519[,...]&date=YYYY-MM-DD`
实测健康检查：`GET /api/v1/health → {"code":0,"message":"ok at 2026-07-02 07:15:43"}`。

| 路径 | 实测响应（一支 600519，2026-07-01） | 覆盖能力 |
|------|----------------------------------|---------|
| `/api/v1/moneyflow/daily/stocks?codes=600519&date=2026-07-01` | `{"code":0,"data":[{"code":"600519","source":"tdx","total":{"main_inflow":1865015178,"main_outflow":1739591624,"main_net":125423554,"total_inflow":1883634732,"total_outflow":1759986769,"total_net":123647963,"volume":3120300,"amount":3691020280}}]}` | **daily 资金流补字段 / financial 衍生** |
| `/api/v1/moneyflow/minute/stocks?codes=600519&date=2026-07-01` | `{"code":0,"data":[{"code":"600519","records":[{"Code":"600519","Name":"","TradeDate":"2026-07-01","BucketTime":"09:25","TotalAmount":21949860,"TotalVolume":18600,"InflowAmount":0,"OutflowAmount":0,"NetAmount":0,"SuperLargeInflow":0,...,"NetAmount":7207491（09:30）， ...]},"source":"tdx"}]}` | **minute 资金流（与 minute K 叠加）** |

**响应字段**（实测 + 对照 `fund_flow_moneyflow.go` 客户端定义）：

- **minute** 单点字段（PascalCase，`moneyflowMinuteRecord`）：`Code / Name / TradeDate / BucketTime / TotalAmount / TotalVolume / InflowAmount / OutflowAmount / NetAmount / SuperLarge{In,Out,Net} / Large{In,Out,Net} / Medium{In,Out,Net} / Small{In,Out,Net} / MainTraditional{In,Out,Net} / MainBroad{In,Out,Net} / Retail{In,Out,Net} / NeutralAmount / UnknownAmount / ValidCount / InvalidCount / Source`。
  - 注意双信号体系：`MainTraditional`（传统主力定义，超大单+大单）vs `MainBroad`（广义主力，超大单+大单+中单）；fquant 内部 `fund_flow_moneyflow.go` 也同时使用两者。
  - **首笔桶**（09:25）通常 `NetAmount=0`（集合竞价未拆方向）。

- **daily** 字段（首字母小写）：`code / source / total.{main_inflow, main_outflow, main_net, total_inflow, total_outflow, total_net, volume, amount}`——比 minute 简洁，仅给一个聚合"收盘快照"。

- 顶层 envelope：`code / message / data`。`code != 0` 视为错误（见 `fund_flow_moneyflow.go:458`）。

### 2.4 三源能力—契约映射初步结论

| 契约能力 | 主源 | 备份 | 关联章节 |
|---------|-----|------|---------|
| `instruments` | fstore `base_infos` | engine-data 不提供 / fquant HTTP / 缓存 | §4.3 |
| `daily` | engine-data `wide`（字段最全） | fstore `day_klines`（多档复权） / moneyflow daily（资金流补字段） | §4.4 |
| `adj_factor` | engine-data `xdxr` | fstore `chuquan_chuxi`（累计反推） | §4.5 |
| `minute` | engine-data `minutes` | fquant minute HTTP | §4.6 |
| `realtime` | **本期🔜**（路线 1） | engine-data `trans` + moneyflow minute 聚合 | §4.7 |
| `financial` | fstore `*_report_*` | engine-data 不提供 / fquant financials | §4.8 |

---

## 3. Provider 接口契约分析

### 3.1 base.py 完整方法签名

| 方法 | 签名 | 消费入口（实测） | 当前实现（TickFlowProvider） |
|------|------|------------------|-----------------------------|
| `get_instruments(asset_type)` | → `pl.DataFrame` 列：`symbol/name/code/exchange/asset_type/source`（INSTRUMENT_COLS） | `backend/scripts/test_fquant_provider.py`、service 层（未消费） | ✅ 三所轮询 |
| `get_daily(symbols, start, end, asset_type)` | → `pl.DataFrame` 列：`symbol/date/open/high/low/close/volume/amount`（DAILY_COLS） | `services/kline_sync.py`、`scripts/test_fquant_provider.py` | ✅ batch + 10000 count |
| `get_adj_factors(symbols, start, end, asset_type)` | → `pl.DataFrame` 列：`symbol/trade_date/ex_factor` | 现有 fquant_provider 返回空 | ✅ `ex_factors(...)` |
| `get_minute(symbols, start, end, asset_type, freq="1m")` | → `MINUTE_COLUMNS` | 现有 fquant 返回空 | ❌ 强制空（"service 层自管"） |
| `get_realtime(universes, symbols)` | → 未定 schema | 现有 fquant 返回空 | ✅ tencent 兜底 |

`MINUTE_COLUMNS = ["symbol","asset_type","source","datetime","open","high","low","close","volume","amount","freq"]`（来自 `schemas.py`）。

### 3.2 逐方法支持度评估（capability 标签释义）

- ✅ = 本期实现完整契约方法。
- ⚠️ = 方法实现存在但**会**降级或部分字段缺失；上层需感知"可能是近似值"。
- ❌ = 标记 `False`，provider 返回空 DataFrame（即"未实现但契约允许"路径）。
- 🔜 = 留到 §10 路线表，本期不做。

| 方法 | 现状 FQuantProvider | 本设计目标 | 关键设计点 |
|------|---------------------|-------------|------------|
| `get_instruments` | ✅（fquant HTTP 单资产类，已 PoC） | ✅（fstore 全 asset_type + 健康查 + 增量化） | 拉 `base_infos`，按 `asset_type` 过滤；缓存 24h TTL。 |
| `get_daily` | ✅（fquant HTTP，仅不复权） | ✅（engine-data `wide` 主 + fstore `day_klines` 复权档） | wide 字段最全但没有 `fq`；用 wide 做不复权主源，fq=0/1/2 走 day_klines。 |
| `get_adj_factors` | ❌（PoC） | ✅（engine-data `xdxr`） | xdxr 按事件逐项还原；返回 `trade_date / ex_factor`（以首个交易日 = 1.0 归一）。 |
| `get_minute` | ❌（PoC） | ✅（engine-data `minutes` + fquant HTTP 备份） | 客户端重建时间；返回 MINUTE_COLUMNS，**不补 realtime 衍生**（见 §4.6）。 |
| `get_realtime` | ❌（PoC） | 🔜（§10 路线 1） | 本期保持 ❌；不在 cap.matrix 上拔高，避免误导上层。 |
| `financial` 字段未在 base.py 暴露方法（只声明 capability） | ❌（PoC cap.financial=False） | ✅ `capabilities.financial=True`（**契约只声明 booleans，未列入方法签名**；本期通过新增 service 适配点直接消费 `fquant_provider.list_financials(...)` 等扩展方法） | 契约**未要求**新增方法；能力声明与扩展方法配套，详见 §3.4。 |
| `capabilities.instruments / daily / adj_factor / minute` | 升级 | ✅ 全部 True | 与 cap 字段一致。 |
| `capabilities.realtime` | ❌ | ❌（与 §10 路线 1） | 保持 PoC。 |
| `capabilities.financial` | ❌ | ✅ | 唯一新增 True。 |

### 3.3 约束：`base.py` 不可改

- 五个接口方法的**入参/出参 schema** 不允许改。
- 任何新增 service 层消费点都要通过**扩展方法**（非契约方法）走 `provider.list_financials(...)` / `provider.get_moneyflow_daily(...)` 等；扩展方法通过 `Protocol.isinstance` 不强制（动态 duck-typing，poi：`TickFlowProvider` 没有这些方法也无影响，因为上层只在 provider 名是 `"fquant"` 时调用）。

### 3.4 扩展方法（不在 base.py 中，但属于 FQuantProvider 类）

以下**仅在 `FQuantProvider` 实例上可用**，tickflow provider 没有；上层在做"fquant 专项能力"时显式 `isinstance` / `name == "fquant"` 判断。

| 方法 | 用途 | 顶层入口设计 |
|------|------|--------------|
| `get_financial(symbol, table)` | 表名 ∈ {income, balance_sheet, cash_flow, annual, quick, forecast} → 归一 df | 复用 fstore §2.1 表 |
| `get_moneyflow_daily(symbols, date)` | 资金流日级 → `symbol/date/main_net/total_net/...` | moneyflow API §2.3 |
| `get_moneyflow_minute(symbols, date)` | 资金流分钟 → 与 minute K 合并 | moneyflow + engine-data minutes |
| `get_transactions(symbol, date)` | 逐笔 → 用于上层计算日内方向分布 | engine-data `trans` |
| `get_corp_action(symbol, start, end)` | 公司行动一览 → 与 adj_factor 互补 | fstore `chuquan_chuxi` |

设计理由：finance/moneyflow/transaction/corp-action 这些数据**不在** Provider 契约方法里，不能强行塞进 `get_daily` 输出（否则上层会被迫消费非 OHLCV 字段）。扩展方法属于 FQuantProvider 内部实现，服务层在调用前先 `assert provider.name == "fquant"`。

### 3.5 capabilities 声明（伪代码/接口骨架）

```python
# 仅用于说明，文档中不输出可运行实现
class FQuantProvider:
    name = "fquant"

    capabilities = ProviderCapabilities(
        instruments=True,   # ✅ fstore base_infos
        daily=True,         # ✅ engine-data wide + day_klines
        adj_factor=True,    # ✅ engine-data xdxr
        minute=True,        # ✅ engine-data minutes
        realtime=False,     # 🔜 留 §10
        financial=True,     # ✅ fstore *_report_*
    )
```

---

## 4. 模块设计

### 4.1 顶层职责分层

```
backend/app/data_providers/
├── base.py                  # (N1) 不修改
├── schemas.py               # (N1) 不修改
├── normalizer.py            # (N1) 不修改
├── registry.py              # (N1) 不修改
├── tickflow_provider.py     # (N1) 不修改
├── fquant_provider.py       # PoC 状态保留（本期不动）
└── fquant_provider_v2.py    # 本设计新模块（或就地替换 fquant_provider.py，决策见 §10 R2）

backend/app/data_providers/fquant/
├── __init__.py
├── symbols.py               # 符号归一（split_symbol 等；可复用 PoC 中的工具函数）
├── fstore_client.py         # psycopg 连接 + 查询函数
├── engine_data_client.py    # engine-data API 客户端（含 day/wide/minutes/trans/xdxr）
├── moneyflow_client.py      # moneyflow API 客户端
├── mapping.py               # 上游字段 → 内部 schema 的转换函数（按表/按 dataset 各一个）
├── adj_factor.py            # xdxr 事件 → 累积 ex_factor 计算
└── fallback.py              # 三源降级策略表（§7）
```

> **依赖选择**：
> - HTTP 用 `httpx`（PoC 已用）。`Client` 实例共享一个连接池，启停用 lifespan 控制。
> - DB 用 `psycopg[binary]` v3.x（同步），配置走 settings（§6）。
> - 日志用 `logging.getLogger(__name__)`，命名 `app.data_providers.fquant.*`。
> - 不引入 `pandas / sqlalchemy / asyncpg`（N5）。

### 4.2 FQuantProvider 类骨架（接口骨架，不含实现）

```python
# 伪代码/接口骨架
class FQuantProvider:
    name = "fquant"
    capabilities = ProviderCapabilities(
        instruments=True, daily=True, adj_factor=True,
        minute=True, realtime=False, financial=True,
    )

    def __init__(self, *, fstore: FStoreClient, engine: EngineDataClient, moneyflow: MoneyflowClient):
        self._fstore = fstore
        self._engine = engine
        self._moneyflow = moneyflow
        self._instruments_cache: dict[str, pl.DataFrame] = {}  # asset_type → df
        self._instruments_cache_ts: dict[str, datetime] = {}

    # ---- 契约方法 ----
    def get_instruments(self, asset_type) -> pl.DataFrame: ...
    def get_daily(self, symbols, start_time, end_time, asset_type) -> pl.DataFrame: ...
    def get_adj_factors(self, symbols, start_time, end_time, asset_type) -> pl.DataFrame: ...
    def get_minute(self, symbols, start_time, end_time, asset_type, freq="1m") -> pl.DataFrame: ...
    def get_realtime(self, universes=None, symbols=None) -> pl.DataFrame:
        # 本期保持空
        return pl.DataFrame()

    # ---- 扩展方法 (§3.4) ----
    def get_financial(self, symbol: str, table: str) -> pl.DataFrame: ...
    def get_moneyflow_daily(self, symbols: list[str], date: datetime) -> pl.DataFrame: ...
    def get_moneyflow_minute(self, symbols: list[str], date: datetime) -> pl.DataFrame: ...
    def get_transactions(self, symbol: str, date: datetime) -> pl.DataFrame: ...
    def get_corp_action(self, symbol: str, start: datetime, end: datetime) -> pl.DataFrame: ...
```

### 4.3 `get_instruments(asset_type)` 实现策略

**主源**：`fstore.base_infos`
```sql
-- 伪 SQL，asset_type 传入：1=A股 / 3=港股 / 10=指数 / 20=ETF
SELECT code, name, symbol, asset_type, stype, ssdate, zgb, ltgb, zsz, ltsz, hsgt, bk
FROM base_infos
WHERE asset_type = $1
  AND code NOT IN ('bjx4-templates', ...)   -- 按需过滤
ORDER BY code;
```

归一映射（调用 `normalizer.normalize_instruments`，复用 PoC 工具函数 `code_and_market_to_symbol`）：
- 内部 symbol 列统一为 `"600519.SH" / "000001.SZ" / "00700.HK" / "000300.INDEX" / "510330.ETF"`。
- exchange 字段由 `code` 前缀映射（60/68/9→SH；0/30/20→SZ；8/4→BJ；其它保持空）。

**缓存**：24h TTL；键 `(asset_type)`；过期后下次请求异步刷新一次（不阻塞当前请求）。

**降级**：DB 连接失败 → 退到 fquant `GET /api/metadata/stocks?markets=...`（PoC 已实现）。`asset_type` 不为 stock 时退化为空集。

### 4.4 `get_daily(symbols, start, end, asset_type)` 实现策略

**双源融合**（伪代码/接口骨架）：
```
1) 解析 symbols → list[(code, asset_type_internal)]
2) two parallel calls (async-style, sync code):
   a) engine.wide(code) → df_wide                    # 主源，字段最全（含 inner/outer/last_close）
   b) fstore.day_klines(code, start, end, fq=0/1/2)  # 多档复权备份
3) 按 (symbol, date) 做 outer-join，wide 字段优先，fq 用 day_klines 填补
4) 归一化到 DAILY_COLS = [symbol, asset_type, source, date, open, high, low, close, volume, amount, pre_close, change_pct]
   - symbol → split_symbol 还原
   - change_pct ← round((close - pre_close)/pre_close*100, 4) where pre_close 来自 wide.last_close
   - amount 从 wide 拿；否则用 day_klines.cje
   - source = self.name
5) 过 filter_halt_days（同 normalizer 当前实现）
6) 按 start_time/end_time 截断
```

**响应字段映射**（实测 → 内部）：

| 内部列 | wide 来源 | day_klines 兜底 | 备注 |
|-------|-----------|----------------|-----|
| `open` | `rows[].open` | `open` | numeric(15,4) → Float64 |
| `high` | `rows[].high` | `high` | |
| `low` | `rows[].low` | `low` | |
| `close` | `rows[].close` | `close` | |
| `volume` | `rows[].volume` | `cjl` | bigint → Float64 |
| `amount` | `rows[].amount` | `cje` | numeric → Float64 |
| `pre_close` | `rows[].last_close` | — | 仅 wide 提供 |
| `change_pct` | `rows[].change_rate`（已经乘 100） | `zf` | 优先 wide |
| `date` | `rows[].date` | `tdate` | |

### 4.5 `get_adj_factors(symbols, start, end, asset_type)` 实现策略

**主源**：engine-data `xdxr`。每个事件提供 `fenhong/fenshu/songzhuangu/peigu/qianzongguben/houzongguben/...`。
```
1) GET /api/v1/xdxr/{code}  → rows[]
2) 累乘公式（首日=1.0）：
   for event in ascending date:
       if event.fenhong > 0:    # 现金分红（per 10 股）
           ex_factor *= 1 - (fenhong/10) / (close_at_event - fenhong/10 ... )
         # 简化公式：本设计采用
         #   adj = 1 - fenhong/10 / (close_before_event)
       if event.fenshu  > 0:    # 每 10 股送 fenshu 股
           adj *= 10 / (10 + fenshu)
       if event.songzhuangu > 0: ...
       if event.peigu > 0:     # 配股（一般不调整 ex_factor）
           ...
   return trade_date / adj_factor
3) 截断 [start_time, end_time]
4) df 形如 [symbol, trade_date, ex_factor]，与 normalizer 兼容（normalize_adj_factors 接受 ex_factor 而非 adj_factor 命名）
```

**降级**（fquant 端不可用或 xdxr 报 404）→ `chuquan_chuxi` `pxbl`（派息比，每10股）反推：`adj = 1 - pxbl/10 / pre_close`。`cqcxtype=1` 视为除权除息事件。

### 4.6 `get_minute(symbols, start, end, asset_type, freq)` 实现策略

**主源**：engine-data `minutes`
```
1) 对每个 symbol GET /api/v1/minutes/{code}?date=YYYYMMDD&limit=240
   - date 推断：end_time 或最新交易日（从 wide 的 max(date) 取）
2) rows[].{price, volume} → 客户端重建时间戳（沿用 engine_stock_data.go:201）
   - index 0..119 → 09:31..11:30
   - index 120..239 → 13:01..15:00
3) 拼接多日 → MINUTE_COLUMNS：[symbol, asset_type, source, datetime, open, high, low, close, volume, amount, freq]
   - 每桶 open=price, high=price, low=price, close=price, volume=volume
   - amount=close*volume（best-effort；上层若需要精确值另走 get_moneyflow_minute 合并）
4) freq="1m" 默认；其它 freq 通过 resample（本期不支持，仅日志警告并返回 1m 重采样结果）
```

**API 不响应**：退到 fquant minute HTTP（PoC 提到的 `/api/stocks/{market}/{code}/minute`）。

### 4.7 `get_realtime` 本期策略

直接返回 `pl.DataFrame()`；capabilities.realtime 保持 False。`engine-data.trans` 已经在 fquant 服务内整合出 `engineStockTickSummary`（参见 `engine_stock_data.go:120`），但要构建 `Standard RealtimeQuote` 还需要 tencent/tdex 兜底——见 §10 路线 1。

### 4.8 `get_financial(symbol, table)` 扩展方法

```
table ∈ {"income", "balance_sheet", "cash_flow", "annual", "quick", "forecast"}
↓
映射到 fstore 表名（见 §2.1）
↓
执行 SELECT，按 t_date DESC LIMIT N，bigint/numeric → Float64
↓
归一列：symbol / t_date / (period_type) / 各指标列 + notice_date / industry
```

输出 schema（以 income 为例）：
| 列 | 来源 |
|----|------|
| `symbol` | 外部传入 |
| `t_date` | `t_date` |
| `industry_code` | `industry_code` |
| `industry_name` | `industry_name` |
| `total_oper_income` | `total_oper_income` |
| `parent_net_profit` | `parent_net_profit` |
| ... | ... |
| `notice_date` | `notice_date` |

**降级**：DB 不可达 → 返回空 df（PoC 风格，绝不抛）。

### 4.9 `get_moneyflow_daily / get_moneyflow_minute` 扩展方法

**daily**：单笔请求 `GET /api/v1/moneyflow/daily/stocks?codes=...&date=...` → 拆 envelope → 输出 `[symbol, date, source] + total.{main_net, total_net, ...}`。

**minute**：每只股票单独请求（实测一次拿多 stocks 也能成 200KB+；建议**走批请求 1 次**而非循环 N 次）。schema：
| 列 | 来源 |
|----|------|
| `symbol` | `code` |
| `trade_date` | `TradeDate` |
| `bucket_time` | `BucketTime` |
| `total_amount` | `TotalAmount` |
| `net_amount` | `NetAmount` |
| `main_traditional_net` | `MainTraditionalNet` |
| `main_broad_net` | `MainBroadNet` |
| `large_net` | `LargeNet` |
| `super_large_net` | `SuperLargeNet` |
| `medium_net` | `MediumNet` |
| `small_net` | `SmallNet` |
| `neutral_amount` | `NeutralAmount` |

**降级**：API 返回 code !=0 或超时 → 回退到 fstore 内 `daily_markets.zljlr / cddjlr / ddjlr` 收盘后聚合（来自 `fund_flow.go:fundFlowValueSQL`）。

### 4.10 依赖选择（最终）

| 组件 | 库 | 说明 |
|------|----|-----|
| HTTP | `httpx` (PoC 已引入) | 同步客户端 + 共享连接池 |
| Postgres | `psycopg[binary]` v3.x | 同步；连接池用 `psycopg_pool.ConnectionPool` 或自带 `with conn_pool.connection()` |
| Polars | 已在依赖中 | 字段归一 |

---

## 5. 数据映射（符号归一 + 字段对照）

### 5.1 符号归一总图

> 三源**异构代码口径**实测：

| 上游 | 例子 | 字段 | 归一后（内部） |
|------|-----|-----|-------------|
| fstore `base_infos.code` | `600519` | 不带市场后缀 | `600519.SH` |
| fstore `base_infos.symbol` | `sh600519` | **带 sh 前缀**；为反向引用，**不用作主**，主靠 `code + asset_type` | `600519.SH` |
| engine-data URL 段 | `600519` | 同 fstore `code` | `600519.SH` |
| engine-data `cache_id` | `sh600519` | 同 `symbol`，标记用 | — |
| engine-data `code` 响应字段 | `600519` | 同 | — |
| moneyflow `Code / code` | `600519` | 同 | — |
| fquant old `_SUFFIX_MAP` | — | 输入 `"600519.SH"` 拆 `(code, suffix)` | 内部 routing |

**统一做法**（伪代码/接口骨架，调用 PoC 已存在的 `split_symbol`）：
```
internal_symbol = code_and_market_to_symbol(code, asset_type)
# code="600519" asset_type=1 → "600519.SH"
# code="00700"  asset_type=3 → "00700.HK"
# code="000300" asset_type=10 → "000300.INDEX"
# code="510330" asset_type=20 → "510330.ETF"
```

**asset_type 推断规则**（当输入只有 6 位 code 时）：
- `5/6/9/688` 开头 → SH，A 股
- `0/30/20/2/3` 开头 → SZ，A 股
- `4/8/92` 开头 → BJ，A 股
- `00700/0XXXX` 5 位左零 → HK
- `000300/510` → INDEX/ETF 由 fstore `base_infos.asset_type` 决定

> §5.1 全部表内不写实现，使用 PoC `fquant_provider.py:66-121` 现成的工具函数（符号归一零改动）。

### 5.2 daily 字段对照表（实测）

| 内部列 (DAILY_COLS) | engine-data `wide` | engine-data `day` | fstore `day_klines` | fstore `daily_markets` | moneyflow daily `total` |
|---------------------|-------------------|--------------------|---------------------|---------------------|--------------------------|
| `symbol` | — | — | — | — | — （构造）|
| `date` | `rows[].date` | `rows[].date` | `tdate` | `tdate` | —（URL `date=`）|
| `open` | `open` | `open` | `open` | `jrkpj` | — |
| `high` | `high` | `high` | `high` | `zgj` | — |
| `low` | `low` | `low` | `low` | `zdj` | — |
| `close` | `close` | `close` | `close` | `price` | — |
| `volume` | `volume` | `volume` | `cjl` | `cjl` | `volume` |
| `amount` | `amount` | `amount` | `cje` | `cje` | `amount` |
| `pre_close` | `last_close` | ❌ | ❌ | `zrspj` | — |
| `change_pct` | `change_rate` | ❌ | `zf` | ❌ | — |
| `source` | `"fquant:engine-data:/api/v1/wide"` | `"fquant:engine-data:/api/v1/day"` | `"fquant:fstore:day_klines"` | `"fquant:fstore:daily_markets"` | `"fquant:moneyflow:daily"` |

> `change_rate` 实测返回值（如 `0.6343368564897256`）已经是百分比（不是 0.0063）；需扫描单位再做归一，**本期文档约定**：wide 取到时**不再次乘 100**，原样入库。

### 5.3 adj_factor 字段对照表（实测）

| 内部列 (ADJ_FACTOR_COLS) | engine-data `xdxr` | fstore `chuquan_chuxi` |
|---------------------------|---------------------|------------------------|
| `symbol` | URL 段 | `code` |
| `trade_date` | `date` | `t_date::date` |
| `ex_factor` | **重算**（基于 fenhong+fenshu+peigu） | **重算**（基于 `pxbl`） |
| `category`（扩展方法 get_corp_action 输出，非 DF cols） | `category`（1=除权除息,5=股本变化）| `cqcxtype` |
| `dividend_per_10` | `fenhong` | `pxbl` |
| `bonus_share_per_10` | `fenshu` / `songzhuangu` | `sgbl` |

### 5.4 minute 字段对照表（实测）

| 内部列 (MINUTE_COLUMNS) | engine-data `minutes` | fquant minute HTTP |
|--------------------------|------------------------|---------------------|
| `symbol` | URL 段 | route 段 |
| `datetime` | 客户端重建（`generatedMinuteTime(index)`） | 通常已含 |
| `open/high/low/close` | 全等于 `price` | 同 |
| `volume` | `volume` | 同 |
| `amount` | `= price*volume` | 同 |
| `freq` | `"1m"` | 同 |

### 5.5 instrument 字段对照表（实测）

| 内部列 (INSTRUMENT_COLS) | fstore `base_infos` | 备注 |
|---------------------------|--------------------|------|
| `symbol` | 归一后构造 | 见 §5.1 |
| `name` | `name` | |
| `code` | `code` | 不带后缀 |
| `exchange` | 派生 | 见 §4.3 |
| `asset_type` | `asset_type`（数字）→ 字符串 | 1/3/10/20 → stock/stock/index/etf |
| `source` | `self.name` | "fquant" |
| `list_date`（扩展） | `ssdate` | PoC schema 没有该列，本期不输出避免改 schema |
| `status`（扩展） | `stype / hsgt` 派生 | `hsgt=1` → "沪股通"，其它 None |

> `INSTRUMENT_COLUMNS = [symbol, name, exchange, asset_type, source, list_date, status]`（来自 PoC `normalizer.py`）。这两列不在 `INSTRUMENT_COLS`（注意大小写）里，输出时归一器会用 `select(INSTRUMENT_COLS)` 丢弃 `list_date / status`——**这意味着即使传入也不能进 df**。本期设计**不暴露**这两个字段，保持与 normalizer 一致。

### 5.6 financial 字段对照表（实测，按表）

> 内部 df 列统一：`symbol, t_date, ...<source cols>..., notice_date`，`source="fquant:fstore:<table>"`。

| table | 来源 fstore 表 | 关键列 |
|-------|---------------|--------|
| `income` | `financial_report_income_statement` | `total_oper_income, operate_income, operate_cost, total_oper_cost, operate_expense, operate_expense_ratio, operate_profit, operate_profit_ratio, total_profit, parent_net_profit, parent_net_profit_ratio, invest_income, finance_expense, sale_expense, manage_expense, industry_code, industry_name` |
| `balance_sheet` | `financial_report_balance_sheet` | `total_assets, total_assets_ratio, total_liabilities, total_liab_ratio, total_equity, monetary_funds, monetary_funds_ratio, accounts_receivable, accounts_receivable_ratio, inventory, inventory_ratio, advance_receivables, advance_receivable_ratio, short_loan, short_loan_ratio, accounts_payable, accounts_payable_ratio, report_type, industry` |
| `cash_flow` | `financial_report_cash_flow` | `net_cash_flow, net_cash_flow_ratio, net_cash_operate, net_cash_operate_ratio, net_cash_invest, net_cash_invest_ratio, net_cash_finance, net_cash_finance_ratio` |
| `annual` | `financial_report_annual` | `report_year, report_type, quarter, basic_eps, bps, weight_avg_roe, net_profit, total_income, gross_margin, yoy_income, qoq_income, yoy_profit, qoq_profit, assign_desc, industry, market, sec_code, sec_type, yo_y_income/yo_y_profit` |
| `quick` | `financial_report_quick` | 同 annual 子集（无 `report_year/quarter`）；用 `report_type` 区分 |
| `forecast` | `financial_report_forecast` | `forecast_type, forecast_content, change_reason, change_range_lower/upper, predict_profit_lower/upper, pre_year_profit, predict_finance` |

### 5.7 moneyflow 字段对照表（实测）

| 内部列 | moneyflow minute | moneyflow daily |
|--------|------------------|-----------------|
| `symbol` | `Code` | `code` |
| `trade_date` | `TradeDate` | URL `date=YYYY-MM-DD` |
| `bucket_time` | `BucketTime` | —（single bucket daily） |
| `total_amount` | `TotalAmount` | `total.amount` |
| `net_amount` | `NetAmount` | `total.total_net` |
| `main_traditional_net` | `MainTraditionalNet` | —（无对应；用 `main_net` 代替作为近似） |
| `main_broad_net` | `MainBroadNet` | `total.main_net` |
| `large_net` | `LargeNet` | — |
| `super_large_net` | `SuperLargeNet` | — |
| `medium_net` | `MediumNet` | — |
| `small_net` | `SmallNet` | — |
| `neutral_amount` | `NeutralAmount` | — |
| `valid_count` | `ValidCount` | — |
| `source` | 构造 `"fquant:moneyflow:minute"` | 构造 `"fquant:moneyflow:daily"` |

---

## 6. 配置（环境变量清单）

### 6.1 必备变量

| 变量 | 默认 | 必填 | 说明 |
|------|------|------|------|
| `FSTORE_DATABASE_HOST` | `pve.wf` | 否 | fstore PG host |
| `FSTORE_DATABASE_PORT` | `5432` | 否 | |
| `FSTORE_DATABASE_USERNAME` | `postgresql` | 否 | |
| `FSTORE_DATABASE_PASSWORD` | — | **是** | 来自 `fquant/.env`，**用共享 secrets**，不写到本仓库 |
| `FSTORE_DATABASE_DATABASE` | `fstore` | 否 | |
| `FSTORE_DATABASE_POOL_MIN` | `1` | 否 | 连接池下界 |
| `FSTORE_DATABASE_POOL_MAX` | `4` | 否 | 连接池上界 |
| `FSTORE_DATABASE_TIMEOUT` | `10`（秒） | 否 | 语句超时 |
| `FQUANT_ENGINE_DATA_BASE_URL` | `http://192.168.5.99:8099` | 否 | |
| `FQUANT_ENGINE_DATA_TIMEOUT` | `8`（秒） | 否 | |
| `FQUANT_ENGINE_DATA_LIMIT_MAX` | `5000`（分钟） / `20000`（day/wide/xdxr） | 否 | 每次请求的 limit 上界（实测 fquant server.go `engineDataFullTradeLimit=20000`） |
| `FQUANT_MONEYFLOW_BASE_URL` | `http://pve.wf:8090` | 否 | |
| `FQUANT_MONEYFLOW_TIMEOUT` | `10`（秒） | 否 | |
| `FQUANT_MONEYFLOW_CACHE_TTL` | `30`（秒） | 否 | 客户端短 TTL 缓存，参考 fquant server 默认 |
| `FQUANT_INSTRUMENTS_CACHE_TTL` | `86400`（24h） | 否 | instruments df 缓存 TTL |
| `FQUANT_HTTP_PROXY` | 空 | 否 | 若需通过代理；与 fquant `isSensitiveProxyQueryKey` 一律禁止 key/secret 类参数 |
| `FQUANT_LOG_LEVEL` | `INFO` | 否 | `app.data_providers.fquant.*` logger level |

### 6.2 保留兼容（PoC 已有）

| 变量 | 默认 | 说明 |
|------|------|------|
| `FQUANT_BASE_URL` | `http://localhost:8088` | 仅旧 PoC fallback 使用；本期保留作为"fquant HTTP 兜底"的连接 |
| `FQUANT_TIMEOUT` | `10` | |

### 6.3 secrets 来源约定

- `FSTORE_DATABASE_PASSWORD` 仅在运行时由 settings.py 读取 `fquant/.env`（不在本仓库提交）。PoC 文件 `app/data_providers/fquant_provider.py` 已通过 `os.getenv` 取；本期新增 client 一致处理。
- 不要把数据库密码硬编码到 `app/data_providers/fquant/*.py`。
- 文档不输出密码原文，只输出变量名（与 §6.1 表格一致）。

### 6.4 配置加载顺序（伪代码/接口骨架）

```
1) Django settings / Pydantic Settings 读取 os.environ
2) `_load_fstore_dsn()` 把上述变量拼成 libpq DSN：host=... port=... user=... password=... dbname=...
3) 缺失 password → 启动失败日志 "FSTORE_DATABASE_PASSWORD not set; FQuantProvider disabled"，同时让 capabilities.financial=False（与现实情况对齐）
4) engine-data / moneyflow base URL 解析失败 → 单实例 degrade，不影响其它源
```

---

## 7. 错误降级策略

### 7.1 降级矩阵

| 方法/扩展 | 主源 | 备份 1 | 备份 2 | 最坏兜底 |
|----------|------|--------|--------|----------|
| `get_instruments` | fstore `base_infos` | fquant `/api/metadata/stocks` | — | 空 df |
| `get_daily` | engine-data `wide` | fstore `day_klines` | moneyflow daily（缺 OHLCV，但能补 amount/volume） | 空 df |
| `get_adj_factors` | engine-data `xdxr` | fstore `chuquan_chuxi` | fquant （无） | 空 df |
| `get_minute` | engine-data `minutes` | fquant `/api/stocks/{m}/{c}/minute` | — | 空 df |
| `get_realtime` | 🔜 路线 1 | — | — | 空 df |
| `get_financial` | fstore `*_report_*` | — | — | 空 df；warning 日志 |
| `get_moneyflow_daily` | moneyflow `/moneyflow/daily/stocks` | fstore `daily_markets.zljlr` 收盘后聚合 | — | 空 df |
| `get_moneyflow_minute` | moneyflow `/moneyflow/minute/stocks` | fquant 内部"fstore 收盘快照" | — | 空 df |
| `get_transactions` | engine-data `trans` | — | — | 空 df |
| `get_corp_action` | fstore `chuquan_chuxi` | engine-data `xdxr` | — | 空 df |

### 7.2 各级失败的具体行为

| 级别 | 条件 | 行为 |
|------|------|------|
| L0：无错误 | 正常响应 | 直接归一返回 |
| L1：单股票缺数 | 单 symbol 4xx/5xx，或 rows=[] | 跳过该 symbol，warning 日志，继续返回 |
| L2：源连接失败 | 连接超时 / DNS / connection refused | warning 日志，**自动切换备份源**，重试最多 1 次 |
| L3：源响应异常 envelope | `moneyflow.code != 0` 或 envelope 解析失败 | warning 日志，跳过该方法所有 symbols |
| L4：所有源失败 | 备份链全部 L2/L3 | 返回 `pl.DataFrame()`，error 日志（loglevel=ERROR） |
| L5：致命（启动时） | DB password 缺失、psycopg import 失败 | 进程层 error → capabilities.financial=False，**不抛** |

### 7.3 关键场景示例

**场景 A：real-time 09:30 调用 daily**
- 主 `wide` HTTP 200 但 `truncated=true`：发起第二轮 `wide` 翻页（拼 `?limit=` 翻到 `truncated=false`）——保留 `engine-data` 的 *分页协议*。
- 主 `wide` 超时 8s：退 fstore `day_klines`，但 fstore 实测 600519 在 2026-07 之后**无数据**（最后一条 2025-10-31），再次降级——返回空 df + 日志；上层调用方应感知"今天 daily 暂缺"。

**场景 B：周末调用 get_transactions**
- 传入日期为非交易日 → engine-data 返回空 rows；
- 失败语义：空 df 而非抛错；上层若要严格判 available，再单独走 `engine.latestEngineTradeDate`。

**场景 C：fstore DB 偶发中断（mid-day）**
- 关键路径 `get_financial` / `get_instruments` 触发 L2；
- 备份（fquant HTTP）有时延 1-2s；上层调用方应该已经在线程池等待，并接受 503。

### 7.4 实测已知的"假阳性"陷阱

- `daily_markets` 在 600519 实测 0 行——**不能用它当 daily 主源**，因为它的覆盖不完整（推测依赖新浪行情源，常年缺数）。
- `engine-data.chips` 实测超时——明确不允许走 L0 自动等待，要**直接走 L2 备份链 + warning**，避免上层阻塞。
- `moneyflow.minute` 全 A 股覆盖良好，但 09:25 桶 `NetAmount=0`、RetailNet=0、InflowAmount=0——上层做"主力净流入"计算时要跳过首桶（按业务约定，§10 路线 5）。

---

## 8. 测试方案

> 本期不出实现代码，因此本节只列测试规划而非测试实现。后续接入 §10 路线时由实现 agent 落地。

### 8.1 单元测试（`backend/tests/data_providers/test_fquant_provider.py`）

- **符号归一**：4 类后缀 × 6 种 code 前缀组合（共 24 用例），对照 §5.1。
- **capabilities 声明**：assert 6 个 bool 字段与 §3.5 一致；并且 `financial=True`（新增点）。
- **空输入**：`symbols=[]` → 空 df；`asset_type="index"`（fstore 支持，但 PoC 老代码不支持）→ 仍然能查到 base_infos 行。

### 8.2 集成测试（连真实三源，标 `integration`）

依赖：
- 测试用 4 个测试 symbol：`"600519.SH", "000001.SZ", "000858.SZ", "00700.HK"`。
- 起始/结束日期相对 `datetime.now()` 自动计算（避免硬编码）。
- 通过 fixture 注入 mock `fstore/engine/moneyflow` 的 client。
- 三源任一不可达 → `pytest.skip("source unavailable")` 而不是 fail。

覆盖：

| 用例 | 期望 |
|------|-----|
| `get_instruments("stock")` | df 行数 ≥ 4000；symbol 列格式全部 6 位 + `.SH/.SZ/.BJ/.HK` |
| `get_daily(["600519.SH"], t-30, t, "stock")` | df 行数 ≤ 30；列 = DAILY_COLS；`amount` / `volume` 均不为空 |
| `get_daily(["600519.SH"], t-30, t, "stock")` 主源 wide 503 模拟 | 退 day_klines；warning 日志；df 仍非空（除非 fstore 也没有） |
| `get_adj_factors(["600519.SH"], t-365, t, "stock")` | 含至少 1 行（除权除息事件）；ex_factor 单调非增 |
| `get_minute(["600519.SH"], 2026-07-01 00:00, 2026-07-01 23:59, "stock")` | 240 行；datetime 间隔 60s |
| `get_financial("600519", "income")` | 列含 `parent_net_profit / total_oper_income`；行按 `t_date` DESC |
| `get_moneyflow_daily(["600519.SH"], today)` | 单行；主源返回 → 字段非 0 |
| `get_moneyflow_minute(["600519.SH"], today)` | 240 行；09:25 桶净流入允许为 0，但其它桶必有非 0 |
| `get_transactions("600519.SH", today)` | rows 计数 ≥ 1；direction ∈ {0,1,2} |

### 8.3 兼容性测试（与 TickFlowProvider 同源对照）

- 对同一段 `(symbols, start, end, asset_type)` 同时跑 `get_provider("fquant")` 和 `get_provider("tickflow")`，断言：
  - 行集合形状一致（允许 fquant 字段更多，但 DAILY_COLS 子集必须严格相等）。
  - close 价格相对误差 ≤ 1e-4（fquant 走不复权，tickflow 走 `adjust=none`，应当全等）。
  - 任一缺失/不等 → test fail 并打印 diff first 5 rows。

### 8.4 故障注入（chaos）

| 场景 | 注入方式 | 期望 |
|------|---------|------|
| fstore 连不上 | monkeypatch `_fstore.connection` 抛 `OperationalError` | `get_financial()` 返回空 df，error 日志 |
| engine-data 5xx | mock response 503 | `get_daily()` 退 day_klines |
| moneyflow envelope code=99 | mock `{code:99}` | warning + fallback 到 daily_markets 聚合 |
| 所有源全挂 | 同时 inject | L5 + service 层感知 |

### 8.5 性能/规模化

| 指标 | 期望 |
|------|-----|
| 单股票 `get_daily` （30 天 wide + 30 天 day_klines） | P95 < 500ms |
| 50 股票 batch `get_daily` | P95 < 4s（连接池起作用） |
| `get_instruments("stock")` 缓存命中 | < 5ms |
| `get_moneyflow_minute` 1 symbol/1 day | < 1s |

### 8.6 验收 checklist（任务 §6）

1. ✅ DESIGN.md 文件存在：`backend/docs/FQUANT_PROVIDER_DESIGN.md`。
2. ✅ 10 个章节齐全：见 §1..§10；每节首行标记（如 "§2 三个上游源能力清单"）。
3. ✅ 字段映射实测：本设计所有"内部列 → 上游字段"均带 `实测` 标签或引用 `engine-data` / `moneyflow` / `fstore.` 表实测输出。
4. ✅ 无实现代码：本文档出现的代码均为**伪代码/接口骨架**，关键代码块已在 §3.5 / §4.2 / §4.4 / §6.4 明确标注；未创建任何 `.py` 文件。
5. ✅ 现有文件未改：见 §10 末尾"已读不改"清单。

---

## 9. 覆盖矩阵（财务不再是缺口）

> 行 = `MarketDataProvider` 契约能力 + FQuantProvider 扩展方法。
> 列 = 当前 TickFlowProvider / PoC FQuantProvider / **本设计**。
> 单元格标签：✅=完全覆盖 / ⚠️=部分覆盖（带备注） / ❌=未覆盖 / 🔜=路线项目。

| 能力 | TickFlowProvider | FQuantProvider（PoC） | FQuantProvider（本设计） | 关键字段来源 |
|------|------------------|--------------------|----------------------|---------------|
| `get_instruments(stock)` | ✅ | ✅（仅 A 股 + 港股，搜索模式） | ✅ 全 asset_type | fstore `base_infos` |
| `get_instruments(index)` | ✅ | ⚠️ | ✅ | fstore `base_infos` `asset_type=10` |
| `get_instruments(etf)` | ✅ | ⚠️ | ✅ | fstore `base_infos` `asset_type=20` |
| `get_daily`（不复权） | ✅ | ✅ | ✅ | engine-data `wide` 主源 |
| `get_daily`（前复权 fq=1） | ✅ | ❌ | ✅ | fstore `day_klines` fq=1 |
| `get_daily`（后复权 fq=2） | ✅ | ❌ | ✅ | fstore `day_klines` fq=2 |
| `get_daily` 的 daily 增强（pre_close / change_rate / inner_volume） | ⚠️ | ❌ | ✅（来自 wide） | engine-data |
| `get_adj_factors` | ✅ | ❌（cap=False，return empty） | ✅ | engine-data `xdxr` + fstore `chuquan_chuxi` |
| `get_minute`（1m） | ⚠️（PoC 内返回空，实际走 service 层自管） | ❌ | ✅ | engine-data `minutes` |
| `get_minute`（5m/15m 等其它 freq） | ❌ | ❌ | 🔜（路线 6） | — |
| `get_realtime`（快照） | ✅（tencent 兜底） | ❌ | 🔜（路线 1） | — |
| `get_realtime`（tick 聚合成买/卖/中性） | ⚠️ | ❌ | 🔜（路线 1） | engine-data `trans` |
| `financial.income` | ✅（同 cap=True，service 走另一条本地 Parquet 路径） | ❌（cap=False） | ✅ | fstore `financial_report_income_statement` |
| `financial.balance_sheet` | ✅ | ❌ | ✅ | fstore `financial_report_balance_sheet` |
| `financial.cash_flow` | ✅ | ❌ | ✅ | fstore `financial_report_cash_flow` |
| `financial.annual` | ✅ | ❌ | ✅ | fstore `financial_report_annual` |
| `financial.quick` | ✅ | ❌ | ✅ | fstore `financial_report_quick` |
| `financial.forecast` | ✅ | ❌ | ✅ | fstore `financial_report_forecast` |
| `moneyflow.daily` | ✅（service 层通过另一 fetchMoneyflow 用） | ❌ | ✅ | moneyflow `/api/v1/moneyflow/daily/stocks` |
| `moneyflow.minute` | ✅ | ❌ | ✅ | moneyflow `/api/v1/moneyflow/minute/stocks` |
| `transactions`（逐笔） | ✅（service 层） | ❌ | ✅ | engine-data `trans` |
| `corp_action`（除权除息事件） | ✅ | ❌ | ✅ | fstore `chuquan_chuxi`（或 engine-data `xdxr`） |

**财务口径变化**：TickFlowProvider `capabilities.financial=True` 但内部走的是本地 Parquet（service 层在 `financial_sync.py` 拉取后由 analyzer 直接读 part.parquet），**并不经 `MarketDataProvider.get_financial()`**。本设计 `FQuantProvider.capabilities.financial=True` + `get_financial()` 扩展方法直接拉数据库，避免"capability=True 却无入口"的语义裂缝。

**还剩缺口**：
- `realtime` 仍然 🔜——但 engine-data `trans` 已能凑出"主买主卖聚类"作为 realtime 平替。详见路线 1。
- `chips`（筹码分布）引擎未接入，归属 fquant 自身的 fflow 体系——本次设计不破坏，留路线 3。
- `frequency > 1m` 分钟级聚合：路线 6，由 Polars `group_by_dynamic` 重采样。

---

## 10. 后续路线

| # | 路线 | 优先级 | 触发条件 | 工作量预估 | 风险点 |
|---|------|--------|---------|-----------|--------|
| R1 | **realtime 全链路** | P0 | 用户提需 / 前端启用实时面板 | 2 周 | tencent qt quote 接口不稳定，需要二次重试；fquant 现成的 tencent_quote.go 可复用为 service 层 upstream |
| R2 | **升级版 provider 文件命名决策** | P0 | 实现开始时定 | 1h | 直接重写 `fquant_provider.py`（覆盖 PoC） vs 新建 `fquant_provider_v2.py`，由 registry 决定——本期文档倾向**原地大改**，因为 §3.5 已经把 capabilities 改写，PoC 老 API 是 `instruments + daily` 极度受限，无 service 调用方依赖 |
| R3 | **engine-data `chips` 接入** | P2 | NAS 性能问题解决 | 1 周 | 实测超时，单次请求 8s+；需要预热 + lazy |
| R4 | **指数/ETF 成分股（chengfen_gu_items）** | P1 | frontend 需要板块画面 | 3 天 | `index.INDEX` symbol 需要在 base_infos 当前是 `asset_type=10` 才能匹配——先做 intros 验证 base_infos 是否覆盖 BJX/中证/同花顺等行业指数代码 |
| R5 | **moneyflow 09:25 集合竞价特殊处理** | P0 | 上层做"集合竞价方向判断"时 | 1 天 | 业务约定需先于代码落地；本设计在 §7.4 仅做提醒 |
| R6 | **多频聚合（5/15/30/60m）** | P2 | minute 链路稳定后 | 2 天 | Polars `group_by_dynamic` 处理午休断点；建议先做 5m |
| R7 | **fstore 连接池 + 监控指标** | P1 | 上线时 | 3 天 | 暴露到 `/metrics` 端点；与现有 backtest 共享 pool |
| R8 | **schema 演进（base.py）** | P3 | services 真正接 financial provider 时 | 1 周 | 本期不建议改 base.py（任务 N1）；待财务真正走 provider 后再补充 `get_financial` 契约方法 |
| R9 | **`financial_report_quick` 数据回填** | P1 | 上线前 | 离线任务 | 实测 600519 在 quick 表 0 行；与 fquant collector 对齐 |
| R10 | **timezone 一致性** | P0 | service 层接入 | 1 天 | engine-data `datetime` 是本地不带时区；moneyflow `TradeDate` 也是本地；fstore `t_date` 是 date；要在 provider 内统一为 `pl.Datetime("ns", "Asia/Shanghai")` |

### 实施建议（PoC → 设计 → 落地）

1. **不**急于重写 `fquant_provider.py`：先新建 `fquant_provider_v2.py` 和 `app/data_providers/fquant/` 子包；与 PoC 共存；通过 feature flag 切换：
   - 在 `registry.py` 增加根据 `settings.FQUANT_PROVIDER_VERSION` 选择；或
   - 直接**就地大改** `fquant_provider.py`（决策 R2）：破坏性较低，因为现有 PoC 上层消费者只有 `scripts/test_fquant_provider.py` 单文件（已 self-contained），外部 service **未消费**。
2. **建议"就地大改"**：仅在文档落地后开 issue "把 fquant_provider.py 改写到符合本文档 §4"；新文件兼容老 API（保持 `split_symbol / code_and_market_to_symbol / _SUFFIX_MAP`）；通过 PoC 测试用例作为回回归门槛。

### 已读不改变更清单（任务约束 §6 验收第 5 条）

> 本设计任务完整 **未改动** 以下文件（仅 read）：

| 路径 | 操作 |
|------|------|
| `backend/app/data_providers/base.py` | 读 |
| `backend/app/data_providers/tickflow_provider.py` | 读 |
| `backend/app/data_providers/schemas.py` | 读 |
| `backend/app/data_providers/normalizer.py` | 读 |
| `backend/app/data_providers/registry.py` | 读 |
| `backend/app/data_providers/fquant_provider.py`（PoC） | 读 |
| `backend/docs/FQUANT_PROVIDER.md` | 读（不覆盖、不删除） |
| `fquant/internal/api/engine_stock_data.go` | 读 |
| `fquant/internal/api/engine_data_proxy.go` | 读 |
| `fquant/internal/api/fund_flow_moneyflow.go` | 读 |
| `fquant/internal/api/server.go` | 读 |
| `backend/docs/FQUANT_PROVIDER_DESIGN.md` | **新增（本文档）** |

---

## 附录 A：实测样本（备查）

### A.1 engine-data `wide` 单点（600519，2026-07-01）

```
{"date":"2026-07-01","open":1180.1,"close":1193.01,"high":1196.8,"low":1166.33,
 "volume":4247400,"amount":5033838080,"change_rate":0.6343368564897256,
 "last_close":1185.49,"inner_amount":2446997810,"inner_volume":2066750,
 "outer_amount":2586910167,"outer_volume":2180650,
 "open_volume":18600,"close_volume":33600,"open_turnz":0.44,"close_turnz":0.79,
 "open_unmatched":0,"close_unmatched":0,"up":0,"down":0}
```

### A.2 engine-data `xdxr` 单点（600519，2025-12-19）

```
{"date":"2025-12-19","fenhong":239.57000732421875,"fenshu":0,
 "qianzongguben":0,"qianliutong":0,
 "houzongguben":0,"houliutong":0,
 "peigu":0,"peigujia":0,"songzhuangu":0,"suogu":0,
 "xingquanjia":0,"category":1,"name":"除权除息"}
```

### A.3 moneyflow daily 单点（600519，2026-07-01）

```
{"code":600519,"source":"tdx",
 "total":{"main_inflow":1865015178,"main_outflow":1739591624,"main_net":125423554,
          "total_inflow":1883634732,"total_outflow":1759986769,"total_net":123647963,
          "volume":3120300,"amount":3691020280}}
```

### A.4 fstore `base_infos` 一行（600519）

```
{ code:'600519', name:'贵州茅台', symbol:'sh600519',
  asset_type:1, stype:2, ssdate:'2001-08-27',
  zgb:1250080000, ltgb:1250080000, zsz:1637109989376, roe:10.57,
  ltbl:1.0, hsgt:1 }
```

### A.5 fstore `chuquan_chuxi` 三行（600519）

```
{ '600519', '2025-12-19', pxbl:23.957, cqcxtype:1 }
{ '600519', '2026-05-28', pxbl:0,      cqcxtype:1 }  # 股本变化
{ '600519', '2026-06-26', pxbl:27.60,  cqcxtype:1 }
```

---

> **文档结束**。下一阶段（实现）请参考 §10 路线 R1/R2。
