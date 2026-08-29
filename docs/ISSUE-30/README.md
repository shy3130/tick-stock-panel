# ISSUE-30 日线开盘价锚定入场过滤研究（daily-open-anchor-filter）

> 状态：**真实 OOS 已完成，冻结门禁 verdict=`inconclusive`（原稿臂样本不足）；待本次 verdict 文档 PR 合并后关闭 Issue**。
> 日期：2026-08-29 · 基线：`7bf2982` · GitHub Issue：[wf2311/fm-workbench#30](https://github.com/wf2311/fm-workbench/issues/30)

## 这是什么

研究问题：「收盘确认信号之后，仅当价格仍处于最近一根已收阳线的开盘价锚附近/下方时才允许入场」这一入场位置过滤器，能否在期望收益不恶化的前提下降低止损触发率。原稿仅有单一案例，不作采信依据；本研究以 sealed 数据四臂对照（无过滤 / 原稿 / 反向 / 确定性随机）回答，结论只由 OOS 决定。

v1/v2/v3 均冻结为 A 股多头、日频收盘确认、下一可交易日开盘执行；盘中小周期因 sealed 分钟覆盖不足而 `unavailable`。权威契约见 [final-design.md](final-design.md)。

## 范围与非目标

- 代理信号 MA5/MA20 金叉；锚为 signal_date 之前最近收阳线 open；统一 pinned canonical 前复权口径，PIT 限价带在 raw 尺度计算后附加 flags。
- 四臂共享同一 signal universe；被过滤样本由 none 臂逐候选终态提供虚拟结局；IS/OOS 按 signal_date 分段，verdict 只读 OOS。
- 不做盘中、空头/期货/期权/融券、FVG/OB、前端、策略池/optimizer/Agent、真实交易、全市场生产回测或 `data/` 写入；不采信原稿收益主张。

## 文档索引

| 文档 | 作用 |
|------|------|
| [feasibility.md](feasibility.md) | 可行性、数据事实、缺口与 fail-closed 边界 |
| [plan-v1.md](plan-v1.md) | 初版契约（历史记录） |
| [review-v1.md](review-v1.md) | 首轮独立评审：5 项 finding，Reject |
| [plan-v2.md](plan-v2.md) | 首轮评审后的修订契约（历史记录） |
| [review-v2.md](review-v2.md) | 二次独立评审：3 项 major，Reject |
| [plan-v3.md](plan-v3.md) | 修复二次评审 M1–M3 的实施契约（已批准） |
| [review-v3.md](review-v3.md) | 最终门禁：Approve，附一项已处置 P2 |
| [final-design.md](final-design.md) | plan-v3 权威副本（已批准） |
| [coding-review.md](coding-review.md) | 独立 coding review 九项修复与 strict reader/依赖处置记录 |
| [verification.md](verification.md) | 主会话首验与集成后证据、最终 review 结论 |
| [oos-verdict.json](oos-verdict.json) | 真实 OOS 请求、immutable provenance、四臂/TNT 对照与 verdict 的机器可读摘要 |

## 代码落点（已实现，主会话验证已通过）

| 文件 | 作用 |
|------|------|
| `backend/app/services/daily_open_anchor_filter.py` | sealed reader 装配、raw PIT bands、锚点、四臂、execution ledger、candidate stats、IS/OOS verdict、scripts/tnt 趋势对照（tnt_open_anchor_contrast） |
| `backend/app/api/research.py` | capability GET + factor evaluate POST（fail-closed 400/503 映射） |
| `backend/tests/services/test_daily_open_anchor_filter.py` | raw/adjusted corporate-action、T+1、终态 ledger、虚拟结局与统计夹具 |
| `backend/tests/api/test_daily_open_anchor_evaluate_api.py` | API、markets pin、fail-closed、reader 生命周期与边界校验 |

## 验收标准（照录 Issue #30，当前实现及依赖集成已完成）

- [x] `docs/ISSUE-30/` 完成可行性、两轮方案 review、最终设计与验证记录。
- [x] 锚点严格满足 `anchor_date < signal_date`，新锚切换时点唯一，可截断复算，无未来函数。
- [x] 四臂共享同一代理信号全集；保留/过滤样本与被过滤样本虚拟结局并列披露。
- [x] PIT 精确涨跌停事实缺失整单 fail-closed；T+1、停牌、跳空止损、费用滑点均生效。
- [x] 分层披露趋势/震荡、高开/低开、距锚距离，结论只由 OOS 决定。
- [x] 覆盖阳/阴线锚、高低平开、单边下跌、跳空穿越、新锚切换、无信号、停复牌、缺数据、随机确定性和涨跌停夹具。
- [x] 定向测试、后端全量回归与 Ruff F/E9 通过；独立 coding review 无 blocker/major。

实现、依赖集成、工程验证和真实 OOS 均已完成；最终工程 review 无 blocker/major。OOS 因原稿臂仅 9 笔而按冻结的 30 笔门槛得到 `inconclusive`，不是失败或通过。

## scripts/tnt 单边趋势日对照（PR #32 后遗漏修正，已完成待 PR）

`docs/TODO.md`「日线开盘价锚定入场位置过滤器」要求与 `scripts/tnt/` 做T研究对照：其预注册结论为**单边趋势日（尤其单边下跌）按开盘价锚定入场为接飞刀**。PR #32 合并时遗漏该对照，本次补齐（2026-08-28）：

- 评估响应顶层追加 `tnt_open_anchor_contrast`：只读 `arms[*].segments.oos.layers.trend_bucket`，不重扫交易、不改变过滤 mask、不读取 IS（`read_scope=oos_only`）。
- 对 `single_side_down` 与 `range` 分别披露 none/original 的 `n_trades`、`stop_hit_rate`、`expectancy` 与比较状态 `improved|adverse|neutral|inconclusive`；任一臂 `n_trades < MIN_OOS_TRADES(30)` 或指标缺失即 `inconclusive`。
- 冻结判定：`adverse` = original 止损触发率更高或期望更低；`improved` = 止损触发率更低且期望不低于 none；其余 `neutral`。
- verdict 追加 `applicability` 与 `warnings`：`label=rejected` → `not_applicable_rejected`；`label=inconclusive` → `inconclusive_overall`；仅整体 `validated` 进入趋势状态机：任一桶 `inconclusive` → `inconclusive_by_trend`，双桶 `adverse` → `unsupported_in_preregistered_regimes`，单边下跌或震荡单桶 `adverse` → `conditional_by_trend` 并写对应 warning；仅两桶均为 `improved|neutral` → `all_regimes`。既有 `label` 规则不变。
- 【已废止，由下方 PR #33 后续日型/波动率修正替代】当时的日频代理声明（信号日前可计算的个股 5 日趋势桶，±3% 阈值）不再成立：现行 `trend_bucket` 为 execution-day body_ratio 口径，非信号日 5 日动量；本节其余对照结构（OOS-only、桶状态机、判定规则）继续有效。

本节与对应代码、测试为 PR #32 之后的补充修正，已由主会话验证闭合：定向测试 **45 passed**、后端全量 **3508 passed / 3 skipped / 8 warnings / 131.21s**、Ruff（F/E9）通过、独立最终 review **Approve（无 blocker/major/minor）**，证据见 [verification.md](verification.md)。

## PR #33 后续日型/波动率修正（2026-08-28，完成待 PR）

PR #33 仅完成 TNT 对照首修；其后发现原 `trend_bucket` 的 5 日动量代理并非执行日形态，本波按共享 Contract 修正：

- `trend_bucket` 仅诊断 planned execution day 的完整日线，`body_ratio=(close-open)/(high-low)`；`>=0.60` 为 `single_side_up`，`<=-0.60` 为 `single_side_down`，其余 `range`，高低无效或 `high<=low` 为 `unavailable_shape`。
- 新增 `volatility_bucket`：执行日 `true_range_pct=max(high-low, abs(high-prev_close), abs(low-prev_close))/prev_close`，基准为执行日前连续 20 个完整市场日同口径 TR% 的 `statistics.median`；比例 `>=1.50`/`<=0.75`/其余分别为 high/low/normal，缺历史、前收或基准非正为 `insufficient_history`。
- 两项都是 execution-day、post-entry、read-only diagnosis，不参与 precheck、retention 或 engine 输入；`volatility_bucket` 已进入 candidate、ledger、event、segment layers。`SCHEMA_VERSION=2`、`EXECUTION_LEDGER_VERSION=3`。
- TNT 对照来源改为实际存在的 Obsidian 笔记 `clipper/2026-08-15-bollinger-volatility-t-strategy-research.md`；笔记列出的 `scrpits/tnt/*.py` 均标记 `missing_not_in_repository`，不进行代码复现。本波已由主会话验证闭合：focused **59 passed**、后端全量 **3522 passed / 3 skipped / 8 warnings / 119.24s**、Ruff（F/E9）通过、独立最终 review **Approve（无 blocker/major/minor）**，证据见 [verification.md](verification.md)。

## 真实 OOS 收口（2026-08-29）

- canonical generation `20260829T002957-4b1bfcad`，manifest SHA-256 `0d5b5a457e7fa8c25bb047005b20cc6ca06ed19092f7ce20ba65f4604dfdd372`。
- markets generation `20260829T000704`，manifest SHA-256 `a2a9d2b8208af33f4bcb66bcbe46a02ee836659c337deab4d0fd550ffead22a8`。
- evaluator revision：commit `46c3dbc`（blob `80e5ca89`），clean rerun source tree HEAD `5e7121b`；以 fresh Python process 执行，evaluator 与 repository 文件均无 dirty diff，详见 [verification.md](verification.md)。
- 确定性 10 标的、370 天上限运行 `status=ok`；整体 OOS 四臂：none 55 笔（stop 0.345, exp -0.0154），original 9 笔（stop 0.111, exp -0.0144），inverted 46 笔（stop 0.391, exp -0.0156），random 16 笔（stop 0.188, exp -0.0167）。original 臂低于预注册最少 30 笔，verdict=`inconclusive`。
- TNT 分层同样不足：`single_side_down` original 为 0 笔，`range` original 为 7 笔，两桶均 `inconclusive`。
- 结果不支持将过滤器升级为 validated，也不进入默认策略池、Agent 排序或真实交易。

机器可读摘要见 [oos-verdict.json](oos-verdict.json)，完整执行解释见 [verification.md](verification.md)。

## 红线

sealed-only；canonical 与 markets generation 均 immutable pin；raw PIT bands 只与 raw OHLC 比较，缺必需事实整单 fail-closed；execution ledger 不伪造成交；研究 payload 仅统计性结果，无订单/方向/仓位建议；不触碰 `data/`、`short_pool`、Agent 运行时。请求上限 symbols≤200、window≤370、signals≤1000；四臂逐候选调用成本必须披露。
