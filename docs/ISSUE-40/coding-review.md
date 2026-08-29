# Issue #40 Coding Review

## 范围

独立 reviewer 复核以下实现与契约：

- `backend/app/services/universe_presence_history.py`
- `backend/app/storage/repository.py` 的 `pit_presence_universe`
- `backend/tests/services/test_universe_presence_history.py`
- `docs/ISSUE-40/final-design.md`

复核重点为同 generation source pin、exact-day presence 语义、A 股市场日历、schema v2 完整性、原子发布、fail-closed reader，以及与 `eligible_v1` 的接口隔离。

## 发现与处置

### P2：市场日 count 接受 JSON `true` / `1.0`

原实现直接用 `len(days) != count` 比较。在仅有一个市场日时，Python 的 `True == 1`、`1.0 == 1` 可能让非整数元数据通过。

已修复：

- `market_days.count` 与 `coverage.market_day_count` 先拒绝 `bool` 和非 `int`，再比较值；
- 同类加固 `schema_version`，拒绝 `bool`、浮点数和非整数；
- 新增单市场日 `true` / `1.0` 与 `schema_version=2.0` 的回归测试。

## 最终结论

Reviewer 静态复核确认该 P2 已关闭，未发现 blocker、P1 或残余 P2；实现可提交。验证命令与生产源只读 smoke 证据见 [verification.md](verification.md)。
