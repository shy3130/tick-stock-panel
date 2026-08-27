# 最终设计

新增独立 `weak_to_strong_v1` fail-closed 研究契约。run-level manifest 固定日线、分钟、集合竞价、逐笔/盘口和 PIT 制度/ST/股本的 generation/校验和/覆盖；PIT 同时校验 effective_at 与 available_at 不晚于信号。触板/炸板/回封按变体验证逐笔排序，封板额外要求盘口证据；缺失只能 bar_touched 或 unavailable。不接入 short_pool/Agent/交易事实。

## 生产化状态更新（2026-08-27）

production composite reader/API seam 已实现：minimum/full capabilities 分级，canonical/markets/#10 sparse minute/signal-year pinned callauction 组件均固定 generation、manifest hash、coverage 并生成 composite SHA-256。markets PIT 使用 generation `created_at` 作为唯一 `available_at`，effective/available 均须通过 09:25 Asia/Shanghai 门禁，事件阈值优先 exact `ztj`。未声明的 sortable tick、历史盘口、float 返回空/None，触板/封板/一字板相关分支只产生明确删失，禁止伪造 sealed 分类。

API 每请求拥有 production reader，成功和异常均 finally 精确级联关闭；registry reader 保持 caller-owned。历史 signal 若 publication 晚于 effective cutoff，预期 `pit_incomplete`，但仍可构造 composite manifest。定向 34 项合同/API 测试与 `ruff --select F,E9` 通过；真实 reader smoke 固定了 canonical、markets、ordered-trans、callauction 四组件，并确认当前历史/同日 PIT 均不会越过 09:25 publication boundary。独立二次 Review 无 blocker/major。
