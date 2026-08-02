# 趋势监控快速首屏语义验收

状态：本地语义验收通过；10.28 候选与正式性能证据待发布阶段补充。

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

## 发布阶段待补

- 10.28 候选端口的 symbols、list-overview、notification-summaries TTFB 与响应字节数。
- 已登录浏览器请求顺序和 WebSocket subscribe/首个 quote 时间。
- 详情点击后才请求完整周期状态的网络证据。
- 3018 正式切换后的健康、静态块、WebSocket、容器重启次数和错误日志。
