# 验证记录

## 生产 generation

- root：`/Volumes/WD1/duckdb/snapshots/engine-a-ordered-trans`
- generation：`20260827T134357Z-f751ea5b08e3b4da`
- manifest SHA-256：`cf5e2dc98fae3bd249f4a1c402b09ce6102cd2fe64a7ab490d03fbcc424ab475`
- bounded symbols：`600519.SH / 000001.SZ / 300750.SZ`
- 扫描窗口：`2026-07-01..2026-08-26`
- complete days：30；只发布三标的都满足 48/48 canonical 5m trade windows 的日期。
- OOS split：在查看研究结果前，按 complete days 后 1/3 冻结 `oos_start=2026-08-04`；真实 smoke 使用 `end=2026-08-20`（最后一个 complete day）。

首次发布暴露正常收盘集合竞价没有 240 根正成交 1m bar；已按 `production-amendment.md` 修正为 sparse true-trade 1m + exact 48×5m，禁止使用零量指示价或未来成交回填。另发现少数 raw 文件有相邻分钟逆序，publisher 只按 minute 分桶、桶内保留物理 source sequence。

## 哈希与真实数据探针

对 `2026-07-01/2026-08-19` 的沪深样本重算：source CSV SHA-256 与 manifest 一致，derived Parquet SHA-256 与 manifest 一致。样本 artifact 为 239 个 sparse 1m bars、缺 `14:59` close，但仍有 48/48 五分钟窗口；没有伪造空分钟。

## 服务 smoke

固定请求：

```json
{
  "start": "2026-07-01",
  "end": "2026-08-20",
  "oos_start": "2026-08-04",
  "symbols": ["600519.SH", "000001.SZ", "300750.SZ"]
}
```

真实 FQuantProvider factory + published reader：

- `status=ok`
- `days=30`，sparse 1m `bars_total=21,502`
- horizon 1：raw 1,437 / cross-boundary 3 / overlap 1,194 / effective 240 / effective OOS common 88
- horizon 2：raw 1,434 / cross-boundary 6 / overlap 1,269 / effective 159 / effective OOS common 58
- verdict：`rejected`
- 原因：`factor_post_cost_not_positive`、`factor_wilson_lower_not_above_baselines`
- 最强 baseline point hit rate：`0.4772727273`

该 rejected 是真实研究结论，不会写入短线池、Agent 或默认策略。

## HTTP API smoke

用无 lifespan 的独立 FastAPI 壳挂载真实 `research.router`，避免触碰用户 `data/`：

- `POST /api/research/factors/mtf-direction/evaluate` → HTTP 200
- `status=ok`
- generation/hash、common OOS 与 service smoke 完全一致
- verdict 同为 `rejected`

## 自动验证

```text
uv run --project <main-backend> python -m pytest \
  tests/data_providers/test_ordered_trans_publisher.py \
  tests/data_providers/test_ordered_trans_reader.py \
  tests/data_providers/test_ordered_trans_capability.py \
  tests/services/test_mtf_direction_15m5m.py \
  tests/api/test_mtf_direction_api.py \
  tests/api/test_research_factor_evaluate_api.py -q
# 25 passed

uv run --project <main-backend> python -m pytest tests -q
# 3428 passed, 3 skipped, 8 warnings in 781.94s

uv run --project <main-backend> ruff check --select F,E9 <Issue10 changed Python files>
# All checks passed
```

独立 coding review 初审发现并修复：provider/service session 类型与 09:30 语义错配、bar 私有类型错配、unconditional baseline 使用 purge 前 IS、manifest identity 未验证、current symlink、关闭链异常。复审确认无 blocker/major。真实尾盘口径修订也经独立复审批准。