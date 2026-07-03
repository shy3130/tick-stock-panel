# B3：港股日线落 parquet + Trade Journal 追涨覆盖实现计划

> **面向 AI 代理的工作者：** 只补 Trade Journal 追涨诊断的港股盲区，不扩成完整港股行情系统。

**目标：** 当前 `pricepos.py` 明确跳过 `.HK`，导致银河样本大量港股买入零覆盖。把 P6 已能读取的港股日线落到本地 parquet，并让 `build_price_lookup()` 扫描港股 enriched。

**现状证据：**
- `pricepos.py` 当前过滤：`targets = ... and not f.symbol.endswith(".HK")`
- 当前扫描目录：`kline_daily_enriched/kline_etf_enriched/kline_daily/kline_etf_daily`
- `EngineDataDiskClient` 已支持 `asset_type="hk"` 路径归一：`02577.HK -> hk02577`
- Trade Journal warning 以 uncovered symbols 计数，零覆盖才算 uncovered。

**范围：** 港股日 K 落盘 + pricepos 覆盖。港股基准、港股实时、港股盘口不做。

## 文件

| 文件 | 动作 |
|---|---|
| `backend/app/storage/repository.py` | 增加 `append_hk_daily` / `append_hk_enriched` / 读路径工具 |
| `backend/app/services/trade_journal/pricepos.py` | 纳入 `.HK` target，扫描 `kline_hk_enriched/kline_hk_daily` |
| `backend/scripts/backfill_hk_daily.py` | 创建，只读 TDX/fquant_local，写 parquet |
| `backend/tests/services/trade_journal/test_pricepos.py` | 补港股覆盖测试 |
| `backend/tests/data_providers/test_engine_data_disk.py` | 补港股路径测试 |

## 任务 1：先修 pricepos 的 HK 过滤测试

- [ ] 写失败测试：构造 `Fill(symbol="02577.HK", side="buy", date="2026-01-30")`，临时目录下写 `kline_hk_enriched/date=2026-01-30/part.parquet` 和前 20 日数据。
- [ ] 预期：`build_price_lookup()` 返回 key `("02577.HK", "2026-01-30")`，`uncovered` 不含 `02577.HK`。
- [ ] 当前会失败，因为 `.HK` 被过滤且 `_daily_globs()` 不扫 hk 目录。

## 任务 2：pricepos 最小改动

- [ ] 删除 `.HK` target 过滤。
- [ ] `_daily_globs()` 追加顺序：
  1. `kline_daily_enriched`
  2. `kline_etf_enriched`
  3. `kline_hk_enriched`
  4. raw fallback：`kline_daily/kline_etf_daily/kline_hk_daily`
- [ ] 保留 `.unique(["symbol","date"], keep="first")`，避免 enriched/raw 重复污染 rolling。
- [ ] uncovered 定义保持“零覆盖标的”，部分覆盖不算 uncovered。

## 任务 3：repository 港股路径

- [ ] 新目录：
  - `data/kline_hk_daily/date=YYYY-MM-DD/*.parquet`
  - `data/kline_hk_enriched/date=YYYY-MM-DD/*.parquet`
- [ ] schema：
  - raw：`symbol/date/open/high/low/close/volume/amount/source`
  - enriched：至少 `symbol/date/close/change_pct/source`
- [ ] 不复用 `kline_daily`，避免 A 股涨跌停/换手率逻辑误套港股。
- [ ] 若 `amount=0`，原样写入，不伪造。

## 任务 4：回填脚本

`backend/scripts/backfill_hk_daily.py`

- [ ] 参数：
  - `--symbols 02577.HK,06088.HK`
  - `--symbols-from journal`：从最新 `data/user_data/trade_journal/ledger.json` 的 trips/open_positions 或重新上传后续再扩。
  - `--start 2015-01-01`
  - `--end YYYY-MM-DD`
- [ ] 默认不做 `--symbols-from all`；全市场港股 universe 未确认，YAGNI。
- [ ] provider：`FQuantProvider(engine_mode="disk").get_daily(... asset_type="hk")`
- [ ] enriched：只计算 `change_pct` 和 rolling 需要的 `close`；不跑 A 股 `compute_enriched()`。
- [ ] 写入前按 `symbol/date` 去重。

## 任务 5：样本验收

- [ ] 用 `~/Downloads/银河.xlsx` 跑 parser + build_price_lookup。
- [ ] 回填 `02577.HK/06088.HK` 后，这些 symbol 不再出现在 uncovered。
- [ ] 若没有对应 TDX 港股历史，warning 仍准确保留。

## 任务 6：验证

```bash
cd backend
uv run --extra dev pytest tests/services/trade_journal/test_pricepos.py tests/data_providers/test_engine_data_disk.py -q
uv run python scripts/backfill_hk_daily.py --symbols 02577.HK,06088.HK --start 2024-01-01
```

## 非目标

- 不做恒生指数基准。
- 不做港股实时/盘口。
- 不把上传原始流水长期落盘。
