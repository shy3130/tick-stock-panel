# 趋势监控盘中每小时 AI 阶段分析 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将趋势监控中独立的盘中 AI 分析从“每 30 分钟复述指标”升级为“每小时一次的高级证券分析师阶段报告”，同时解释分钟级路径、相邻阶段变化、当日累计结构、通道与形态、量价/资金含义，并分别给持仓者和未参与者可验证的建议。

**Architecture:** 保留现有通用 API 路径、ClickHouse 历史表和实时重点解读，不改变正式买卖信号。后端先用确定性算法从分钟 OHLCV、实时评估快照和上一份报告中提取“可核验市场事实”，再让 LLM 只负责把这些事实组织成结构化阶段报告。调度器按各市场本地整点与连续交易段收盘触发，重启只补最新一个应生成的检查点；前端按新结构渲染，同时兼容历史 30 分钟报告。

**Tech Stack:** Python 3、FastAPI、Pydantic、ClickHouse、pytest、React 18、TypeScript、Vitest、Testing Library、pnpm。

## Global Constraints

- 所有生产代码修改前，先完成权威规格、冲突裁决和 `docs/traceability.yaml` 追踪关系；语义测试先红后绿。
- 实时重点解读与盘中 AI 阶段报告保持独立；阶段报告不得写回或覆盖正式买卖信号。
- LLM 不得自行计算价格、涨跌幅、量比、形态阈值或时间；数值和候选结构全部由后端确定性计算提供。
- 不迁移或删除历史 30 分钟结果；旧记录必须永久可读并通过旧布局回显。
- 正常节奏为每小时一次；连续交易段结束时强制生成收盘阶段报告，首段可能不足 60 分钟。
- 发布、10.28 部署、推送远端仓库均不属于本计划的自动步骤，必须收到用户后续明确授权。
- 不修改用户已有的 `.playwright-cli/`、`.superpowers/brainstorm/` 和 `output/` 未跟踪目录。

---

## File map

| File | Action | Responsibility |
|---|---|---|
| `docs/specs/dow-monitor-half-hour-ai-analysis.md` | Modify | 将最新用户裁决写入既有权威规格，保留历史兼容要求 |
| `docs/decisions/2026-08-01-dow-monitor-hourly-ai-cadence-precedence.md` | Create | 记录“每小时 + 连续交易段收盘”对旧 30 分钟节奏的优先级 |
| `docs/spec-index.yaml` | Modify | 注册新增稳定需求 ID 和已解决规格冲突 |
| `docs/traceability.yaml` | Modify | 将每项需求映射到实现、可执行测试与验收证据 |
| `tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py` | Create | 验证规格索引、需求 ID、追踪字段和验收材料完整性 |
| `backend/app/services/dow_monitor_half_hour_ai_calendar.py` | Modify | 按市场时区生成整点和连续交易段收盘检查点 |
| `backend/app/workers/dow_monitor_half_hour_ai.py` | Modify | 重启时只选最新应执行窗口，并编排新版报告输入输出 |
| `backend/app/services/dow_monitor_hourly_ai_structure.py` | Create | 确定性提取阶段路径、事件、通道、形态、量价结构和阶段对比 |
| `backend/app/services/dow_monitor_half_hour_ai_models.py` | Modify | 增加小时报告结构，同时保持旧模型字段兼容 |
| `backend/app/services/dow_monitor_half_hour_ai_repository.py` | Modify | ClickHouse 增量列、结构化报告读写、上一份报告查询 |
| `backend/app/services/dow_monitor_half_hour_ai_snapshot.py` | Modify | 组装阶段窗口、当日累计窗口、上一阶段上下文和实时快照 |
| `backend/app/services/dow_monitor_half_hour_ai_prompt.py` | Modify | 强制 LLM 输出分析师式结构化报告并校验 metric key |
| `backend/app/services/dow_monitor_service.py` | Modify | 列表返回轻量阶段摘要，详情返回完整报告 |
| `backend/app/api/dow_monitor.py` | Modify only if schema declarations require | 保持现有 URL，公开扩展后的响应结构 |
| `tests/backend/test_dow_monitor_hourly_ai_structure.py` | Create | 对分钟路径、通道、形态和量价事件做语义单元测试 |
| `tests/backend/test_dow_monitor_half_hour_ai.py` | Modify | 调度、仓储、提示词、工作器、API 回归与兼容测试 |
| `tests/backend/test_dow_monitor_offline_ai_bootstrap_integration.py` | Modify | 离线补数与小时检查点集成测试 |
| `frontend/src/components/dow-monitor/types.ts` | Modify | 新增结构化报告 TypeScript 类型 |
| `frontend/src/components/dow-monitor/DowMonitorAiStageReport.tsx` | Create | 渲染新版阶段报告，按批准顺序呈现业务解释和建议 |
| `frontend/src/components/dow-monitor/DowMonitorAiAnalysisDialog.tsx` | Modify | 新报告使用新组件，旧报告保留历史布局 |
| `frontend/src/components/dow-monitor/DowMonitorHalfHourAiButton.tsx` | Modify | 用户文案改为“盘中AI分析/小时阶段分析” |
| `frontend/src/components/dow-monitor/DowMonitorAiStageReport.test.tsx` | Create | 新报告内容、条件和移动端弹窗回归测试 |
| `frontend/src/components/dow-monitor/DowMonitorHalfHourAiButton.test.tsx` | Modify | 更新按钮文案和状态测试 |
| `docs/acceptance/dow-monitor-hourly-ai-stage-analysis.md` | Create | 记录语义、后端、前端和兼容验收证据 |
| `docs/reviews/2026-08-01-dow-monitor-hourly-ai-stage-analysis-review.md` | Create | 独立需求到证据审查与残余风险 |
| `E:/Obsidian-alwin/alwin/longbridge-stock/dow-monitor-system-api-runbook.md` | Modify | 更新生产运行、调度、表字段、API 与验证流程 |

## Public interfaces and compatibility rules

### Backend model additions

`HalfHourAiSummary` 与 `HalfHourAiAnalysis` 保留现有字段，并新增：

```python
report_frequency: Literal["half_hour", "hourly"] = "half_hour"
stage_start: datetime | None = None
stage_trading_minutes: int | None = None
report: HourlyStageReport | None = None
```

`HourlyStageReport` 必须包含以下结构，不允许用自由文本替代整个对象：

```python
class HourlyStageReport(BaseModel):
    headline: StageHeadline
    stage_path: list[StagePathSegment]
    hidden_changes: list[str]
    comparison_with_previous: str
    day_overview: str
    channel: ChannelAssessment
    patterns: list[PatternAssessment]
    volume_capital_interpretation: str
    holding_advice: PositionAdvice
    watching_advice: PositionAdvice
    next_stage_conditions: NextStageConditions
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
```

固定枚举：

- `trend_bias`: `BULLISH | BEARISH | NEUTRAL | TRANSITION`
- `opportunity_change`: `STRENGTHENING | WEAKENING | UNCHANGED | REVERSING`
- `channel.direction`: `UP | DOWN | RANGE | TRANSITION`
- `pattern.status`: `FORMING | CONFIRMED | FAILED | NONE`
- `advice.state`: `FOCUS | WAIT_CONFIRMATION | HOLD_OBSERVE | DEFENSIVE | AVOID_CHASING | REDUCE_RISK`

### API compatibility

- URL 保持 `/api/dow-monitor/{symbol}/ai-analyses` 与 `/api/dow-monitor/{symbol}/ai-analyses/{analysis_id}`。
- 概览只返回 `report_frequency`、`stage_start`、`stage_trading_minutes`、标题、摘要和 `opportunity_change`；不返回长正文。
- 详情返回完整 `report`；旧记录 `report=null` 时继续返回并渲染 `conclusion/evidence/risks/scenarios`。
- ClickHouse 表继续使用 `longbridge.lb_dow_monitor_half_hour_ai_analyses`，通过 `ADD COLUMN IF NOT EXISTS` 扩展，不重写旧数据。

### Deterministic analysis boundary

- 阶段窗口：`stage_start <= decision_minute <= data_cutoff`；分钟结果使用完成分钟的决策时刻，等于检查点的完成分钟必须保留并按同一时刻去重。
- 当日累计：连续交易日 `session_open <= decision_minute <= data_cutoff`；严格排除截止点之后的分钟。
- 上一报告只提供上一阶段状态和结论，不作为当前价格事实来源。
- 阈值集中定义在 `dow_monitor_hourly_ai_structure.py` 的具名常量中，每个阈值均需测试覆盖。

---

## Task 1: Resolve specification authority and establish traceability

**Files:**

- Modify: `docs/specs/dow-monitor-half-hour-ai-analysis.md`
- Create: `docs/decisions/2026-08-01-dow-monitor-hourly-ai-cadence-precedence.md`
- Modify: `docs/spec-index.yaml`
- Modify: `docs/traceability.yaml`
- Create: `tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py`

**Requirement IDs:**

- `REQ-DOW-MONITOR-HOURLY-AI-CADENCE-001`
- `REQ-DOW-MONITOR-HOURLY-AI-STAGE-REPORT-001`
- `REQ-DOW-MONITOR-HOURLY-AI-MINUTE-PATH-001`
- `REQ-DOW-MONITOR-HOURLY-AI-VIEW-001`

- [ ] Write the failing spec contract first. Assert that all four IDs are authoritative, indexed, traced to implementation and executable tests, and reference `docs/acceptance/dow-monitor-hourly-ai-stage-analysis.md`.

Run:

```powershell
python -m pytest tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py -q
```

Expected: FAIL because the new decision, requirement mappings and acceptance evidence do not yet exist.

- [ ] Update the authoritative specification with the approved hourly cadence, close checkpoints, report sections, deterministic/LLM boundary, legacy retention and no-signal-mutation rule.
- [ ] Record precedence: the latest explicit user ruling replaces only the 30-minute cadence; the offline bootstrap bounds, one-report isolation and historical retention remain authoritative.
- [ ] Add the resolved conflict entry to `docs/spec-index.yaml` and add complete traceability entries. Test paths may point to files created by later tasks, but every path and test name must be concrete.
- [ ] Re-run the spec contract and compliance checker.

Run:

```powershell
python -m pytest tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py -q
python scripts/check_spec_compliance.py
```

Expected: PASS.

- [ ] Commit the specification gate.

```powershell
git add docs/specs/dow-monitor-half-hour-ai-analysis.md docs/decisions/2026-08-01-dow-monitor-hourly-ai-cadence-precedence.md docs/spec-index.yaml docs/traceability.yaml tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py
git commit -m "docs(dow-monitor): authorize hourly AI stage analysis"
```

## Task 2: Implement hourly and continuous-session-close scheduling

**Files:**

- Modify: `backend/app/services/dow_monitor_half_hour_ai_calendar.py`
- Modify: `backend/app/workers/dow_monitor_half_hour_ai.py`
- Modify: `tests/backend/test_dow_monitor_half_hour_ai.py`

**Interfaces:**

- `HalfHourWindowCalendar.session_window_ends` keeps its internal name but returns hourly/close checkpoints.
- `select_due_windows` returns at most one checkpoint: the newest eligible non-terminal checkpoint.

- [ ] Add RED tests for exact market-local schedules:
  - US regular session: `10:00, 11:00, 12:00, 13:00, 14:00, 15:00, 16:00`.
  - CN regular session: `10:00, 11:00, 11:30, 14:00, 15:00`.
  - HK regular session: `10:00, 11:00, 12:00, 14:00, 15:00, 16:00`.
  - DST conversion is correct for US sessions.
  - Restart with multiple missed checkpoints selects only the latest; a terminal latest checkpoint does not fall back to older work.

Run:

```powershell
python -m pytest tests/backend/test_dow_monitor_half_hour_ai.py -q -k "calendar or due_window or restart"
```

Expected: FAIL against the 30-minute implementation.

- [ ] Replace interval stepping with “first whole local hour strictly after segment start + subsequent whole hours + segment end if absent”.
- [ ] Change restart selection to a single newest eligible checkpoint while preserving startup eligibility and terminal-state protection.
- [ ] Re-run the focused tests, then the whole file.

```powershell
python -m pytest tests/backend/test_dow_monitor_half_hour_ai.py -q -k "calendar or due_window or restart"
python -m pytest tests/backend/test_dow_monitor_half_hour_ai.py -q
```

Expected: PASS.

- [ ] Commit scheduler semantics.

```powershell
git add backend/app/services/dow_monitor_half_hour_ai_calendar.py backend/app/workers/dow_monitor_half_hour_ai.py tests/backend/test_dow_monitor_half_hour_ai.py
git commit -m "feat(dow-monitor): schedule hourly AI stage checkpoints"
```

## Task 3: Build deterministic minute-path and market-structure extraction

**Files:**

- Create: `backend/app/services/dow_monitor_hourly_ai_structure.py`
- Create: `tests/backend/test_dow_monitor_hourly_ai_structure.py`

**Interfaces:**

```python
def build_hourly_market_structure(
    *,
    minute_rows: Sequence[MinuteRow],
    stage_start: datetime,
    data_cutoff: datetime,
    previous_stage: PreviousStageContext | None,
) -> HourlyMarketStructure
```

`HourlyMarketStructure` must expose stage aggregate, 5-minute segments, hidden events, cumulative-day aggregate, channel candidate, pattern candidates, volume distribution and comparison with the previous stage.

- [ ] Add RED semantic tests using small hand-calculated OHLCV fixtures for:
  - a low-before-last-third V repair with close above stage VWAP;
  - a symmetric inverted-V failure;
  - true breakout versus false breakout of pre-stage session high/low;
  - rising channel, falling channel, range and transition;
  - first-half/second-half volume comparison and final-five-minute volume share;
  - consecutive up/down runs and exact high/low timestamps;
  - no duplicated cutoff bar and insufficient-data degradation;
  - previous-stage opportunity strengthening, weakening, unchanged and reversing.

Run:

```powershell
python -m pytest tests/backend/test_dow_monitor_hourly_ai_structure.py -q
```

Expected: FAIL because the extractor does not exist.

- [ ] Implement normalized minute-row ordering and named constants for every threshold.
- [ ] Implement aggregate OHLCV/VWAP/range/close-position calculations and 5-minute subsegments.
- [ ] Implement channel and pattern candidate logic. Pattern outputs must carry status, evidence metric keys and invalidation metric keys.
- [ ] Implement previous-stage comparison from structured states only.
- [ ] Re-run the semantic tests; manually verify at least one fixture calculation in assertions rather than snapshots.

```powershell
python -m pytest tests/backend/test_dow_monitor_hourly_ai_structure.py -q
```

Expected: PASS.

- [ ] Commit the deterministic semantic layer.

```powershell
git add backend/app/services/dow_monitor_hourly_ai_structure.py tests/backend/test_dow_monitor_hourly_ai_structure.py
git commit -m "feat(dow-monitor): derive hourly minute market structure"
```

## Task 4: Extend models and ClickHouse storage without breaking history

**Files:**

- Modify: `backend/app/services/dow_monitor_half_hour_ai_models.py`
- Modify: `backend/app/services/dow_monitor_half_hour_ai_repository.py`
- Modify: `tests/backend/test_dow_monitor_half_hour_ai.py`

**Storage additions:**

```sql
report_frequency LowCardinality(String) DEFAULT 'half_hour'
stage_start Nullable(DateTime64(3, 'UTC'))
stage_trading_minutes Nullable(UInt16)
report_json String DEFAULT '{}'
```

- [ ] Add RED tests for model validation, enum rejection, old-row deserialization, `ADD COLUMN IF NOT EXISTS`, new-row round trip and strict ordering in `latest_completed_before`.

Run:

```powershell
python -m pytest tests/backend/test_dow_monitor_half_hour_ai.py -q -k "model or repository or legacy or latest_completed"
```

Expected: FAIL because the schema and fields are absent.

- [ ] Add the explicit Pydantic structures and backward-compatible defaults described in Public interfaces.
- [ ] Extend `ensure_schema()` with idempotent ALTER statements; serialize the new report to `report_json` and parse `{}` as `None`.
- [ ] Implement `latest_completed_before(market, symbol, trade_date, window_end)` and exclude failed/running/insufficient rows.
- [ ] Run focused and full backend tests.

```powershell
python -m pytest tests/backend/test_dow_monitor_half_hour_ai.py -q -k "model or repository or legacy or latest_completed"
python -m pytest tests/backend/test_dow_monitor_half_hour_ai.py -q
```

Expected: PASS.

- [ ] Commit storage compatibility.

```powershell
git add backend/app/services/dow_monitor_half_hour_ai_models.py backend/app/services/dow_monitor_half_hour_ai_repository.py tests/backend/test_dow_monitor_half_hour_ai.py
git commit -m "feat(dow-monitor): persist structured hourly AI reports"
```

## Task 5: Assemble stage, cumulative-session and previous-report context

**Files:**

- Modify: `backend/app/services/dow_monitor_half_hour_ai_snapshot.py`
- Modify: `backend/app/services/dow_monitor_hourly_ai_structure.py`
- Modify: `tests/backend/test_dow_monitor_hourly_ai_structure.py`
- Modify: `tests/backend/test_dow_monitor_half_hour_ai.py`

**Interfaces:**

`build_snapshot` must produce:

- exact `stage_start`, `data_cutoff` and trading-minute count;
- stage-only minute path and cumulative-session facts;
- deterministic market structure;
- latest realtime evaluation facts with freshness;
- prior completed report context when present;
- data-quality reasons when any source is stale or incomplete.

- [ ] Add RED tests proving lunch breaks are not counted as trading minutes, the stage excludes earlier bars, cumulative scope retains them, and previous report context is absent/present deterministically.
- [ ] Add a test proving stale realtime data lowers quality but does not erase valid minute-K structure.

```powershell
python -m pytest tests/backend/test_dow_monitor_hourly_ai_structure.py tests/backend/test_dow_monitor_half_hour_ai.py -q -k "snapshot or stage_scope or trading_minutes or previous_context or stale"
```

Expected: FAIL.

- [ ] Integrate the pure extractor into snapshot assembly and expose all numeric facts through stable `metric_key` entries.
- [ ] Keep raw input serialization bounded: include 5-minute segments and detected events, not the entire unbounded session payload in the prompt.
- [ ] Re-run the focused tests.

```powershell
python -m pytest tests/backend/test_dow_monitor_hourly_ai_structure.py tests/backend/test_dow_monitor_half_hour_ai.py -q -k "snapshot or stage_scope or trading_minutes or previous_context or stale"
```

Expected: PASS.

- [ ] Commit context assembly.

```powershell
git add backend/app/services/dow_monitor_half_hour_ai_snapshot.py backend/app/services/dow_monitor_hourly_ai_structure.py tests/backend/test_dow_monitor_hourly_ai_structure.py tests/backend/test_dow_monitor_half_hour_ai.py
git commit -m "feat(dow-monitor): assemble hourly stage analysis context"
```

## Task 6: Require analyst-style structured LLM output

**Files:**

- Modify: `backend/app/services/dow_monitor_half_hour_ai_prompt.py`
- Modify: `tests/backend/test_dow_monitor_half_hour_ai.py`

**Prompt contract:**

- Role: senior intraday securities analyst.
- Explain what happened and why it matters; do not list indicators as the conclusion.
- Cover stage path, hidden minute changes, comparison with previous report, cumulative session, channel, patterns, volume/capital interpretation, separate holder/watcher advice and next-stage conditions.
- Any numeric claim must point to a provided `metric_key`; unknown keys and invented prices fail validation.
- Advice is conditional and cannot emit formal signal mutations, position percentages or order instructions.

- [ ] Add RED tests with a fake model for valid output, missing required section, unknown metric key, invented evidence, enum error and legacy fallback.
- [ ] Add a semantic test that rejects an output consisting only of indicator narration even when JSON shape is valid.

```powershell
python -m pytest tests/backend/test_dow_monitor_half_hour_ai.py -q -k "prompt or model_output or metric_key or analyst"
```

Expected: FAIL.

- [ ] Update the prompt and parser to the new structured schema; increase the completion budget to 3200 tokens and keep deterministic temperature settings.
- [ ] Reject invalid output before repository save and retain data-quality/failure details.
- [ ] Re-run focused tests.

```powershell
python -m pytest tests/backend/test_dow_monitor_half_hour_ai.py -q -k "prompt or model_output or metric_key or analyst"
```

Expected: PASS.

- [ ] Commit prompt and validation behavior.

```powershell
git add backend/app/services/dow_monitor_half_hour_ai_prompt.py tests/backend/test_dow_monitor_half_hour_ai.py
git commit -m "feat(dow-monitor): generate analyst-style hourly reports"
```

## Task 7: Integrate worker execution and offline bootstrap

**Files:**

- Modify: `backend/app/workers/dow_monitor_half_hour_ai.py`
- Modify: `tests/backend/test_dow_monitor_half_hour_ai.py`
- Modify: `tests/backend/test_dow_monitor_offline_ai_bootstrap_integration.py`

**Behavior:**

- Load minute data through the current checkpoint before invoking the model.
- Use offline data immediately when available; do not wait one hour for WebSocket accumulation.
- Preserve asynchronous persistence and one-model-call concurrency.
- Save `report_frequency="hourly"`, exact interval metadata and the structured report.
- Never generate reports for stocks outside trend monitoring or outside their regular session.

- [ ] Add RED integration tests for a newly added symbol with offline history, a restart with several missed hourly checkpoints, regular-session filtering, model failure isolation and no duplicate completed checkpoint.

```powershell
python -m pytest tests/backend/test_dow_monitor_half_hour_ai.py tests/backend/test_dow_monitor_offline_ai_bootstrap_integration.py -q -k "worker or offline or restart or regular_session or duplicate"
```

Expected: FAIL.

- [ ] Wire repository previous-report lookup, snapshot assembly, model invocation and structured save into `_run_checkpoint()`.
- [ ] Ensure one symbol failure records its own failure and does not block other monitored symbols.
- [ ] Re-run the focused integration tests and both full files.

```powershell
python -m pytest tests/backend/test_dow_monitor_half_hour_ai.py tests/backend/test_dow_monitor_offline_ai_bootstrap_integration.py -q
```

Expected: PASS.

- [ ] Commit worker integration.

```powershell
git add backend/app/workers/dow_monitor_half_hour_ai.py tests/backend/test_dow_monitor_half_hour_ai.py tests/backend/test_dow_monitor_offline_ai_bootstrap_integration.py
git commit -m "feat(dow-monitor): run isolated hourly AI stage jobs"
```

## Task 8: Expose lightweight summaries and render full reports on demand

**Files:**

- Modify: `backend/app/services/dow_monitor_service.py`
- Modify only if needed: `backend/app/api/dow_monitor.py`
- Modify: `tests/backend/test_dow_monitor_half_hour_ai.py`
- Modify: `frontend/src/components/dow-monitor/types.ts`
- Create: `frontend/src/components/dow-monitor/DowMonitorAiStageReport.tsx`
- Modify: `frontend/src/components/dow-monitor/DowMonitorAiAnalysisDialog.tsx`
- Modify: `frontend/src/components/dow-monitor/DowMonitorHalfHourAiButton.tsx`
- Create: `frontend/src/components/dow-monitor/DowMonitorAiStageReport.test.tsx`
- Modify: `frontend/src/components/dow-monitor/DowMonitorHalfHourAiButton.test.tsx`

- [ ] Add RED backend API tests proving overview responses exclude long report content, detail responses include it, and old records remain readable.
- [ ] Add RED frontend tests for approved section order, exact stage interval, channel/pattern display, holder/watcher advice, strengthen/risk/invalidation conditions and legacy fallback.
- [ ] Add a narrow-screen test proving the existing mobile first-four-column layout remains intact and the report opens in its separate dialog.

```powershell
python -m pytest tests/backend/test_dow_monitor_half_hour_ai.py -q -k "api or summary or detail or legacy"
pnpm --dir frontend test --run src/components/dow-monitor/DowMonitorAiStageReport.test.tsx src/components/dow-monitor/DowMonitorHalfHourAiButton.test.tsx
```

Expected: FAIL.

- [ ] Extend backend summary/detail mapping without changing URLs.
- [ ] Add TypeScript discriminated structures matching Pydantic enums and nullability exactly.
- [ ] Implement the new report component in this order: headline, this-hour path, hidden minute changes, previous-stage comparison, day-to-now overview, channel/pattern, volume/capital meaning, holder advice, watcher advice, next-stage confirmation/risk/invalidation, data quality.
- [ ] Change user-facing “半小时分析” to “盘中AI分析” and show “小时阶段分析” on new records; retain the original label on legacy records only where it clarifies historical frequency.
- [ ] Re-run backend and frontend tests, lint and build.

```powershell
python -m pytest tests/backend/test_dow_monitor_half_hour_ai.py -q
pnpm --dir frontend test --run src/components/dow-monitor/DowMonitorAiStageReport.test.tsx src/components/dow-monitor/DowMonitorHalfHourAiButton.test.tsx
pnpm --dir frontend lint
pnpm --dir frontend build
```

Expected: PASS.

- [ ] Commit API and UI changes.

```powershell
git add backend/app/services/dow_monitor_service.py backend/app/api/dow_monitor.py tests/backend/test_dow_monitor_half_hour_ai.py frontend/src/components/dow-monitor/types.ts frontend/src/components/dow-monitor/DowMonitorAiStageReport.tsx frontend/src/components/dow-monitor/DowMonitorAiAnalysisDialog.tsx frontend/src/components/dow-monitor/DowMonitorHalfHourAiButton.tsx frontend/src/components/dow-monitor/DowMonitorAiStageReport.test.tsx frontend/src/components/dow-monitor/DowMonitorHalfHourAiButton.test.tsx
git commit -m "feat(dow-monitor): present hourly AI stage reports"
```

## Task 9: End-to-end semantic acceptance, runbook and independent review

**Files:**

- Create: `docs/acceptance/dow-monitor-hourly-ai-stage-analysis.md`
- Create: `docs/reviews/2026-08-01-dow-monitor-hourly-ai-stage-analysis-review.md`
- Modify: `docs/traceability.yaml`
- Modify: `E:/Obsidian-alwin/alwin/longbridge-stock/dow-monitor-system-api-runbook.md`

- [ ] Run the semantic layer before downstream suites; record command, date, commit and result in acceptance evidence.

```powershell
python -m pytest tests/backend/test_dow_monitor_hourly_ai_structure.py -q
python -m pytest tests/spec_contracts/test_dow_monitor_hourly_ai_stage_contract.py tests/backend/test_dow_monitor_half_hour_ai.py tests/backend/test_dow_monitor_offline_ai_bootstrap_integration.py -q
```

Expected: PASS. A frontend snapshot alone is not acceptable semantic proof.

- [ ] Run broader backend and frontend verification.

```powershell
python -m pytest tests/backend -q
pnpm --dir frontend test --run
pnpm --dir frontend lint
pnpm --dir frontend build
python scripts/check_spec_compliance.py
git diff --check
```

Expected: PASS. If a pre-existing unrelated failure occurs, record its exact command/output and prove the feature-focused suites still pass; do not silently waive it.

- [ ] Perform a deterministic NBIS.US replay with locally available minute data. Confirm that the produced structured facts identify the minute path, stage change, cumulative structure, channel/pattern status and volume behavior before reviewing LLM prose.
- [ ] Verify the full report in desktop and mobile widths, plus one historical 30-minute record. Record API payload IDs and screenshots/observations in the acceptance file without committing generated `output/` artifacts.
- [ ] Update the runbook with hourly schedules by market, close checkpoints, table columns, model invocation boundary, offline bootstrap, API payload shape, legacy fallback, 3018/19912 service distinction and static bundle verification.
- [ ] Conduct an independent requirements-to-evidence review. For each active requirement ID, cite the authoritative clause, implementation symbol, executable test and acceptance evidence; explicitly check no formal signal mutation and no dependency on WebSocket accumulation.
- [ ] Scan for placeholders and type drift.

```powershell
rg -n "TODO|TBD|placeholder|NotImplemented|pass\s*(#.*)?$" backend/app/services/dow_monitor_* backend/app/workers/dow_monitor_half_hour_ai.py frontend/src/components/dow-monitor docs/acceptance/dow-monitor-hourly-ai-stage-analysis.md docs/reviews/2026-08-01-dow-monitor-hourly-ai-stage-analysis-review.md
rg -n "half_hour|hourly|stage_start|stage_trading_minutes|report_frequency" backend frontend/src/components/dow-monitor tests docs/specs/dow-monitor-half-hour-ai-analysis.md
git status --short
```

Expected: no production placeholders; Python/TypeScript/storage/API names and nullability are consistent; only intended files plus the known unrelated untracked directories appear.

- [ ] Commit repository acceptance and traceability evidence. The Obsidian runbook is outside this Git worktree; verify its saved contents separately and do not pass its absolute path to this repository's `git add`.

```powershell
git add docs/acceptance/dow-monitor-hourly-ai-stage-analysis.md docs/reviews/2026-08-01-dow-monitor-hourly-ai-stage-analysis-review.md docs/traceability.yaml
git commit -m "docs(dow-monitor): verify hourly AI stage analysis"
Get-Item E:/Obsidian-alwin/alwin/longbridge-stock/dow-monitor-system-api-runbook.md | Select-Object FullName, LastWriteTime, Length
```

## Task 10: Publication gate after explicit authorization

This task must remain unexecuted until the user explicitly requests publication of this completed feature.

- [ ] Re-run the complete Task 9 verification on the exact commit to be published.
- [ ] Confirm the target is the 10.28 production host, not localhost, and use the repository’s documented deployment process.
- [ ] Apply the ClickHouse idempotent schema migration before restarting the worker/API that writes new columns.
- [ ] Deploy backend and freshly built frontend bundle; verify ports `3018` and `19912` according to the runbook.
- [ ] Smoke-test one monitored in-session symbol, one mobile viewport, the generic AI-analysis endpoints and one old 30-minute report.
- [ ] Verify scheduler logs show at most one newest due checkpoint on restart and no 30-minute recurring calls.
- [ ] Only if the user separately asks for GitHub publication, push the reviewed branch and report the exact remote branch/commit.

Expected publication evidence: production URL/API response, deployed commit SHA, bundle version, schema columns, worker checkpoint log and rollback command recorded in the runbook/acceptance addendum.
