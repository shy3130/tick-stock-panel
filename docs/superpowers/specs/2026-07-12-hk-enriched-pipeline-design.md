# 港股 enriched 管道 — 设计

日期：2026-07-12
状态：已确认，待实现

## 目标

补齐港股的本地 enriched 面板（同步 → 落盘 → 指标/信号），为后续港股筛选打底。

**不在本次范围**：港股回测接入（见「复权缺口」）、港股筛选 UI。本次只交付面板。

## 摸底结论：缺口只在面板层

| 层 | 现状 |
|---|---|
| provider 取数 | **已就绪**。`AssetType` 契约含 `"hk"`；`get_instruments('hk')` 返回 2981 只、含 `total_shares`/`float_shares`；`get_daily(..., asset_type='hk')` 返回完整 OHLCV + amount |
| 指标/信号计算 | **已就绪**。`compute_all()` 里 `asset_type == "stock"` 才走 `compute_limit_signals`，否则只 `_attach_turnover_rate` —— 传 `asset_type="hk"` 即自动得到「全套指标 + 信号 + 换手率、无涨跌停」 |
| 维表落盘 | **缺**：无 `instruments_hk` |
| enriched 落盘 | **缺**：`append_hk_enriched` 只存 `symbol/date/close/change_pct/source` 五列，应为全量 `ENRICHED_STORAGE_COLS` |
| 内存缓存 | **缺**：无 `_refresh_hk_enriched`；`get_enriched_latest_asset("hk")` 返回空 |
| 管道接线 | **缺**：`append_hk_daily` / `append_hk_enriched` 无任何调用方（死代码） |

ETF 是现成模板：它已经是「一个没有涨跌停语义的资产类」，港股照抄其形状即可。

## 核心口径（必须写进代码注释）

### 不复权 —— 这不是选择，是数据现实

本地**没有任何港股除权数据源**，实测证据：

- fstore `chuquan_chuxi` 无 `asset_type` 列，按代码长度分布：**只有 6 位代码（A 股）**，5 位（港股）0 行；腾讯 `00700` 除权记录 **0 条**
- tdx-hk 库只有 `market_day_kline` + `stock_data_manifest`，**无 xdxr 表**
- tdx-hk `market_day_kline.adjustment_count` **全表 6,357,180 行均为 0**，不携带信息

因此 `compute_enriched(factors=None)`，`raw_close = close`。`markets.py` 里 HK 的
`adjustment="none"` 反映的正是这个现实，不是随手设的。

**未决问题**：tdx-hk 的价格序列本身是否已被上游复权过，**用本地数据无法验证**。这是
个真实的未知数，且它决定回测的正确性。

### 回测保持门控

除权跳空会被读成真实亏损。港股股息普遍 5-8%，一次分红就是一个 5-8% 的假跌幅，
回测收益率会系统性出错且难以察觉。**港股回测必须等复权问题解决后再开放**，本次不碰。

筛选/指标/复盘可用，但除权日附近会有假信号（MA 穿越、n 日新低），文档标注。

### 不产涨跌停信号

港股无涨跌停制度（`markets.py`: `has_price_limit=False`）。`compute_all` 的
`asset_type != "stock"` 分支已保证不产 `signal_limit_up` / `signal_broken_limit_up` /
`signal_limit_down` / `consecutive_limit_ups` 等列。

### 回补起点

**2024-10-09**，与 A 股 enriched 面板对齐（现为 426 个交易日、82MB）。港股 2981 只
约为 A 股六成，估算 ~50MB。两市场起点一致，跨市场口径可比。

## 组件

全部镜像 ETF 的现成实现：

**`app/storage/repository.py`**
- `save_hk_instruments()` / `get_hk_instruments()` —— 镜像 `save_etf_instruments` / `get_etf_instruments`，落 `instruments_hk/` 并挂 DuckDB 视图
- `append_hk_enriched()` —— 存储列由 5 列拓宽为 `ENRICHED_STORAGE_COLS`（按 `in df.columns` 过滤，港股天然没有 `consecutive_limit_ups` 等列，自动跳过）
- `_refresh_hk_enriched()` —— 镜像 `_refresh_etf_enriched`：读最新日分区 + 近 300 日历史，`compute_indicators` + `compute_signals`，缓存到内存
- `get_enriched_latest_asset("hk")` —— 增加 `hk` 分支

**`app/services/index_sync.py`**
- `sync_hk_instruments(repo)` —— 镜像 `sync_etf_instruments`

**`app/services/preferences.py`**
- `get_pipeline_pull_hk()` —— 镜像 `get_pipeline_pull_etf`，默认关闭（港股是可选能力）

**`app/jobs/daily_pipeline.py`**
- `pull_hk` 开关下：同步港股维表 → `sync_daily_batch(asset_type="hk")` → `append_hk_daily`
  → `compute_enriched(raw, factors=None, instruments=hk_instruments, asset_type="hk")`
  → `append_hk_enriched`
- **不做** adj_factor 步骤（无数据源）

## 数据流

```
fstore base_infos(asset_type=3) ──► get_instruments('hk') ──► instruments_hk/
                                                                    │
tdx-hk market_day_kline(hkday) ──► get_daily(asset_type='hk') ──► kline_hk_daily/
                                                                    │
                                          compute_enriched(factors=None,
                                                           asset_type='hk')
                                                                    │
                                                          kline_hk_enriched/
                                                                    │
                                                    _refresh_hk_enriched() ──► 内存缓存
```

## 错误降级

- provider 不可达 / 港股维表为空 → 跳过港股阶段，不影响 A 股管道（沿用 ETF 的降级姿势）
- `pull_hk` 默认关闭 —— 未启用时管道行为与现状完全一致，零回归风险

## 测试

单测：
- `append_hk_enriched` 落全量存储列（而非 5 列）
- `compute_all(asset_type="hk")` **不产** `signal_limit_up` / `consecutive_limit_ups`，
  但**产出** `turnover_rate` 与 `ma20` —— 锁住「无涨跌停但有换手率」这个港股特有形状
- `compute_enriched(factors=None)` 时 `raw_close == close`（不复权）

真实数据验证：
- 跑一次港股管道，核对 enriched 分区的列集、行数（~2900/日）、指标非空率
- 抽查腾讯 `00700.HK` 的 ma20 与手算一致
