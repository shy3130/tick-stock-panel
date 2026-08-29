# 冻结方案最终 review

第三次复核聚焦二次 review 后的 calendar/gap/hash 修订，发现 1 个 blocker：仅返回 symbols list 无法区分 `not_observed` 与 eligibility 的 `NOT_IN_POOL`。

最终调整：reader interface 冻结为 `PresenceDaySnapshot` 与 `PresenceStatus(PRESENT | NOT_OBSERVED)`；不暴露 `NOT_IN_POOL`。Issue #38 只有 `PRESENT` 可进入 denominator，任一 `NOT_OBSERVED`/coverage fault 整单 unavailable。

复核结论：唯一 finding 已关闭，未见残余 blocker/major，批准进入实现。