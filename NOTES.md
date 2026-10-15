# 市场宽度(R3) 风控提醒 — 实现说明 NOTES

> 把 `research/regime_sizing/` 里**唯一可落地**的结论（R3 20 日涨跌家数比）做成**提醒**，
> 不是自动仓位。低档时在看板顶部提示「市场宽度转弱，建议降低总仓位」。
> 本文说明：如何重算、阈值为何冻结、为什么不做自动仓位、以及实现边界。

---

## 1. 这是什么 / 不是什么

| 是 | 不是 |
|---|---|
| 看板顶部一条**非阻断**的只读横幅 | ❌ 不是买卖信号 |
| 后端日终算出的 `v_breadth` + 档位 + 建议仓位(纯展示) | ❌ 不触碰下单 / 配平 / DSA 仓位逻辑 |
| 低档提醒、中档弱提示、高档不打扰 | ❌ 不自动调仓、不改任何持仓权重 |

**研究依据**：`research/regime_sizing/REPORT_REGIME_SIZING.md`。
R3_breadth 在研究池 290 只等权、沪深300 上通过四条判定（后段 MDD 0.553 / 0.427，卡玛 3.806 / 0.842 vs 满仓 0.442 / −0.684）。
**但后段超额 t 最大仅 1.65，远低于 Bonferroni 门槛 2.638 → 统计上不显著**，所以只做提醒。

---

## 2. 信号定义（与研究报告逐字一致，不得改动）

```
v_breadth = 近 20 个交易日, 每日 (上涨家数 / 下跌家数) 的均值
股票池    = kline_daily_enriched 全 A(与 regime 时序同源)
毛刺剔除  = 个股日收益 |r| > 25% 不计入涨/跌家数
分母下限  = 下跌家数取下限 1.0(避免除零)
```

- **时点**：t 日收盘后可算 → **t+1 生效**；横幅按 t 日收盘口径解读。严格无未来数据（窗口只含当日及之前）。
- **档位阈值（冻结）**：研究**前段**（≤2026-04-01）分位数 33.33% / 66.67%

  | 档位 | 条件 | 建议总仓位 |
  |---|---|---|
  | low | `v_breadth ≤ 1.53761` | 0.3 |
  | mid | `1.53761 < v_breadth ≤ 1.67424` | 0.6 |
  | high | `v_breadth > 1.67424` | 1.0 |

  对应常量：`backend/app/services/regime_builder.py` 的 `BREADTH_THRESHOLDS = (1.53761, 1.67424)`、`BREADTH_POS`。

---

## 3. 如何重算（复现 / 排障）

### 3.1 数据依赖
- `kline_daily_enriched`（全 A 日线，含 `close`）——regime 时序与宽度序列都从这里来。
- `regime_history/part.parquet` —— 后端的 `v_breadth` 直接读这张表的 `up_count/down_count`，**不重扫全 A**。
  - 若该表为空（首次部署 / 未回填），需先跑一次 regime 重算：`POST /api/regime/recompute`（前端「数据」页也有入口）。

### 3.2 后端
- 入口：`GET /api/regime/breadth?days=60` → `{latest, series, note, threshold_source, thresholds}`
- 轻量：`GET /api/regime/breadth/latest` → 只回最新一日（横幅用）。
- 纯函数：`regime_builder.compute_breadth_series()` / `compute_breadth_latest()` / `classify_breadth()`。
- 重算链路：`_aggregate_daily` 已按「剔除毛刺后」口径产出 `up_count/down_count`（本 PR 修正）；
  `compute_breadth_series` 再对其做 20 日滚动均值 + 定档。

### 3.3 一致性核对（任务书验收项）
```bash
cd backend && .venv/bin/python ../research/regime_sizing/verify_backend_breadth.py
```
抽样 3 个交易日（`2025-09-15` mid / `2025-08-15` high / `2026-10-12` low，覆盖三档），
在同一份 enriched 数据上用两套**彼此独立**的实现各算一遍 `v_breadth` 再比对：
「研究口径」（复刻 `regime_sizing.build_market_panel` 的流式 prev_close）vs「后端口径」（`compute_breadth_series`）。
脚本自包含，不依赖 research/ 下其它模块；数据目录可用环境变量 `TSP_DATA` 覆盖。
实测研究窗口内最大浮点偏差 **5.6e-5**，档位完全一致。

---

## 4. 阈值为何冻结

- 阈值取自研究**前段**（≤2026-04-01）的分位数：研究的第一条纪律就是「阈值只用前段拟合，后段只验证」。
- **上线后不得用全样本重估**：一旦用后段（乃至全量）数据重算阈值，就变成样本内最优化，
  后段那点「卡玛改善」不再是样本外证据，提醒也就失去意义。
- 常量在 `regime_builder.py` 里写死，并由 `tests/test_regime_breadth.py::test_thresholds_frozen` 断言守护 —— 任何人改动都会触发测试失败。

---

## 5. 为什么不做自动仓位

1. **统计不显著**：R3 后段超额 t 最大 1.65 < Bonferroni 门槛 2.638。点估计占优 ≠ 显著。
2. **只在退潮期有价值**：前段单边上涨时任何减仓都只是少赚，它是**风控**不是**收益增强**。
3. **风格不稳**：**中证1000 上 R3 前后段方向不一致**（前段卡玛 −0.83 差于满仓 −0.09，后段才反超）。
   → 所以文案限定为**「大盘 / 中盘宽度」**，不写「全市场万能」。
4. 任务书硬边界：只做提醒，不上 R1/R2/R4/R5/R6，不自动调仓。

---

## 6. 涉及文件

| 文件 | 改动 |
|---|---|
| `backend/app/services/regime_builder.py` | 新增 `BREADTH_*` 常量与 `classify_breadth` / `daily_breadth_ratio` / `compute_breadth_series` / `compute_breadth_latest`；`_aggregate_daily` 的涨跌家数改为**剔除毛刺**口径 |
| `backend/app/api/regime.py` | 新增只读端点 `GET /api/regime/breadth`、`GET /api/regime/breadth/latest` |
| `backend/tests/test_regime_breadth.py` | 单测：阈值边界 / 毛刺剔除 / 无未来数据 / latest 辅助 / 阈值冻结 |
| `frontend/src/lib/api.ts` | 新增 `regimeBreadth` / `regimeBreadthLatest` 与类型 |
| `frontend/src/lib/queryKeys.ts` | 新增 `regimeBreadth` 查询键 |
| `frontend/src/components/data/BreadthBanner.tsx` | 新增横幅组件（低档醒目 / 中档弱提示 / 高档不渲染） |
| `frontend/src/pages/Dashboard.tsx` | 顶部插入 `<BreadthBanner/>` |
| `frontend/src/components/data/BreadthBanner.test.tsx` | 单测：低/中/高/无数据 四种渲染 |
| `research/regime_sizing/verify_backend_breadth.py` | 验收一致性脚本 |

---

## 7. 验收对照

| 任务书验收项 | 结果 |
|---|---|
| 研究窗口抽 3 个交易日，API 档位与研究结果一致（允许浮点误差） | ✅ `verify_backend_breadth.py`：low/mid/high 三档一致，偏差 ≤5.6e-5 |
| 低档时 UI 可见提醒 | ✅ `BreadthBanner` low 分支 + 单测 |
| 高档无骚扰 | ✅ high 分支 `return null` + 单测 |
| 不触碰下单 / 配平 / DSA 仓位逻辑 | ✅ 纯只读展示，无写路径 |
| 单测覆盖：阈值边界 / 毛刺剔除 / 无未来数据 | ✅ `tests/test_regime_breadth.py`（11 例） |
