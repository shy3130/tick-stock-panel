# TickFlow Pro 限频安全预算（进程内 + 跨产品错峰）

## 决策（经 ChatGPT/Codex 评析修订）
- `backend/app/tickflow/rate_limits.py` 提供**单 Python 进程内**的 rpm 槽位限速与 `SAFETY_RPM_FACTOR=0.8`。
- **不要**把「各进程各扣 80%」当成账户级共享限频：Gold Shadow 容器与 A 股面板进程状态独立，理论聚合可达 160%。
- Stage A 期间跨产品靠**错峰**，不在 Gold 上部署分布式限频重构。

## 预算表示例（Pro，单进程 80%）
| capability | 套餐 rpm | 进程内 80% |
|---|---:|---:|
| quote.batch | 120 | 96 |
| quote.pool | 60 | 48 |
| kline.daily.batch | 60 | 48 |
| kline.minute.batch | 30 | 24 |
| depth5.batch | 30 | 24 |
| adj_factor | 60 | 48 |

## 错峰（Stage A）
- 盘中：优先 Gold Shadow 观察与 legacy `gold-monitor`。
- A 股 Pro 探测/大批量同步：建议 **16:00 后**。
- 禁止全市场一年分钟一次性回填。

## 实现要点
- `resolve_limit(..., apply_safety=True)` 默认对 rpm 做 `floor(rpm * 0.8)`。
- `sleep_between_batches`：`index=0` 只占槽不 sleep（首批突发；**并发多个 index=0 仍可能超 rpm**）；后续 batch 按槽位等待。
- Phase 1 / Stage A：**不要并发**启动多个 probe 或大批量 sync。
- 诊断可传 `apply_safety=False`；跨容器账户预算需另设（Stage A 不做）。
- 任一 429 / fallback 应记入证据链，禁止静默混源。
