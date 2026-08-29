# Issue #49 编码复核

实现位于 `backend/app/services/n_shape_pullback_depth.py`，并通过研究 API 接入。复核中发现首版错误地把 Issue #8 的低位首板事件当成 TODO 所要求的 zigzag N0，已删除该口径并改为三状态 causal swing 检测器。

当前实现只读 pinned composite reader；复用 bar 完整性、manifest 与 source provenance 校验，不读取外部源、不写 `data/`。事件显式记录 origin/high/pullback 三个已确认锚点；未确认尾段不出事件，结构破坏和 horizon 缺失分别报告。日期通过 FastAPI JSON 编码验证。

PR #51 Codex review 发现事件日期切分会让 validation 边界的 5/10/20 日收益借用 test 价格。已改为按请求市场日历切 60/20/20，每个 forward outcome 记录 `available_date`，统计、增量和 placebo 统一排除跨 split 结果，并增加边界删失回归。
