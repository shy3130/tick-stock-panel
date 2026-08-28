# ISSUE-38 可行性评估

关联：[Issue #38](https://github.com/wf2311/fm-workbench/issues/38) · [README](README.md)

## 结论

**有条件可行。** 现有 generation-pinned canonical reader、canonical manifest 固定的 PIT daily market facts、独立候选撮合和研究 API 契约足以承载四个日频因子。但 TODO 中的规则不能原样实现：F1 丢失两日量能互补语义，F2/F3/F4 丢失横盘、低位或底部前置，多个阈值与关键位未定义。生产 canonical schema v2 完整 raw/source hashes 未发布前，真实研究必须保持 `unavailable`。

## 原稿核验

来源：`~/share/note/obsidian-note/clipper/2026-08-27-qinchuan-four-types-hold-firm.md`。

1. **F1 首阴**：原稿是“首阴日量能状态 → 次日相反方向的量能互补”，不是首阴当日相对前一日落在 `>=1.5x OR <=0.7x` 即通过。信号评估日至少在首阴次日收盘。应增加“首阴破 MA5”专属对照。
2. **F2 突破回踩**：原稿有长期横盘前置；缩量基准、前高定义、回踩窗口和“持续走低”均需冻结。
3. **F3 缓坡**：必须保留低位前置；“小阴小阳”不能写成窗口内每日涨幅都大于零；量能递减需用可计算趋势而非严格单调；“控盘”只能是待证伪标签。
4. **F4 平台突破**：必须保留底部前置；平台振幅与均量窗口需唯一；守实体底、守突破日收盘和跌破关键位应作为分层，不得事后挑选。
5. `20日/60%/3%/15%/5日/5%/1.5x/0.7x` 均是笔记自定参数，不是视频给出的数值，必须完整登记和做受限敏感性分析。

## 可复用基础设施

| 能力 | 现有位置 | 使用边界 |
|---|---|---|
| sealed canonical 日线 | `backend/app/services/research_sealed_data.py::PublishedCanonicalDailyReader` | 构造时固定 generation/manifest；不跟随 current、不合并 mutable overlay |
| PIT raw/涨跌停/ST/停牌事实 | `backend/app/data_providers/fquant/daily_market_research.py::load_pinned_market_facts` | 从 canonical manifest 固定 markets generation；unknown 必须 fail-closed |
| 研究 API | `backend/app/api/research.py` | 新增独立 capability/evaluate 入口；Pydantic `extra="forbid"` |
| 事件研究范式 | `services/n_shape_golden_phoenix.py`、`single_yang_no_break.py`、`volume_breakout.py` | 复用 provenance、删失、IS/OOS 与免责声明，不复用其信号定义 |
| 日频持有/可达性范式 | `services/zuoyi_defense.py`、`daily_open_anchor_filter.py` | 复用 T+1、pending exit、涨跌停阻塞、跳空与成本语义 |
| 指标事实 | `backend/app/indicators/pipeline.py` | adjusted 价格用于跨公司行动结构和收益；raw 价格仅用于制度/成交证据 |

## 推荐集成边界

- 新建一个深模块 `services/hold_firm_patterns.py`，包含四个纯检测器和一套共享但不合并分母的研究执行器；避免创建四套几乎相同的 reader/API/统计实现。
- API 建议为 `POST /api/research/factors/hold-firm-patterns/evaluate` 与 capability GET；响应顶层按 `factor_id` 返回恰好四个独立结果。
- F1-F4 共享同一请求内 canonical/markets pins、market calendar 和执行规则；每个因子拥有独立事件 ID、baseline arms、censor ledger、IS/OOS stats 和 verdict。
- v1 仅后端研究契约，不进入 short pool。只有真实 OOS 逐因子达到门槛后，才另 Issue 讨论候选池接入。

## 数据与口径缺口

1. **生产 generation 门**：依赖 canonical schema v2 完整 adjusted/raw OHLCV 与 source hashes；不满足时整单 unavailable。
2. **市场环境分层**：现有 sealed reader 没有可直接证明的 PIT 全市场 regime 历史。不得用请求 symbols 的横截面冒充市场。v1 可输出个股趋势/波动诊断，但 Issue 验收中的市场级分层在缺少同 generation 来源时必须显式 unavailable。
3. **题材/人气**：不属于这四个因子的 v1 输入，禁止加入事后标签。
4. **成交可达性**：F1 连板样本尤其容易涨停买不到、跌停卖不出；所有不可执行事件必须保留在 denominator audit。
5. **公司行动**：结构窗口和收益使用同一 pinned generation 的 adjusted 价格；PIT 涨跌停与实际成交证据使用对应 raw 价格，禁止交叉比较。

## Fail-closed 规则

- canonical/markets pin、manifest hash、required columns 或 PIT 制度任一不完整：整单 `unavailable`。
- 单标的 warmup/horizon/bar 缺失：显式 event censor，不得静默删样本。
- `is_st`、停牌、buyable/sellable 或必要 raw quote 为 unknown：涉及制度/执行的研究整单 unavailable，不能猜。
- 市场级 regime 来源不可证明：该分层 unavailable，不伪造市场桶；不得影响核心因子信号。

## 非目标

前端、默认短线池、Agent、监控、optimizer、真实交易、订单语义、外部行情、写 `data/`、复现视频收益（视频没有收益数据）以及把“主力/洗盘/控盘”变成事实字段。
