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

## 独立 Coding Review

Reviewer 的唯一实质 finding 是单市场日 count 对 JSON `true` / `1.0` 的类型混淆；修复并补回归后，reviewer 静态确认无 blocker、P1 或残余 P2，可提交。详见 [coding-review.md](coding-review.md)。
