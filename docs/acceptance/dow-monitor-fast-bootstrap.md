# 趋势监控快速首屏语义验收

状态：本地语义验收与 10.28 正式性能验收均通过。

## 验收范围

- `REQ-DOW-MONITOR-FAST-BOOTSTRAP-001`
- `REQ-DOW-MONITOR-LIGHTWEIGHT-LIST-OVERVIEW-001`
- `REQ-DOW-MONITOR-NOTIFICATION-SUMMARY-001`
- `REQ-DOW-MONITOR-STARTUP-PERFORMANCE-001`

## 下层语义证据

- `tests/backend/test_dow_monitor_fast_bootstrap.py`：4 项通过。证明轻量 overview
  每次只读取一次状态集合、legacy overview 不再逐周期读盘、五周期裁剪边界、
  bars/turning 详情字段裁剪、通知摘要不含大快照、未变化 JSONL 不重载，
  以及 20 只股票/100 条通知的载荷门槛。
- `backend/tests/test_dow_monitor_api.py`：40 项通过。证明旧 overview、完整通知、
  已读回执、详情和既有监控 API 行为无回归。
- `monitorListPresentation.test.ts` 新增完整状态与裁剪状态的派生等价测试，覆盖
  通道/位置、动量、量价资金、ATR/确认、信号和日内趋势线；聚焦前端 35 项通过。
- `DowMonitor.test.tsx` 证明 overview 尚未返回时，symbols 已驱动当前页 WebSocket
  订阅，实时价格 `123.45` 可见，稳定区域明确显示“指标加载中”。
- 完整前端 Vitest：47 个文件、214 项通过、2 项跳过；生产 TypeScript/Vite 构建通过。
- `scripts/check_spec_compliance.py` 通过；新规格契约、追踪、验收和复核文件均已登记。

## 本地执行记录

```text
PYTHONPATH=backend uv run --project backend python -m pytest \
  tests/backend/test_dow_monitor_fast_bootstrap.py -q
4 passed

PYTHONPATH=backend uv run --project backend python -m pytest \
  backend/tests/test_dow_monitor_api.py -q
40 passed

pnpm exec vitest run --reporter=dot
47 files passed; 214 tests passed; 2 skipped

pnpm build
TypeScript and Vite production build passed

python scripts/check_spec_compliance.py
Specification compliance passed
```

## 10.28 正式发布证据

- 最终提交 `eccbd6a92cfda9b8727e9636cd9a652e276b5338`；正式镜像
  `tickflow-stock-panel-app:dow-fast-bootstrap-eccbd6a-20260802-095510`。
- 真实数据只读候选基准：美股 7 只 `list-overview` 为 462.2 ms / 751,529 bytes；
  全市场 13 只为 660.0 ms / 1,469,241 bytes；美股 100 条通知摘要为
  36.8 ms / 35,938 bytes。
- 已登录 Chrome 正式页面：`symbols` 为 257 ms / 1,910 bytes，
  `notification-summaries` 为 338 ms / 36,066 bytes，`list-overview` 为
  1,068 ms / 756,836 bytes。旧正式 overview 基线为约 56.2 s / 6.8 MB。
- 浏览器网络序列为 symbols 完成 `327`、WebSocket 创建 `328`、订阅帧发送 `335`、
  list-overview 响应 `359`，证明订阅不等待稳定摘要；首屏没有完整单周期详情请求。
- 独立 WebSocket 冒烟得到 `hello -> NBIS.US snapshot(quote/depth/candlestick) ->
  unsubscribed`，完整 snapshot 为 820.0 ms。
- `/health` 返回精确 build ID；3018 容器 `302cb3682332...`、`RestartCount=0`。
  AI Worker 仍为 `6199d2015c4f...`，19912 PID 仍为 `3511290`，实时采集 PID
  仍为 `2891152`。3018 日志和浏览器控制台均无错误。
