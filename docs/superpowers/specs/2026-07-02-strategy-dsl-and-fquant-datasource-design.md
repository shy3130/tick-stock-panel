# 设计方案：策略 DSL 重构 + fquant 直连数据源

- **日期**：2026-07-02
- **性质**：设计方案（语言无关架构 + spec），**本文档不含代码实现**
- **背景**：为后端最终可移植到 Golang 扫清两处 Python 专有障碍——
  ① 策略以 Python 代码经 `importlib.exec_module` 运行时执行；
  ② 数据源耦合 TickFlow 官方付费 Python SDK。

## 决策记录（本设计的前置约束）

| # | 决策 | 取值 |
|---|------|------|
| D1 | 目标载体 | 只出设计方案，不写代码；架构语言无关 |
| D2 | DSL 表达力 | 声明式 + **有限窗口/状态算子** |
| D3 | 数据源接入形态 | **panel provider 层自行直连四个上游源**（不经 fquant 服务） |
| D4 | 旧策略处置 | **彻底切换到 DSL，删除 Python 执行路径** |
| D5 | DSL 执行模型 | **方案 A：DSL → 语言无关 IR → 各语言后端编译为原生向量化算子** |
| D6 | 存量迁移工具 | **不做**。18 内置策略以 DSL 重新定义；用户存量 AI Python 策略不自动转换，改由 AI-to-DSL 重新生成 |
| D7 | 窗口算子目录 | 保留完整目录（[ADR-0001](../../adr/0001-dsl-keeps-full-window-operator-catalog.md)） |
| D8 | 含窗口策略执行模型 | T3 混合（无窗口=单日快路径；含窗口=`requires_history`+自动 lookback）（[ADR-0002](../../adr/0002-window-strategy-execution-hybrid.md)） |
| D9 | scoring 能力 | 保留 `direction`(asc/desc) 与 `expr`(内联表达式)，两者皆纳入 |
| D10 | waizao 角色 | 仅补充面（梯队/情绪/板块/名称）；核心行情永不回退第三方（[ADR-0003](../../adr/0003-waizao-supplementary-only.md)） |
| D11 | 实时能力边界 | R1：watchlist 走 tdx-api 逐笔；全市场=fstore `daily_markets` 快照语义（[ADR-0004](../../adr/0004-realtime-scope-watchlist-vs-market-snapshot.md)） |
| D12 | 切换验收闸门 | G1：18 内置旧↔新 parity 对拍全绿才删 Python exec（[ADR-0005](../../adr/0005-dsl-cutover-parity-gate.md)） |

---

# Part A · 动态策略插件系统 → 声明式 DSL

## A0. 现状与问题

- 策略是 Python `.py` 文件，定义 `filter(df, params) -> pl.Expr`，引擎 `importlib.util.spec_from_file_location` + `exec_module` 运行时加载执行（`app/strategy/engine.py:108`）。
- 来源三类：18 个内置策略（`app/strategy/builtin/*.py`）、自定义信号（`custom_signals.py`，**已是 JSON DSL**）、AI 生成（LLM 产出任意 Polars Python，`ast` 黑名单校验后 exec，`ai_generator.py`）。
- 事实核查：**18 个内置策略全部为无状态"列引用 + 阈值"过滤**（无 `filter_history` / `partition_by` / `to_dicts` / `.over`）。
- 问题：`exec` 任意 Python 无法在 Go 中复用，是 Go 移植的头号架构障碍。

## A1. 分层架构（四层，语言无关）

```
strategy.json (DSL)  ──parse+validate──▶  Expression IR (语言无关 AST)  ──compile──▶  原生算子
   用户/AI 产出            schema + 白名单 + 算子目录静态校验        Python 后端: IR → Polars 表达式
                                                                 Go 后端(未来): IR → 向量化算子/循环
```

**关键不变式**：下游三消费者（选股 Screener / 回测 Backtest / 监控 Monitor）**接口不变**——它们仍只消费编译产出的"`entry`/`exit` 布尔列 + `score` 数值列"。这与现有 `custom_signals.inject()` 往面板注入布尔列的模式一致，三处零特殊处理即可生效。

## A2. DSL Schema v1（一个策略 = 一个 JSON）

```jsonc
{
  "schema_version": 1,
  "meta": {
    "id": "macd_golden",
    "name": "MACD 金叉放量",
    "tags": ["MACD", "金叉", "放量"],
    "limit": 100,
    "order_by": "score",
    "descending": true,
    "params": [
      { "id": "vol_ratio_min", "type": "float", "default": 1.5, "min": 0.5, "max": 5.0, "step": 0.1 }
    ]
  },
  "basic_filter": {
    // 沿用现有 DEFAULT_BASIC_FILTER 语义：price_min/max, market_cap_min/max,
    // float_cap_min/max, amount_min/max, turnover_min/max, exclude_st,
    // exclude_new_days, boards[]
    "enabled": true
  },
  "entry":  { /* IR 布尔表达式，见 A3 */ },
  "exit":   { /* IR 布尔表达式 */ },
  "scoring": [
    { "expr": { "col": "momentum_60d" }, "weight": 0.4, "direction": "desc" },
    { "expr": { "col": "vol_ratio_5d" }, "weight": 0.3, "direction": "desc" },
    { "expr": { "col": "change_pct"   }, "weight": 0.3, "direction": "desc" }
  ],
  "risk": {
    "stop_loss": -0.07,
    "trailing_stop": null,
    "take_profit": null,
    "trailing_tp_activate": null,
    "trailing_tp_drawdown": null,
    "max_hold_days": 20
  }
}
```

映射到旧结构：`meta`↔`META`；`entry`/`exit`↔`ENTRY_SIGNALS`/`EXIT_SIGNALS`+`filter()`；`risk`↔`STOP_LOSS`/`MAX_HOLD_DAYS`/`TRAILING_*`；`scoring`↔`meta.scoring`（升级为可含表达式与方向）。运行期参数由 `meta.params` 注入，替代 `filter(df, params)` 的 `params`。

### `entry` 示例（等价于旧 `macd_golden.filter`）

```jsonc
{ "op": "and", "args": [
    { "col": "signal_macd_golden" },
    { "op": ">=", "args": [ { "col": "vol_ratio_5d" }, { "param": "vol_ratio_min" } ] }
] }
```

## A3. Expression IR（薄 AST — 跨语言唯一契约）

节点为封闭集合，JSON 表示：

| 节点 | 形态 | 语义 |
|------|------|------|
| `col` | `{ "col": "ma20" }` | 引用 enriched 列（**白名单**，复用 `custom_signals.ALLOWED_FIELDS` + `signal_*` 布尔列 + `csg_*` 自定义信号列） |
| `lit` | `{ "lit": 2.0 }` / `{ "lit": true }` | 数值 / 布尔常量 |
| `param` | `{ "param": "vol_ratio_min" }` | 运行期由 `meta.params` 注入 |
| `arith` | `{ "op": "-", "args": [a, b] }` | `+ - * /`（如 `high-low`、`close/prev_close`） |
| `cmp` | `{ "op": ">=", "args": [a, b] }` | `> >= < <= == !=`，两侧可为列/常量/表达式（支持跨列比较） |
| `logic` | `{ "op": "and", "args": [...] }` | `and` / `or` / `not`（`not` 单参），任意嵌套 |
| `window` | `{ "fn": "rolling_max", "arg": a, "n": 60 }` | 有限窗口/状态算子，见 A4 |

IR 是**唯一的跨语言契约**：DSL 解析产出 IR，各语言后端只需实现"IR 节点 → 本地算子"的编译。新增能力 = IR 加节点/算子 + 每个后端加一个分支。

## A4. 窗口/状态算子目录（封闭集合）

统一语义前提：面板按 `(symbol, date)` 升序排序；所有窗口算子在 `symbol` 分组内计算（等价 Polars `.over("symbol")` / Go 按 symbol 切片循环）。

| 算子 | 签名 | 语义 |
|------|------|------|
| `rolling_mean/max/min/std/sum` | `(expr, n)` | 组内 n 期滚动聚合 |
| `shift` | `(expr, n)` | 组内后移 n 期 |
| `pct_change` | `(expr, n)` | `expr / shift(expr,n) - 1` |
| `n_day_high` / `n_day_low` | `(expr, n)` | `expr >= rolling_max(expr,n)` / `<= rolling_min` |
| `cross_up` / `cross_down` | `(a, b)` | 金叉/死叉：`a>b 且 shift(a,1)<=shift(b,1)`（反之为死叉） |
| `consecutive_true` | `(bool_expr)` | 连续为真的递推计数（连板数模式，组内分段 `cum_sum`） |
| `cs_rank` | `(expr)` | 截面排名，**over date**（因子回测用） |
| `cs_qcut` | `(expr, n)` | 截面分位分组，**over date** |

> 每个算子的 null 处理、窗口最小周期（不足期返回 null）、排序假设、边界（跨 symbol 不 shift）由 **IR 规范文档逐条钉死**。

## A5. 跨语言一致性保障（方案 A 的核心）

1. **IR 规范文档**：每个节点/算子一条精确语义定义（含 null、类型提升、排序、分组、边界）。
2. **黄金测试集**：一份小型 fixture 面板（含停牌、除权、跨 symbol 边界、null 等边角）+ 一组覆盖全部节点/算子的 DSL 用例 + 期望输出（布尔列/score 列）。Python compiler 与（未来）Go compiler 均跑同一测试集比对，杜绝语义漂移。
3. **静态校验**（编译前）：schema 校验 → 列名在白名单 → 算子在目录内 → `param` 引用存在于 `meta.params` → 类型基本自洽（布尔位置放布尔、数值位置放数值）。

## A5b. 执行模型（T3 混合，见 ADR-0002）

- **无窗口** DSL → 单日快路径 `loader(as_of)`，与现状普通策略一致（选股 `run_all`、盘中监控扫描保持单日热路径）。
- **含窗口** DSL → 编译期检测到 `window` 节点即标记 `requires_history`；`lookback` 取 IR 内所有 `window.n` 最大值自动推导；复用现有 `history_loader(as_of, lookback)` 路径。作者不手写 lookback。
- `run_all` 按"是否含窗口 + 最大 lookback"分组共享历史加载（引擎已有共享 history 先例）。

## A6. AI 生成 → DSL（不再产 Python）

- LLM system prompt 改为携带 **DSL schema + IR 算子目录 + few-shot JSON 样例**（替代现 `docs/strategy-guide.md` 的 Python 指南）。
- 产出 **DSL JSON**；校验从"AST 黑名单 + `exec`"升级为 **A5.3 的白名单式静态校验**。
- 结果：**无 `exec` / 无 `importlib` / 无沙箱**，天然安全且语言无关。`ai_generator.py` 的 `_validate_safety` / `compile` / `eval` 路径删除。

## A7. 拆除与保留

- **删除**（**须先过 A9 对拍闸门**）：`engine.py` 的 `importlib` / `exec_module` 动态加载；`ai_generator.py` 的 Python 代码生成与 `exec`/`eval` 校验路径；`builtin/*.py` 的 Python 策略文件（改为 DSL 定义集）。
- **保留并复用**：`engine.py` 的两阶段过滤 + 通用评分 + 排序/limit 流程（改为消费编译产物，撮合/评分算法不变）。
- **归并 custom_signals**：自定义信号本就是 DSL 的一个子集。明确做法——其"字段+运算符+值"条件在加载时**转换为 IR 节点**（`cmp`/`logic`），走**同一条 IR → compiler 管线**产出 `csg_*` 布尔列；不再保留独立的 `_OP_BUILDERS`→Polars 直译路径。字段白名单合并进 A3 的统一白名单。
- **不做**：存量 AI Python 策略的自动迁移工具（D6）。

## A8. 模块边界

| 模块 | 职责 | 依赖 |
|------|------|------|
| `strategy/dsl/schema` | DSL JSON schema + 校验 | 无 |
| `strategy/dsl/ir` | IR 节点类型定义 + 语义规范（文档） | 无 |
| `strategy/dsl/compile_polars` | IR → Polars 表达式（Python 后端） | polars |
| `strategy/dsl/compile_go`（未来） | IR → Go 向量化算子 | — |
| `strategy/engine` | 加载 DSL → 编译 → 两阶段过滤 + 评分 + 排序 | dsl/* |
| `strategy/builtin/`（DSL） | 18 内置策略的 DSL 定义集 | — |

## A9. Cutover Parity Gate（验收条款，见 ADR-0005）

删除 Python exec **之前**的一次性对拍：18 内置策略旧 Python 版 vs 新 DSL 版，在同一历史 enriched 面板 + 同一区间上运行，断言 **选出 symbol 集合一致 + score 浮点容差内一致**。18 全绿方可删除 `importlib`/`exec`。该对拍同时充当 IR compiler 黄金测试（A5）的首批真实用例。用户存量 AI Python 策略不在闸门内（D6，由用户 AI-to-DSL 重生成）。

---

# Part B · 去 TickFlow SDK，直连 fquant 四上游源

## B0. 现状

- 原架构以 TickFlow 官方付费 Python SDK 为主源（`app/tickflow/client.py`），按订阅档位（`tiers.yaml`）门控能力。
- 已有 `data_providers` 抽象层 + `FQuantProvider`（直连 fstore PG + engine-data + moneyflow），但存在 **realtime、depth 缺口**。
- 参照系：`../fquant`（Go 项目）已有成熟 `Provider` 接口 + `Manager` 链式 fallback（其源集含 fquant 复合源/fstore/fmcli/waizao，且经 `tdx-api` 提供实时）。本设计**复用其源编排范式**，但 panel **自行直连上游**（D3），不经 fquant 服务。
- **panel 采用的四上游源**（与 fquant 源集不完全相同）：**engine-data(日K主源) · fstore-PG(元数据/财务) · tdx-api(实时) · waizao(仅补充面)**；不引入 fmcli。

## B1. 上游源矩阵（panel provider 层自实现适配器）

| 上游源 | 协议 | 覆盖能力 | 角色 |
|--------|------|----------|------|
| **engine-data** `:8099` | HTTP GET | 日K(`wide`) / 分钟 / xdxr / trans | 日K 主源 |
| **fstore** PG `:5432` | pg 驱动 | instruments/`base_infos` · 财务报表 · 复权事件 · 分钟备份 · universes(`chengfen_gu`) | 元数据 + 财务主源 |
| **tdx** (`tdx-api`) | HTTP GET `/api/quote?code=` | **realtime 报价（单标的）** | 补 realtime 缺口；watchlist 逐笔（ADR-0004） |
| **waizao (wz)** | HTTP GET（token 鉴权，第三方） | 涨停梯队 · 板块 · 名称 · 情绪面 | **仅补充面**；核心行情永不回退（ADR-0003） |

## B2. Manager 链式 fallback（对齐 fquant Go 的 Manager 模式）

**关键：fallback 判定粒度是"单标的/覆盖缺口"，不是"整批首个非空"。**
fquant Go `Manager.DailyRange` 是单标的调用，"首个 `len>0` 即返回"成立；但 panel 的 `get_daily`/`get_realtime` 是**批量**接口，若照搬"整批非空即返回"会静默吞掉主源缺失的那部分标的。panel 现有 `fquant_provider.get_daily` 已给出正确范式：**逐 symbol 尝试主源→缺失则该 symbol 回退备源→union**（而非整批一刀切）。本设计沿用此粒度。

- 每个 capability 一条**有序源链**，按上述"逐标的/覆盖缺口"粒度回退，异常记录并降级下一源。
- 具体降级规则（沿用现有）：
  - 日K：engine-data(`wide`) → fstore `day_klines`（**逐标的补缺**，不整批短路）。
  - realtime：tdx-api 单标的 → fstore `daily_markets` 快照（见 ADR-0004）。
  - 补充面（梯队/情绪/板块）：waizao；502 → 降级 0 行 + warning，**不阻断核心接口**（见 ADR-0003）。
- 编排参考 fquant Go 的"遍历 providers、保留 lastErr"结构，但**判定单元下沉到标的级**。

## B3. capabilities 变更

- `realtime: false → true`（tdx-api 提供，**watchlist 逐笔**语义）。全市场"实时"= fstore `daily_markets` **批量快照**语义（近实时、非逐笔），capability/UI 须诚实区分（ADR-0004）。
- `universes: true`（fstore `chengfen_gu` + `base_infos`）。
- **`depth`（5 档盘口）仍为永久缺口**：四源均无盘口，`depth_service` 保持能力门控降级返回空列表。
- 业务入口前的 `provider.capabilities` 检查模式**不变**（如 realtime/depth 缺口时降级）。

## B4. 拆除与保留

- **删除**：`app/tickflow/client.py`（SDK 封装）；`policy.py` / `pools.py` / `capabilities.py` 中的付费档位逻辑；`tickflow_provider.py`；`TICKFLOW_API_KEY` / `tiers.yaml` 档位门控；`pyproject.toml` 的 `tickflow[all]` 依赖。
- **保留**：`app/tickflow/repository.py`（DuckDB/Parquet 存储层，与 SDK 无关）；`data_providers/base.py` 接口契约；`normalizer.py` / `schemas.py`（字段语义稳定，红线不动）。
- **收敛**：`registry` 仅注册 fquant 直连 provider（或保留注册中心但单一 provider）；`DATA_PROVIDER` 环境变量退化/移除。
- **配置**：engine-data / fstore / tdx / waizao 各自 host/port/密码 env；移除档位相关配置。

## B5. 模块边界

| 模块 | 职责 |
|------|------|
| `data_providers/fquant/engine_data_client` | engine-data HTTP 适配（现有，扩展） |
| `data_providers/fquant/fstore_client` | fstore PG 适配（现有，扩展 universes/financial） |
| `data_providers/fquant/tdx_client`（新增） | tdx-api 单标的实时报价适配（现有 `fquant_provider.get_realtime` 已含 tdx-api 调用，抽出成独立 client） |
| `data_providers/fquant/waizao_client`（新增/替换 moneyflow_client） | waizao HTTP：**仅补充面**（梯队 / 板块 / 名称 / 情绪）；**不做核心日 K/实时 fallback**（ADR-0003） |
| `data_providers/fquant_provider` | 聚合四源 + capability + Manager 链式 fallback |
| `data_providers/normalizer` | 字段规范化（**不动**） |

---

# 跨两部分的共同收益与风险

## 收益
- 移除 Go 移植的两处头号障碍（`exec` 策略、Python 专有 SDK）。
- 策略系统更安全（白名单静态校验替代任意代码执行）。
- 数据源自主可控，补上 realtime，甩掉付费依赖。

## 风险 / 注意
- **DSL 表达力天花板**：`window` 目录是封闭集合，超出目录的策略无法表达——需在算子目录设计时覆盖内置 + 可预见需求，后续按需扩目录（每次扩需同步 IR 规范 + 黄金测试 + 各后端 compiler）。
- **跨语言语义漂移**：唯一防线是 A5 的 IR 规范 + 黄金测试，必须先于任一后端 compiler 落地。
- **realtime 语义**：tdx 实时源的字段/延迟需在适配阶段实测对齐 normalizer 契约。
- **红线遵守**：不动 `base.py` 契约的字段语义、不动 `normalizer.py`、不动 `data/` 用户数据；不自动 `git commit`（AGENTS.md 红线 #8）。

# 未列入（YAGNI / 明确排除）
- 存量 AI Python 策略自动迁移工具（D6）。
- depth（盘口）能力（永久缺口）。
- Go 后端 compiler / Go 数据源适配的具体实现（本设计只定义其契约，实现属后续项目）。
