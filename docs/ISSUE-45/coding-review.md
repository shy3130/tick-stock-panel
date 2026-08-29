# Issue #45 编码复核

实现位于 `backend/app/services/daily_event_research/`，并通过 `POST /api/research/factors/dugu-trend/evaluate` 接入。detector 无 I/O；evaluation 只绑定 pinned canonical 与由 canonical manifest 指定的 market facts，不依赖无关的 universe presence。

独立 review 发现 API 曾把 market-facts pin 失败误报为 canonical reader 失败。已拆分 reader 构造和 evaluator 内部异常归因：canonical 缺失返回 `unavailable_canonical_reader`，facts 缺失/不完整返回 `unavailable_market_facts`，并新增 API 回归测试。

已核对严格 response envelope、Pydantic JSON 别名、T+1 entry、双边成本、limit reachability、同父事件基线、OOS 门槛与 `promoted=false`。
