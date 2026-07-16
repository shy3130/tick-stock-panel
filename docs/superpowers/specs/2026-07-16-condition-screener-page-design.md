# 设计：条件选股页（参考 go-stock / 东财条件选股，本地 DuckDB 引擎）

- 日期：2026-07-16
- 状态：已批准方向，待用户复核 spec
- 相关模块：`backend/app/services/screener.py`、`backend/app/api/screener.py`、`backend/app/strategy/`、`frontend/src/pages/`
- 取代：`2026-07-16-nl-screener-design.md`（已删除）

## 背景与动机

`../go-stock` 的选股本质是把自然语言条件 POST 给东方财富在线选股 API，本地无引擎、依赖
`qgqp_b_id` cookie（详见调研结论）。本项目已有基于 DuckDB + Polars 的本地选股引擎
（`services/screener.py`），但现有 `Screener.tsx` 只有 12 个预设策略卡片 + 一个自定义 SQL 框，
条件维度不够丰富。

目标：新建一个独立的**条件选股页**，参考 go-stock / 东财条件选股的多维度条件，做一个
**结构化条件构建器**为主、**自然语言辅助填充**的选股界面，数据源保持本地 DuckDB。

## 已确认的设计决策

1. 主入口：**结构化条件构建器为主 + 自然语言辅助填充**。
2. 旧页去留：**新页并存，现有 `Screener.tsx`（12 预设）保留不动**。
3. 执行层安全：**新页走全新的结构化 predicate 路径，不复用现有不安全的 `run(list[str])`**。
4. 基本面：**本期 JOIN `financials/metrics` 接入**（净利润同比/ROE/EPS/毛利率/行业，PE/PB 派生）。

## 关键约束：来自 codex 代码审查的已核实结论

以下均已实测核实，直接约束本设计（避免重蹈前一版 spec 的缺陷）：

- **裸 SQL 注入面（P0）**：现有 `ScreenerService.run()`（`screener.py:409`）把 `conditions`、
  `order_by`、`limit` 直接拼进 SQL 后 `execute()`，无参数绑定。`/api/screener/run` 与
  `agent_tools` 已在用它。→ **新页不复用此入口**，改用结构化 predicate 编译为 Polars 表达式。
- **order_by 注入 + 方向缺失（P0）**：`sql += f" ORDER BY {order_by}"` 且无方向默认升序。
  → order_by 用 `{field(白名单), direction(asc|desc)}`。
- **部分条件被忽略后 fail-open（P0）**：静默丢约束会返回误导结果。→ **fail-closed**：
  自然语言解析出的未识别条件不静默丢弃，前端标注、用户确认后才执行。
- **基本面并非"本地无数据"（P1）**：实测 `data/financials/metrics/part.parquet` 存在，含
  `yo_y_profit`（净利润同比；注意 `yoy_profit` 列实测全空 0/17558，须用 `yo_y_profit`）、
  `weight_avg_roe`、`basic_eps`、`bps`、`gross_margin`、`industry`（满值 17558/17558）。
  **不含 `pe_ttm`/`pb`**，需用 `close/eps`、`close/bps` 派生（近似，需注明）。键为 `symbol`
  (000001.SZ) / `code`，与 enriched 对齐；按 `report_year`+`quarter` 取最新报告期。
- **基本面 EPS 是累计值（复核轮 A，已实测）**：`basic_eps` 为累计口径（茅台 2024 Q1=15.34
  → Q4=61.71 逐季累加），派生 PE **不能直接用单季 EPS**，须用 TTM（近四季滚动）或年化，否则
  PE 严重失真。最新报告期选取用 `(report_year, quarter)` 复合排序定 tie-break，勿仅按 quarter
  字符串。
- **基本面前视偏差门控（复核轮 B，已实测 `notice_date` 存在）**：financials JOIN 必须用
  `notice_date <= as_of` 过滤，只取"截至选股日已公告"的报告期。否则历史日选股 / 回测会用到
  未来财报，产生前视偏差。这是正确性约束，非可选优化。
- **市值单位（P1）**：实测 `float_shares`/`total_shares` 单位为**股**（茅台 12.56 亿股），
  故 `close*float_shares` 得到**元**；用户输入"流通市值(亿元)"需 `/1e8` 换算。
- **运行时列可用性与 NULL（P1）**：enriched 持久层仅 14 列，`ma60`/连板/信号等运行时补算；
  `turnover_rate` 及基本面字段存在 NULL（部分标的 `yo_y_profit` 为空）。每个白名单字段需声明
  来源与 NULL 处理策略。

## 架构总览

```
前端 条件选股页
  ├─ 结构化条件构建器 (分组下拉: 字段 + 运算符 + 值)
  │     └─▶ predicate JSON: {conditions:[{field,op,value}], order_by:{field,direction}, limit}
  │         └─▶ POST /screener/query
  │             └─▶ 后端安全编译器 (FIELD_REGISTRY 白名单)
  │                 └─▶ Polars 表达式 (非 SQL 字符串)
  │                     └─▶ 在 enriched (+JOIN financials/metrics + instruments) DataFrame 过滤
  │                         └─▶ { rows, total, applied, as_of, elapsed_ms }
  │
  └─ 自然语言框 (辅助)
        └─▶ POST /screener/nl_parse
            └─▶ LLM 解析成 predicate JSON (仅回填构建器, 不执行)
                └─▶ { recognized:[{field,op,value}], unrecognized:[{raw,reason}] }
```

## 组件设计

### 后端

**`app/services/screener_query.py`（新）** — 安全条件编译器（核心）
- `FIELD_REGISTRY: dict[str, FieldSpec]`，每字段声明：
  `{column | expr, source(persist|runtime|financials|derived), unit, value_type, null_policy}`
- `compile_predicate(conditions, order_by) -> pl.Expr`：
  - field 必须命中白名单，否则拒绝（不透传）
  - op 限定 `{>, <, >=, <=, =, !=, between, in}`
  - value 按 `value_type` 校验（数值范围 / 枚举），单位换算（市值 `/1e8`）
  - 编译为 **Polars 表达式**，绝不拼 SQL 字符串
- `QueryService.query(as_of, predicate) -> QueryResult`：
  - 加载 enriched（复用 `ScreenerService._load_enriched_for_date`，含运行时指标）
  - JOIN instruments（float_shares/total_shares/name）+ JOIN financials 最新报告期
  - `df.filter(expr)` → sort（order_by.field/direction）→ limit

**`app/services/screener_financials.py`（新）** — 基本面 JOIN 辅助
- 读取 `financials/metrics`，先按 `notice_date <= as_of` 门控（防前视偏差），再按 `symbol`
  取 `(report_year, quarter)` 复合排序的最新一行
- 暴露列：`yo_y_profit`、`weight_avg_roe`、`basic_eps`、`bps`、`gross_margin`、`industry`
- 派生：`pe_approx = close / eps_ttm`（`eps_ttm` 由累计 EPS 换算为近四季滚动/年化，不用单季累计值）、
  `pb_approx = close / bps`（注明近似，NULL 透传）
- 结果可 LRU 缓存（报告期低频变化）

**`app/services/nl_screener.py`（新）** — 自然语言解析（辅助，不执行）
- `parse(text, profile_id) -> {recognized, unrecognized}`
- 复用 `app.services.ai_provider.generate_ai_text`，system prompt 给出白名单字段清单，
  要求输出结构化 JSON；命中白名单进 `recognized`，否则进 `unrecognized`（fail-closed）
- **只返回给前端回填构建器，不触达执行层**

**`api/screener.py`（改）** — 加端点
- `POST /screener/query` — body: predicate JSON → `QueryService.query` → 结果
- `POST /screener/nl_parse` — body: `{text}` → `nl_screener.parse` → `{recognized, unrecognized}`
- `GET /screener/fields` — 返回 FIELD_REGISTRY 元数据（字段名/分组/单位/是否可用），供前端渲染构建器
- `GET /screener/nl_presets` — go-stock 策略语句库（`gostock_presets.py`）

**`app/strategy/gostock_presets.py`（新）** — go-stock 策略语句库
- 来自 `choice_stock_by_indicators_tool.go` 例句，按可执行性分级：
  `{id, name, description, predicate, executable_level(full|needs_fundamental|unsupported)}`
- 纯技术面标 `full`；含基本面标 `needs_fundamental`（本期基本面已接入，多数可用）；
  含资金流/概念标 `unsupported`

### 前端

**`pages/ConditionScreener.tsx`（新）** — 条件选股页
- 顶部：自然语言框（提交 → `/screener/nl_parse` → 回填构建器；`unrecognized` 标黄提示）
- 主体：分组条件构建器，从 `/screener/fields` 动态渲染
- 底部：执行按钮 → `/screener/query`；结果表复用 `components/screener/ScreenerTable`
- 路由与导航新增入口，与现有「选股器」并列

**`components/screener/ConditionBuilder.tsx`（新）** — 分组条件构建器
- 分组：行情 / 市值 / 技术 / 涨停 / 基本面 / 板块过滤
- 每条：字段下拉 + 运算符 + 值输入；不可用字段（如资金流）置灰标注
- 输出 predicate JSON

**`lib/api.ts`（改）**：在现有 `api` 扁平对象加 `screenerConditionQuery(conditions, orderBy?, limit?)`、
`screenerNlParse(text)`、`screenerFields()`、`screenerNlPresets()`（沿用 `request<T>` helper；注意本仓库
无 `screenerApi` 对象、无 `ScreenerRow` 类型）

## 字段白名单（分组 + 来源 + 单位）

| 分组 | 字段 | 映射 / 来源 | 单位/NULL |
|---|---|---|---|
| 行情 | 涨跌幅 | `change_pct`（persist/runtime） | 小数(0.05=5%)，不做%换算 |
| 行情 | 股价 | `close`（persist） | 元 |
| 行情 | 换手率 | `turnover_rate`（persist，有 NULL） | %，NULL 视为不满足 |
| 行情 | 量比 | `vol_ratio_5d`（runtime） | 倍 |
| 行情 | 成交额 | `amount`（persist） | 元 |
| 市值 | 流通市值 | `close*float_shares/1e8`（derived） | 亿元 |
| 市值 | 总市值 | `close*total_shares/1e8`（derived） | 亿元 |
| 技术 | 均线多头 | `ma5>ma10>ma20>ma60`（runtime） | 布尔 |
| 技术 | MA 上/下方 | `close` vs `ma5/10/20/60`（runtime） | 布尔 |
| 技术 | MACD 金叉 | `signal_macd_golden`（runtime） | 布尔 |
| 技术 | KDJ/RSI | `kdj_k`/`rsi_14`（runtime） | 数值 |
| 技术 | BOLL 突破 | `signal_boll_breakout_upper`（runtime） | 布尔 |
| 涨停 | 当日涨停/连板 | `signal_limit_up`/`consecutive_limit_ups`（runtime） | 布尔/次 |
| 基本面 | 净利润同比 | `yo_y_profit`（financials；`yoy_profit` 全空勿用） | %，NULL 视为不满足 |
| 基本面 | 行业 | `industry`（financials，枚举） | 枚举，`=`/`!=` |
| 基本面 | ROE | `weight_avg_roe`（financials） | % |
| 基本面 | EPS | `basic_eps`（financials） | 元 |
| 基本面 | 毛利率 | `gross_margin`（financials） | % |
| 基本面 | 行业 | `industry`（financials） | 枚举 |
| 基本面 | PE/PB（近似） | `close/eps` / `close/bps`（derived，标注近似） | 倍 |
| 过滤 | 板块 | symbol 前缀 60/00/300/688 / `.BJ` | 枚举 |
| 过滤 | 排除 ST | `name` 正则 `ST\|\*ST\|退` | 布尔 |

**本期不支持（构建器置灰标注）**：主力资金净流入、北向资金、实时题材概念。

## 数据流

1. 用户在构建器选条件（或自然语言框输入 → `/screener/nl_parse` → 回填 → 用户确认 unrecognized）
2. 前端组装 predicate JSON → `POST /screener/query`
3. 后端 `compile_predicate` 校验白名单 + 编译 Polars 表达式
4. `QueryService.query`：加载 enriched（运行时补指标）+ JOIN instruments + JOIN financials 最新期
5. `df.filter(expr)` → sort → limit
6. 返回 `{rows, total, applied(实际生效条件), as_of, elapsed_ms}`
7. 前端 `ScreenerTable` 渲染

## 错误处理

- 构建器条件含非白名单 field/op/非法 value → 400 明确错误（前端本不该发出，双重保险）
- 自然语言解析：LLM 非法 JSON → 重试 1 次 → 失败返回空 recognized + 提示；unrecognized 永不静默丢弃
- financials JOIN 缺失某标的 → 基本面列为 NULL，含该条件时该标的按 null_policy 处理（默认不满足）
- 空结果 → 正常返回，附 `applied` 让用户看到实际生效条件

## 安全

- 执行层零 SQL 字符串拼接：predicate 编译为 Polars 表达式，field/op/value 全部走白名单+类型校验
- order_by 仅 `{白名单 field, asc|desc}`
- LLM 输出只回填构建器、用户可见可改，绝不直达执行层
- 新页不触碰现有 `run(list[str])` 注入面（该 tech-debt 单列，不在本期范围）

## 测试

- `compile_predicate` 单测：白名单命中/拒绝、op 枚举、value 类型与单位换算、恶意 field/order_by 被拒
- `screener_financials` 单测：最新报告期 `(report_year, quarter)` 复合选取、`notice_date <= as_of`
  前视偏差门控（历史 as_of 不得取到未公告报告期）、EPS TTM 换算（累计值不当单季用）、NULL 透传、PE/PB 派生
- `QueryService.query` 集成测试：技术面+基本面混合条件端到端（真实 parquet 小样本）
- `nl_screener.parse` 单测：mock LLM，recognized/unrecognized 分类，fail-closed
- `gostock_presets` 加载测试：分级正确，`full` 级语句能被 compile 通过
- 市值单位边界测试：流通市值 100 亿 → 正确换算

## 范围裁剪（YAGNI，本期不做）

- 重构现有 `ScreenerService.run()` / `/api/screener/run` / `agent_tools` 的注入面（单列 tech-debt）
- 主力资金净流入 / 北向资金 / 实时题材概念接入
- 定时任务 / 结果推送（go-stock 的 cron + feishu/dingding）
- 接入东方财富在线选股 API
- ETF / 板块选股、热门策略榜、同花顺策略广场
- 策略保存 / 收藏（可作为下一期增量）
