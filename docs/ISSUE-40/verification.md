# Issue #40 验证记录

## 定向回归

```text
uv run pytest backend/tests/services/test_universe_presence_history.py backend/tests/services/test_universe_scd.py -q
26 passed in 6.57s
```

覆盖 exact-day presence、空市场日、coverage/非市场日、重复 key/非法代码、production 风格非 canonical source manifest、manifest/path/hash/count/source identity 篡改、interval gap/reused hash、严格整数 schema/count、symlink、CAS 冲突、失败不切 current、幂等、接口隔离和 repository seam。

## 静态检查

```text
新 service 与新测试：Ruff 全规则通过
repository.py：Ruff F/E9 通过
```

`repository.py` 的既有 9 条 import-order baseline 不在本次改动行；未扩大修改范围进行无关格式化。

## 后端全量回归

```text
uv run pytest -q
3532 passed, 3 skipped, 8 warnings in 793.38s
```

warning 均来自既有 Polars sortedness/deprecation/performance 路径，本次新增测试无 warning。

## 真实 pinned source 只读 smoke

先发布到临时 root 并用正式 reader 重读，未修改 `data/`：

```json
{
  "status": "published",
  "source_generation": "20260829T000704",
  "coverage_start": "2022-03-04",
  "coverage_end": "2026-08-28",
  "market_day_count": 1090,
  "interval_count": 653,
  "first_symbol_count": 4725,
  "last_symbol_count": 5905
}
```

首次 smoke 发现生产 `trade_date` 同日包含 A股、港股和互联互通多条记录；实现与测试据此固定为 `mkt='A股' AND isopen=3`，避免跨市场日历混合。

## Production artifact

已原子发布并由 `PublishedPresenceUniverseReader` 重读：

```json
{
  "root": "/Volumes/WD1/duckdb/snapshots/tickflow-universe-presence",
  "generation": "20260829T020332Z-6e648967c37e6739",
  "source_generation": "20260829T000704",
  "source_manifest_sha256": "a2a9d2b8208af33f4bcb66bcbe46a02ee836659c337deab4d0fd550ffead22a8",
  "coverage_start": "2022-03-04",
  "coverage_end": "2026-08-28",
  "market_day_count": 1090,
  "interval_count": 653,
  "status": "published"
}
```

manifest 已核验：`schema_version=2`、`rule_version=presence_v1`、`retrospective=true`、`status_filter=daily_market_row_present_exact_day`，calendar contract 为 `fstore_trade_date:mkt=A股,isopen=3,tdate`。

## PR #41 review follow-up

presence collection 已改为 registry → active `FQuantProvider` 的显式窄扩展路由；pinned DuckDB 查询在 provider helper 内以 read-only 连接执行，provider 和两条连接均在成功/异常路径关闭。非 FQuant provider、缺少显式方法或 query failure 均 fail-closed。reader 已拒绝带时间尾缀的日期 artifact，并新增相应回归覆盖。

主会话复验：

```text
presence + eligible-v1 focused: 28 passed in 7.70s
新 provider helper / 测试 Ruff 全规则：All checks passed!
FQuantProvider / service Ruff F/E9：All checks passed!
backend full: 3548 passed, 3 skipped, 8 warnings in 208.27s
```

独立二次复核未发现 blocker、P1 或 P2。本次只改变查询归属、连接生命周期和 reader 输入校验，不改变 `presence_v1` schema v2、manifest identity 或 production generation；已存在且合法的 generation 无需重发 snapshot。

## 独立 Coding Review

首轮 reviewer 发现 count 类型混淆 P2；PR #41 review 又发现 service 绕 provider 的 P1 与日期尾缀 P2。三项均已修复、补回归并通过独立二次复核，无 blocker、P1 或残余 P2。详见 [coding-review.md](coding-review.md)。
