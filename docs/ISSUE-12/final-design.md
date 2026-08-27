# 最终设计

新增独立 `weak_to_strong_v1` fail-closed 研究契约。run-level manifest 固定日线、分钟、集合竞价、逐笔/盘口和 PIT 制度/ST/股本的 generation/校验和/覆盖；PIT 同时校验 effective_at 与 available_at 不晚于信号。触板/炸板/回封按变体验证逐笔排序，封板额外要求盘口证据；缺失只能 bar_touched 或 unavailable。不接入 short_pool/Agent/交易事实。

## 生产化状态更新（2026-08-27）

事件主路径和能力分级已实现：完整证据可形成 sealed 分类，缺历史盘口只能降为 `bar_touched`，PIT 制度/ST/股本不完整则 unavailable/censored。由于本地历史 PIT 状态与盘口仍不可证明，生产 registry 不注册近似 reader，保持本设计的 fail-closed 边界。
