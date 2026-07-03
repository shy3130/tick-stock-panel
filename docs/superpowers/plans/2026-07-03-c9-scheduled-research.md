# C9：定时研究模板实现计划

> **面向 AI 代理的工作者：** 先模板化，不做任意 prompt cron。调度失败不能影响盘后 pipeline。

**目标：** 把现有定时复盘扩展为“定时研究模板”：大盘复盘、自选复盘、策略池周报。结果进入 C2 research evidence / report。

**现状证据：**
- 项目已有 APScheduler：`daily_pipeline.py`、financial/ext pull scheduler。
- C2 将提供 `data/research/hypotheses` 和 run_card/evidence。
- AI 调用有成本，不能开放任意 prompt 定时执行。

**范围：** 后端 store + scheduler + API。前端 UI 后置。

## 文件

| 文件 | 动作 |
|---|---|
| `backend/app/services/scheduled_research.py` | 创建模型/store/executor |
| `backend/app/api/research.py` | 增加 schedule CRUD |
| `backend/app/main.py` | lifespan 注册/关闭 scheduler |
| `backend/tests/services/test_scheduled_research.py` | 创建 |
| `backend/tests/api/test_research_schedule.py` | 创建 |

## 任务 1：模型与 store

- [x] `ScheduledResearch` 字段：
  - `id/name/template/cron/enabled/params`
  - `created_at/updated_at/last_run_at/last_status/last_error`
- [x] template 枚举：
  - `market_recap_daily`
  - `watchlist_recap_daily`
  - `strategy_pool_weekly`
- [x] 存储：`data/research/schedules/{id}.json`
- [x] cron 校验：只支持 5 字段 cron；非法直接 400。

## 任务 2：失败测试

- [x] 创建 schedule 后可 list/get。
- [x] 非法 template 抛 ValueError。
- [x] 非法 cron 抛 ValueError。
- [x] disabled schedule 不注册 APScheduler job。
- [x] executor 失败时写 `last_status=failed`，不抛到 scheduler 外层。

## 任务 3：执行器

- [x] `market_recap_daily`：调用现有 market recap 装配，保存摘要。
- [x] `watchlist_recap_daily`：读取 watchlist + quote/enriched 摘要。
- [x] `strategy_pool_weekly`：读取策略池 + 最近 run_cards。
- [x] 输出统一 `ResearchRunResult`：`title/summary/artifacts/warnings`。
- [x] 不绑定 hypothesis 时保存 report；绑定时追加 C2 evidence。

## 任务 4：API

- [x] `GET /api/research/schedules`
- [x] `POST /api/research/schedules`
- [x] `PATCH /api/research/schedules/{id}`
- [x] `DELETE /api/research/schedules/{id}`
- [x] `POST /api/research/schedules/{id}/run-now`
- [x] 所有 API 不接收自由 prompt。

## 任务 5：scheduler 接入

- [x] lifespan 中启动独立 scheduler 或复用现有 scheduler，避免重复实例。
- [x] schedule CRUD 后 reschedule。
- [x] 应用退出时 shutdown。
- [x] job id 前缀：`research:{id}`。

## 验证

```bash
cd backend
uv run --extra dev pytest tests/services/test_scheduled_research.py tests/api/test_research_schedule.py -q
```

## 非目标

- 不做任意 prompt cron。
- 不做通知通道。
- 不做复杂前端日历。
- 不让失败任务阻断数据同步 pipeline。
