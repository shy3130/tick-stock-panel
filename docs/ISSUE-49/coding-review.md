# Issue #49 编码复核

实现位于 `backend/app/services/n_shape_pullback_depth.py`，并通过研究 API 接入。复核中发现首版错误地把 Issue #8 的低位首板事件当成 TODO 所要求的 zigzag N0，已删除该口径并改为三状态 causal swing 检测器。

当前实现只读 pinned composite reader；复用 bar 完整性、manifest 与 source provenance 校验，不读取外部源、不写 `data/`。事件显式记录 origin/high/pullback 三个已确认锚点；未确认尾段不出事件，结构破坏和 horizon 缺失分别报告。日期通过 FastAPI JSON 编码验证。

独立 review 未报告重写后实现的 P0/P1/P2 问题。
