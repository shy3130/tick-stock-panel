# 本地磁盘数据源模式 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 为 panel 新增 `fquant_local` 数据源模式——engine-data 数据改直读 TDX 磁盘 CSV，新增 sina/tencent 实时源，本地模式下取消"抓取远程数据落 `data/kline_daily`"环节；同时修复已上线的 raw 前复权污染 bug（HTTP/磁盘两模式共享修复）。

**架构：** 依据 `docs/superpowers/specs/2026-07-02-local-disk-datasource-design.md`（D-L1~D-L7，经三轮 review 修正）。raw 重建放 mapping 层（`normalize_daily` 之前，daily + adj_factor 双链）；`fquant_local` 以 registry 工厂注册（preferences 白名单/settings API/前端 union 同步），并先把 `daily/minute/realtime_data_provider` 真正接入 `registry.get_active_provider_name(capability)`；stock raw mirror 禁写收口在 repository 层；pipeline 新增"provider → enriched"输入分支；realtime 契约显式化后接 sina/tencent。

**技术栈：** Python 3.11 + Polars + FastAPI + httpx + pytest（后端）；TypeScript + React（前端两处小改）。

**执行约束（本仓红线）：**
- 每任务末尾的 commit 步骤**不自动视为授权**；只有用户明确要求提交时才执行 `git commit`；**禁止 push**；禁止 `git clean -fdx` / `reset --hard`。
- `TDX_DATA_DIR`（默认 `/Volumes/vol3/tdx`）对 panel **严格只读**；测试一律用 fixture，不依赖挂载盘（spike 脚本除外）。
- 不修改 `data_providers/normalizer.py` 的既有字段语义（新增 realtime 契约是**追加**）。
- 任务 0/1 是闸门：**任一不通过则停止执行并向用户报告，不进入任务 2+**。

---

## 文件结构

| 动作 | 路径 | 职责 |
|------|------|------|
| 创建 | `backend/app/data_providers/fquant/raw_reconstruct.py` | TDX 前复权逆运算 → raw 价格重建（纯函数） |
| 创建 | `backend/app/data_providers/fquant/engine_data_disk.py` | 磁盘 CSV 客户端（wide 优先、day 降级、xdxr 直读 + 保鲜探测），接口对齐 `EngineDataClient` |
| 创建 | `backend/app/data_providers/fquant/sina_tencent_client.py` | sina/tencent 批量实时 HTTP 客户端 |
| 创建 | `backend/app/services/data_mode.py` | 基于 daily capability resolver 的本地模式判定 + stock raw 写门控开关 |
| 创建 | `backend/scripts/spike_raw_reconstruct.py` | 任务 1 闸门脚本（对拍 fstore fq=0） |
| 创建 | `backend/scripts/refresh_polluted_daily.py` | 存量污染分区重刷脚本 |
| 修改 | `backend/app/data_providers/fquant/mapping.py` | `wide_rows_to_daily` 接 raw 重建产物 |
| 修改 | `backend/app/data_providers/fquant_provider.py` | engine 客户端可注入（http/disk）、`_build_daily_close_map` 用 raw、realtime 链插 sina/tencent |
| 修改 | `backend/app/data_providers/registry.py` | 工厂化注册 + `fquant_local` |
| 修改 | `backend/app/data_providers/normalizer.py` | 追加显式 realtime 契约 |
| 修改 | `backend/app/services/preferences.py` | 白名单 + `fquant_local` |
| 修改 | `backend/app/tickflow/repository.py` | stock raw 写方法加门控，index/ETF raw 保留现有缓存 |
| 修改 | `backend/app/indicators/pipeline.py` | 新增 `run_pipeline_local`（provider → enriched） |
| 修改 | `backend/app/jobs/daily_pipeline.py` | 本地模式走 `run_pipeline_local`，跳过抓取 |
| 修改 | `frontend/src/lib/api.ts` + `frontend/src/pages/settings/System.tsx` | provider union + 下拉加 `fquant_local` |
| 创建 | `backend/tests/data_providers/test_raw_reconstruct.py` 等 | 各任务对应测试（见任务内） |
| 修改 | `AGENTS.md` + `backend/docs/FQUANT_INTEGRATION_PROGRESS.md` | 红线注记 + 阶段 6 沉淀 |

---

## 阶段 0 — 闸门

### 任务 0：`day/` 覆盖缺口分类（硬闸门）

**文件：**
- 创建：`backend/scripts/spike_disk_day_coverage.py`

- [ ] **步骤 1：核对 868 只缺文件构成**

脚本输入：当前 `data/instruments/instruments.parquet`、`TDX_DATA_DIR/day`、fstore `base_infos`、fstore `day_klines` 最大日期。

输出必须按 symbol 分类：
- `tdx_day_exists`：TDX `day/{bucket}/{market}{code}.csv` 存在。
- `missing_retired_or_unlisted`：TDX 缺文件，但 fstore `base_infos` 可证明不是当前在市交易标的。
- `missing_has_fstore_tail`：TDX 缺文件，但 fstore `day_klines` 覆盖到 2025-10-31，可作为历史 fallback；同时标记 2025-11 之后不可补。
- `missing_has_fstore_after_2025_11`：TDX 缺文件，但 fstore `t_1_day_klines/day_klines` 覆盖到 2025-11-01 之后，可作为 fallback。
- `true_gap_active_after_2025_11`：在市/疑似在市，TDX 缺文件，且 fstore 无 2025-11 之后 day K。

- [ ] **步骤 2：判定闸门**

`true_gap_active_after_2025_11 == 0` 才能宣称 `fquant_local.daily` 全市场可用；否则后续实现必须二选一：
- 降级声明：`fquant_local.daily` 对这些 symbol 返回空并在设置页/数据状态页展示缺口清单。
- 补 backup：为这些 symbol 增加明确的非 TDX backup 源，并写清它不是“纯磁盘覆盖”。

该任务不通过时，停止执行并向用户报告缺口样本和数量。

授权后可选提交：

```bash
git add backend/scripts/spike_disk_day_coverage.py
# 用户明确授权后再执行: git commit -m "spike: TDX day 覆盖缺口分类闸门"
```

### 任务 1：Spike — raw 重建公式验证（不过则全计划停止）

**文件：**
- 创建：`backend/scripts/spike_raw_reconstruct.py`

**背景（工程师须知）：** 磁盘 `day/` CSV 是 TDX 减法前复权序列（列 `date,open,close,high,low,volume,amount,up,down,datetime,adjustment_count`；`adjustment_count`=该行被复权次数，0=原始价）。已证实 fquant-HTTP 模式同源同病（600519 在 2025-10-31 close=1378.03 vs fstore 原始 1430.01）。oracle：fstore PG `day_klines`（`ktype=101 AND fq=0`）**全局止于 2025-10-31**。

**待验证假设（TDX 每股口径除权公式，逐事件、对除权日前所有行）：**
```
前复权:  p_adj = (p × 10 − fenhong + peigu × peigujia) / (10 + fenshu + songzhuangu + peigu)
逆运算:  p     = (p_adj × (10 + fenshu + songzhuangu + peigu) + fenhong − peigu × peigujia) / 10
```
逆运算按事件**从新到旧**依次应用（复权是旧→新逐层叠加，还原需逆序剥离）。**必须同时判定** volume/amount 语义：老行 volume 带小数（疑似按送转比例放大）、amount 出现负值（疑似 adjusted price × volume 重算）——spike 输出三者各自的重建规则。

- [ ] **步骤 1：编写 spike 脚本**

```python
"""Spike: 验证 TDX 前复权逆运算公式（任务 1 闸门）。

用法: set -a; source ../fquant/.env; set +a
      TDX_DATA_DIR=/Volumes/vol3/tdx uv run python scripts/spike_raw_reconstruct.py
通过标准: 3 只样本股 price 全区间(≤2025-10-31)对拍 fstore fq=0 误差 < 0.01 元。
"""
from __future__ import annotations
import os
from pathlib import Path
import polars as pl
from app.data_providers.fquant.fstore_client import FStoreClient

TDX = Path(os.environ.get("TDX_DATA_DIR", "/Volumes/vol3/tdx"))
# 高分红 / 高送转 / ST 各一只
SAMPLES = [("600519", "sh"), ("300059", "sz"), ("600186", "sh")]

def read_day(code: str, mkt: str) -> pl.DataFrame:
    p = TDX / "day" / f"{mkt}{code[:3]}" / f"{mkt}{code}.csv"
    return pl.read_csv(p).with_columns(pl.col("date").cast(pl.Utf8))

def read_xdxr(code: str, mkt: str) -> list[dict]:
    p = TDX / "xdxr" / f"{mkt}{code[:3]}" / f"{mkt}{code}.csv"
    if not p.exists():
        return []
    df = pl.read_csv(p)
    return [
        {"trade_date": str(r["Date"]), "fenhong": float(r["FenHong"] or 0),
         "fenshu": float(r["FenShu"] or 0), "songzhuangu": float(r["SongZhuanGu"] or 0),
         "peigu": float(r["PeiGu"] or 0), "peigujia": float(r["PeiGuJia"] or 0),
         "category": int(r["Category"] or 0)}
        for r in df.iter_rows(named=True)
    ]

def invert(df: pl.DataFrame, events: list[dict]) -> pl.DataFrame:
    """逆运算假设: 事件从新到旧, 对 date < 事件日的行剥离一层复权。"""
    price_cols = ["open", "close", "high", "low"]
    for ev in sorted(events, key=lambda e: e["trade_date"], reverse=True):
        if ev["category"] != 1:          # 仅 除权除息 事件参与价格复权(假设,待验证)
            continue
        denom = 10 + ev["fenshu"] + ev["songzhuangu"] + ev["peigu"]
        mask = pl.col("date") < ev["trade_date"]
        df = df.with_columns([
            pl.when(mask)
              .then((pl.col(c) * denom + ev["fenhong"] - ev["peigu"] * ev["peigujia"]) / 10)
              .otherwise(pl.col(c)).alias(c)
            for c in price_cols
        ] + [
            # volume 假设: 按股本扩张比例放大过 → 逆运算除回
            pl.when(mask).then(pl.col("volume") * 10 / denom)
              .otherwise(pl.col("volume")).alias("volume"),
        ])
    return df

def main() -> None:
    c = FStoreClient()
    all_pass = True
    for code, mkt in SAMPLES:
        disk = read_day(code, mkt)
        raw = invert(disk, read_xdxr(code, mkt))
        oracle = pl.DataFrame(c.query(
            "SELECT tdate::text AS date, open::float8 AS o_open, close::float8 AS o_close "
            "FROM day_klines WHERE code=%s AND ktype=101 AND fq=0 ORDER BY tdate", (code,)))
        if oracle.is_empty():
            print(f"[{code}] SKIP: fstore 无数据"); continue
        j = raw.join(oracle, on="date", how="inner")
        diff = j.select(
            (pl.col("close") - pl.col("o_close")).abs().max().alias("max_close"),
            (pl.col("open") - pl.col("o_open")).abs().max().alias("max_open"),
            pl.len().alias("n"))
        mc, mo, n = diff.row(0)
        ok = mc < 0.01 and mo < 0.01
        all_pass &= ok
        print(f"[{code}] rows={n} max_close_diff={mc:.4f} max_open_diff={mo:.4f} "
              f"{'PASS' if ok else 'FAIL'}")
        # adjustment_count=0 尾部行自检: 逆运算不应改动它们
        tail = disk.filter(pl.col("adjustment_count") == 0).join(
            raw, on="date", suffix="_r")
        td = tail.select((pl.col("close") - pl.col("close_r")).abs().max()).item()
        print(f"[{code}] tail(adj_count=0) 自检 diff={td}")
    print("=== SPIKE", "PASS ✅" if all_pass else "FAIL ❌ — 停止计划,调整公式假设并报告", "===")

if __name__ == "__main__":
    main()
```

- [ ] **步骤 2：运行 spike**

运行：`cd backend && set -a && source ../../fquant/.env && set +a && uv run python scripts/spike_raw_reconstruct.py`
预期：3 只样本全 `PASS`。同时人工检查输出：确认 `category != 1` 事件是否需要参与（若 FAIL，优先排查 category=5/6 股本变化事件与 peigu 项）。

- [ ] **步骤 3：判定闸门**

- 全 PASS → 记录 volume/amount 结论到脚本 docstring，进入任务 2。
- FAIL → **停止执行**，把 diff 分布（按日期段）输出给用户，等待公式修订决策。

- [ ] **步骤 4：授权后可选提交**

```bash
git add backend/scripts/spike_raw_reconstruct.py
# 用户明确授权后再执行: git commit -m "spike: TDX 前复权逆运算公式验证脚本(任务1闸门)"
```

## 阶段 1 — raw 重建（修已上线 bug，独立于本地模式）

### 任务 2：`raw_reconstruct.py` 纯函数模块（TDD）

**文件：**
- 创建：`backend/app/data_providers/fquant/raw_reconstruct.py`
- 测试：`backend/tests/data_providers/test_raw_reconstruct.py`

- [ ] **步骤 1：编写失败的测试**

```python
"""raw_reconstruct 单测 — 用构造数据验证逆运算，不依赖磁盘/PG。"""
import polars as pl
from app.data_providers.fquant.raw_reconstruct import reconstruct_raw_rows

# 构造: raw close=110, 除权日 2024-06-05 每10股派10元(fenhong=10)
# 前复权: p_adj = (110*10 - 10)/10 = 109.0 (除权日前行)
EVENTS = [{"trade_date": "2024-06-05", "fenhong": 10.0, "fenshu": 0.0,
           "songzhuangu": 0.0, "peigu": 0.0, "peigujia": 0.0, "category": 1}]

def _rows(close_pre: float, close_post: float) -> list[dict]:
    return [
        {"date": "2024-06-04", "open": close_pre, "high": close_pre,
         "low": close_pre, "close": close_pre, "volume": 100.0,
         "amount": 1.0, "adjustment_count": 1},
        {"date": "2024-06-05", "open": close_post, "high": close_post,
         "low": close_post, "close": close_post, "volume": 100.0,
         "amount": 1.0, "adjustment_count": 0},
    ]

def test_dividend_inverse():
    out = reconstruct_raw_rows(_rows(109.0, 109.5), EVENTS)
    assert abs(out[0]["close"] - 110.0) < 1e-9      # 除权前行还原
    assert abs(out[1]["close"] - 109.5) < 1e-9      # 除权后行不动

def test_songzhuan_inverse():
    ev = [{**EVENTS[0], "fenhong": 0.0, "songzhuangu": 10.0}]
    # raw=20, 10送10 → p_adj = 20*10/20 = 10
    out = reconstruct_raw_rows(_rows(10.0, 10.2), ev)
    assert abs(out[0]["close"] - 20.0) < 1e-9

def test_no_events_passthrough():
    rows = _rows(9.0, 9.1)
    assert reconstruct_raw_rows(rows, []) == rows

def test_multi_event_reverse_order():
    # 两次除权: 2023 派 5 元 → 2024 派 10 元; raw(2022)=100
    # 顺向: 100 → (1000-5)/10=99.5 → (995-10)/10=98.5
    evs = [{**EVENTS[0], "trade_date": "2023-06-05", "fenhong": 5.0}, EVENTS[0]]
    rows = [{"date": "2022-01-04", "open": 98.5, "high": 98.5, "low": 98.5,
             "close": 98.5, "volume": 100.0, "amount": 1.0, "adjustment_count": 2}]
    out = reconstruct_raw_rows(rows, evs)
    assert abs(out[0]["close"] - 100.0) < 1e-9
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd backend && uv run pytest tests/data_providers/test_raw_reconstruct.py -v`
预期：FAIL，`ModuleNotFoundError: raw_reconstruct`

- [ ] **步骤 3：实现模块**

```python
"""TDX 减法前复权逆运算 → 原始价重建（设计 D-L3）。

输入: mapping 层 daily 行(list[dict], 含 adjustment_count 时优先自检)
      + xdxr 事件(mapping.xdxr_rows_to_events 同构字段)。
输出: 原始价 daily 行。纯函数、无 IO。
公式(任务 1 spike 验证): p = (p_adj*(10+fenshu+songzhuangu+peigu) + fenhong - peigu*peigujia)/10
volume 逆运算: v = v_adj * 10 / (10+fenshu+songzhuangu+peigu)  # spike 结论如不同,以 spike 为准修正
"""
from __future__ import annotations

_PRICE_KEYS = ("open", "high", "low", "close", "last_close")


def reconstruct_raw_rows(rows: list[dict], events: list[dict]) -> list[dict]:
    if not rows or not events:
        return rows
    evs = sorted(
        (e for e in events if e.get("category") == 1 and e.get("trade_date")),
        key=lambda e: str(e["trade_date"]), reverse=True,
    )
    if not evs:
        return rows
    out = [dict(r) for r in rows]
    for ev in evs:
        fh = float(ev.get("fenhong") or 0)
        denom = 10.0 + float(ev.get("fenshu") or 0) \
            + float(ev.get("songzhuangu") or 0) + float(ev.get("peigu") or 0)
        pg_amt = float(ev.get("peigu") or 0) * float(ev.get("peigujia") or 0)
        ex_date = str(ev["trade_date"])
        for r in out:
            if str(r.get("date") or "") >= ex_date:
                continue
            for k in _PRICE_KEYS:
                if r.get(k) is not None:
                    r[k] = (float(r[k]) * denom + fh - pg_amt) / 10.0
            if r.get("volume") is not None:
                r["volume"] = float(r["volume"]) * 10.0 / denom
    return out
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/data_providers/test_raw_reconstruct.py -v` → 4 PASS

- [ ] **步骤 5：授权后可选提交**

```bash
git add backend/app/data_providers/fquant/raw_reconstruct.py backend/tests/data_providers/test_raw_reconstruct.py
# 用户明确授权后再执行: git commit -m "feat(provider): TDX 前复权逆运算 raw 重建纯函数"
```

### 任务 3：raw 重建接入 daily + adj_factor 双链

**文件：**
- 修改：`backend/app/data_providers/fquant_provider.py`（`_get_daily_from_engine_wide` 与 `_build_daily_close_map`）
- 测试：`backend/tests/data_providers/test_provider_raw_chain.py`

**要点（codex review #2）：** 重建必须发生在 `normalize_daily` 之前（normalizer 只留 8 canonical 列会丢 `adjustment_count`）；`get_adj_factors` 的 `pre_close` 链同步换用重建后 close，否则 ex_factor 仍基于污染价。

- [ ] **步骤 1：编写失败的测试**

```python
"""provider daily 链路应输出重建后的 raw 价格。mock EngineDataClient。"""
from unittest.mock import patch
from app.data_providers.fquant_provider import FQuantProvider

WIDE_ROWS = [  # engine wide 返回: 前复权价, 除权日 2024-06-05 派 10 元
    {"date": "2024-06-04", "open": 109.0, "high": 109.0, "low": 109.0,
     "close": 109.0, "volume": 100, "amount": 1.0, "adjustment_count": 1},
    {"date": "2024-06-05", "open": 109.5, "high": 109.5, "low": 109.5,
     "close": 109.5, "volume": 100, "amount": 1.0, "adjustment_count": 0},
]
XDXR_ROWS = [{"date": "2024-06-05", "fenhong": 10.0, "fenshu": 0, "songzhuangu": 0,
              "peigu": 0, "peigujia": 0, "category": 1}]

@patch("app.data_providers.fquant_provider.EngineDataClient")
@patch("app.data_providers.fquant_provider.FStoreClient")
@patch("app.data_providers.fquant_provider.MoneyflowClient")
def test_get_daily_returns_raw(_mf, _fs, mock_engine):
    inst = mock_engine.return_value
    inst.get_wide.return_value = list(reversed(WIDE_ROWS))  # engine 最新在前
    inst.get_xdxr.return_value = XDXR_ROWS
    p = FQuantProvider()
    df = p.get_daily(["600519.SH"], None, None, "stock")
    pre = df.filter(df["date"].cast(str) == "2024-06-04")
    assert abs(pre["close"][0] - 110.0) < 1e-6   # 已还原 raw
```

- [ ] **步骤 2：运行验证失败**

`uv run pytest tests/data_providers/test_provider_raw_chain.py -v` → FAIL（close 仍为 109.0）

- [ ] **步骤 3：实现接入**

在 `fquant_provider.py` 的 `_get_daily_from_engine_wide` 末尾（`return wide_rows_to_daily(...)` 之前）插入重建；`xdxr_rows_to_events` 是 mapping 现有函数：

```python
        # D-L3: engine 上游是 TDX 前复权序列, 在 normalize 之前还原 raw
        if rows:
            from app.data_providers.fquant.raw_reconstruct import reconstruct_raw_rows
            from app.data_providers.fquant.mapping import xdxr_rows_to_events
            xdxr = self._engine.get_xdxr(code)
            events = xdxr_rows_to_events(xdxr, symbol) if xdxr else []
            rows = reconstruct_raw_rows(rows, events)
        return wide_rows_to_daily(rows, symbol, source=self.name)
```

`_build_daily_close_map` 内部走 `_get_daily_from_engine_wide`（改后自动是 raw），确认无独立取数路径即可（有则同样包一层）。

- [ ] **步骤 4：运行验证通过 + 回归**

`uv run pytest tests/data_providers/ -v` → 全 PASS
`uv run python scripts/test_fquant_provider.py`（有 PG env 时）→ 16 项不回退

- [ ] **步骤 5：授权后可选提交**

```bash
git add backend/app/data_providers/fquant_provider.py backend/tests/data_providers/test_provider_raw_chain.py
# 用户明确授权后再执行: git commit -m "fix(provider): engine daily 链路接入 raw 重建,修复前复权污染(D-L3)"
```

### 任务 4：存量污染分区重刷脚本

**文件：**
- 创建：`backend/scripts/refresh_polluted_daily.py`

**边界：** 该脚本只用于迁移前/`DATA_PROVIDER=fquant` HTTP 模式已污染 `data/kline_daily` 的一次性修复；`fquant_local` 上线后的日常路径仍禁止写 stock raw mirror。

- [ ] **步骤 1：编写脚本**

```python
"""重刷 fquant 模式同步以来被前复权污染的 kline_daily 分区 + enriched 重算。

用法: DATA_PROVIDER=fquant uv run python scripts/refresh_polluted_daily.py --since 2026-07-01
逻辑: 对 --since 起的每个 kline_daily 分区内全部 symbol, 经修复后的 provider
      重新拉取并 append_daily(merge-upsert 覆盖), 然后 run_pipeline 重算 enriched。
"""
from __future__ import annotations
import argparse
from datetime import date, datetime
from pathlib import Path
import polars as pl
from app.config import settings
from app.services import kline_sync
from app.tickflow.repository import KlineRepository
from app.tickflow.capabilities import detect_capabilities

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="首个可能污染的分区日期 YYYY-MM-DD")
    args = ap.parse_args()
    since = date.fromisoformat(args.since)
    d = Path(settings.data_dir) / "kline_daily"
    parts = sorted(p for p in d.glob("date=*") if p.name.split("=")[1] >= args.since)
    symbols: set[str] = set()
    for p in parts:
        symbols |= set(pl.read_parquet(p / "part.parquet")["symbol"].to_list())
    print(f"重刷 {len(parts)} 个分区, {len(symbols)} 只标的, since={since}")
    repo = KlineRepository()
    capset = detect_capabilities()
    n = kline_sync.sync_and_persist_daily_batch(
        sorted(symbols), repo, capset,
        start_date=datetime.combine(since, datetime.min.time()),
        end_date=datetime.now())
    print(f"daily 重写 {n} 行; 现在重算 enriched…")
    from app.indicators.pipeline import run_pipeline
    rows = run_pipeline(symbols=sorted(symbols), new_dates_only=False)
    print(f"enriched 重算 {rows} 行 ✅")

if __name__ == "__main__":
    main()
```

- [ ] **步骤 2：运行（用户环境，需 PG env）**

`cd backend && set -a && source ../../fquant/.env && set +a && DATA_PROVIDER=fquant uv run python scripts/refresh_polluted_daily.py --since <fquant 切换首日,查 git log 12d1c98 日期>`
预期：重刷完成后抽查 `600519.SH` 除权日前分区 close 不再带 `.075769` 尾巴：
`uv run python -c "import polars as pl; print(pl.read_parquet('../data/kline_daily/date=2026-06-25/part.parquet').filter(pl.col('symbol')=='600519.SH'))"` → close=1199.72 附近的整分值（以 spike oracle 为准）

- [ ] **步骤 3：授权后可选提交**

```bash
git add backend/scripts/refresh_polluted_daily.py
# 用户明确授权后再执行: git commit -m "chore(scripts): 存量污染 kline_daily 分区重刷脚本"
```

---

## 阶段 2 — `fquant_local` 完整切换入口

### 任务 5：registry 工厂化 + 注册 `fquant_local`

**文件：**
- 修改：`backend/app/data_providers/registry.py`
- 修改：`backend/app/data_providers/fquant_provider.py`（`__init__` 加 `engine_mode` 参数）
- 测试：`backend/tests/data_providers/test_registry.py`

- [ ] **步骤 1：编写失败的测试**

```python
from unittest.mock import patch
from app.data_providers import registry

def test_fquant_local_registered():
    assert registry.normalize_provider_name("fquant_local") == "fquant_local"

@patch("app.data_providers.fquant_provider.EngineDataClient")
@patch("app.data_providers.fquant_provider.FStoreClient")
@patch("app.data_providers.fquant_provider.MoneyflowClient")
def test_fquant_local_uses_disk_engine(_mf, _fs, _en):
    p = registry.get_provider("fquant_local")
    assert p.name == "fquant_local"
    from app.data_providers.fquant.engine_data_disk import EngineDataDiskClient
    assert isinstance(p._engine, EngineDataDiskClient)
```

- [ ] **步骤 2：运行验证失败** → `Unsupported data provider: fquant_local`

- [ ] **步骤 3：实现**

`registry.py` 的 `_PROVIDERS` 改工厂（值统一为无参 callable，`get_provider` 不变即可调用）：

```python
_PROVIDERS = {
    "tickflow": TickFlowProvider,
    "fquant": FQuantProvider,
    "fquant_local": lambda: FQuantProvider(engine_mode="disk"),
}
```

`fquant_provider.py` `__init__` 签名与 engine 接线改为：

```python
    def __init__(self, engine_mode: str = "http") -> None:
        self._fstore = FStoreClient()
        if engine_mode == "disk":
            from app.data_providers.fquant.engine_data_disk import EngineDataDiskClient
            self._engine = EngineDataDiskClient()
            self.name = "fquant_local"
        else:
            self._engine = EngineDataClient()
        self._engine_mode = engine_mode
        ...  # 其余原样
```

（`name` 原为类属性 `"fquant"`，disk 模式实例覆盖为 `"fquant_local"`。任务 8 前先放一个最小 `EngineDataDiskClient` 占位类——只含与 `EngineDataClient` 相同的空方法签名，任务 8 用 TDD 填实。）

- [ ] **步骤 4：运行验证通过；授权后可选提交**

```bash
uv run pytest tests/data_providers/test_registry.py -v
git add -A backend/app/data_providers
# 用户明确授权后再执行: git commit -m "feat(provider): registry 工厂化并注册 fquant_local"
```

### 任务 6：preferences 白名单 + settings API

**文件：**
- 修改：`backend/app/services/preferences.py:99`（`_ALLOWED_DATA_PROVIDERS`）
- 测试：`backend/tests/services/test_preferences_provider.py`

- [ ] **步骤 1：测试**

```python
from app.services import preferences

def test_fquant_local_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "_PREFS_PATH", tmp_path / "preferences.json", raising=False)
    assert preferences.set_data_provider("fquant_local") == "fquant_local"
    assert preferences._clean_data_provider("fquant_local") == "fquant_local"

def test_unknown_still_rejected():
    import pytest
    with pytest.raises(ValueError):
        preferences.set_data_provider("akshare")
```

（若 `_PREFS_PATH` 属性名不符，按 `preferences.py` 顶部实际存储路径常量调整 monkeypatch 目标——先 `grep -n "def load\|def save\|Path" app/services/preferences.py` 确认。）

- [ ] **步骤 2：失败 → 实现**

```python
_ALLOWED_DATA_PROVIDERS = {"tickflow", "fquant", "fquant_local"}
```

settings API（`api/settings.py:386` `update_data_provider`）走 `preferences.set_data_provider` 校验，白名单放开后自动生效，无需改动；确认 `_reset_data_provider_singletons()`（settings.py:318）覆盖 kline_sync/quote_service 等 4 个单例即可。

- [ ] **步骤 3：通过；授权后可选提交**

```bash
uv run pytest tests/services/test_preferences_provider.py -v
git add -A backend/app/services backend/tests
# 用户明确授权后再执行: git commit -m "feat(settings): provider 白名单放开 fquant_local"
```

### 任务 6.5：per-capability provider resolver 真接线

**文件：**
- 修改：`backend/app/data_providers/registry.py`
- 修改：`backend/app/services/kline_sync.py` / `quote_service.py` / `financial_sync.py` / `instrument_sync.py`（按 capability 取 provider）
- 测试：`backend/tests/data_providers/test_registry.py`

- [ ] **步骤 1：测试**

```python
from app.data_providers import registry
from app.services import preferences

def test_daily_provider_uses_daily_preference(monkeypatch):
    monkeypatch.delenv("DATA_PROVIDER", raising=False)
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "fquant_local")
    assert registry.get_active_provider_name("daily") == "fquant_local"

def test_env_provider_overrides_capability_preference(monkeypatch):
    monkeypatch.setenv("DATA_PROVIDER", "tickflow")
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "fquant_local")
    assert registry.get_active_provider_name("daily") == "tickflow"
```

- [ ] **步骤 2：实现**

扩展现有 `registry.get_active_provider_name()`，不要新增第二套 resolver：

```python
def get_active_provider_name(capability: str | None = None) -> str:
    env_provider = os.environ.get("DATA_PROVIDER")
    if env_provider:
        return normalize_provider_name(env_provider)

    from app.services import preferences
    if capability == "daily":
        return preferences.get_daily_data_provider()
    if capability == "minute":
        return preferences.get_minute_data_provider()
    if capability == "realtime":
        return preferences.get_realtime_data_provider()
    if capability == "adj_factor":
        p = preferences.get_adj_factor_provider()
        return preferences.get_daily_data_provider() if p == "same_as_daily" else p
    return preferences.get_data_provider()
```

服务工厂按真实用途传 capability：`kline_sync` 用 `"daily"`，`quote_service` 用 `"realtime"`，财务/标的仍走全局 provider（除非已有独立偏好字段）。

- [ ] **步骤 3：验证**

```bash
uv run pytest tests/data_providers/test_registry.py -v
uv run pytest tests/services/test_preferences_provider.py -v
```

提交只在用户明确授权后执行：

```bash
git add -A backend/app backend/tests
# 用户明确授权后再执行: git commit -m "feat(provider): 接入 per-capability provider resolver"
```

### 任务 7：前端 union + 下拉

**文件：**
- 修改：`frontend/src/lib/api.ts:777`、`frontend/src/pages/settings/System.tsx:23,52,96-100`

- [ ] **步骤 1：修改**

api.ts：`updateDataProvider: (data_provider: 'tickflow' | 'fquant' | 'fquant_local') =>`
System.tsx：`saveDataProvider` 回调与 `onChange` 断言同步改为三元 union；`<select>` 内加：

```tsx
            <option value="fquant_local">FQuant 本地磁盘</option>
```

- [ ] **步骤 2：验证；授权后可选提交**

运行：`cd frontend && pnpm tsc --noEmit`（或 `pnpm build`）→ 无类型错误

```bash
git add frontend/src/lib/api.ts frontend/src/pages/settings/System.tsx
# 用户明确授权后再执行: git commit -m "feat(ui): 设置页数据源下拉新增 fquant_local"
```

### 任务 8：`EngineDataDiskClient`（TDD）

**文件：**
- 创建/填实：`backend/app/data_providers/fquant/engine_data_disk.py`
- 测试：`backend/tests/data_providers/test_engine_data_disk.py`（用 tmp_path fixture 造 CSV，不碰真盘）

- [ ] **步骤 1：测试**

```python
import polars as pl
from app.data_providers.fquant.engine_data_disk import EngineDataDiskClient, _csv_path

DAY_CSV = """date,open,close,high,low,volume,amount,up,down,datetime,adjustment_count
2026-06-30,1187,1185.49,1195.67,1176,3960700,4684236288,0,0,2026-06-30 15:00:00,0
2026-07-01,1180.1,1193.01,1196.8,1166.33,4247300,5033838080,0,0,2026-07-01 15:00:00,0
"""
WIDE_CSV = """date,open,close,high,low,volume,amount,last_close,change_rate,datetime,adjustment_count
2026-07-01,1180.1,1193.01,1196.8,1166.33,4247300,5033838080,1185.49,0.63,2026-07-01 15:00:00,0
"""
XDXR_CSV = """Date,Category,Name,FenHong,PeiGuJia,SongZhuanGu,PeiGu,SuoGu,QianLiuTong,HouLiuTong,QianZongGuBen,HouZongGuBen,FenShu,XingQuanJia
2002-07-25,1,除权除息,6,0,1,0,0,0,0,0,0,0,0
"""

def _mkdisk(tmp_path):
    (tmp_path / "day" / "sh600").mkdir(parents=True)
    (tmp_path / "day" / "sh600" / "sh600519.csv").write_text(DAY_CSV)
    (tmp_path / "wide" / "sh600").mkdir(parents=True)
    (tmp_path / "wide" / "sh600" / "sh600519.csv").write_text(WIDE_CSV)
    (tmp_path / "xdxr" / "sh600").mkdir(parents=True)
    (tmp_path / "xdxr" / "sh600" / "sh600519.csv").write_text(XDXR_CSV)
    return tmp_path

def test_path_rule(tmp_path):
    assert _csv_path(tmp_path, "day", "600519.SH") == tmp_path / "day" / "sh600" / "sh600519.csv"
    assert _csv_path(tmp_path, "day", "300059.SZ") == tmp_path / "day" / "sz300" / "sz300059.csv"
    assert _csv_path(tmp_path, "day", "899050.BJ") == tmp_path / "day" / "bj899" / "bj899050.csv"

def test_get_wide_reads_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("TDX_DATA_DIR", str(_mkdisk(tmp_path)))
    c = EngineDataDiskClient()
    rows = c.get_wide("600519.SH", limit=250)
    assert len(rows) == 1 and rows[0]["date"] == "2026-07-01"
    assert rows[0]["last_close"] == 1185.49 and rows[0]["change_rate"] == 0.63

def test_get_wide_falls_back_to_day(tmp_path, monkeypatch):
    root = _mkdisk(tmp_path)
    (root / "wide" / "sh600" / "sh600519.csv").unlink()
    monkeypatch.setenv("TDX_DATA_DIR", str(root))
    rows = EngineDataDiskClient().get_wide("600519.SH", limit=250)
    assert len(rows) == 2
    assert "last_close" not in rows[0]  # day 降级路径缺增强字段

def test_get_xdxr_maps_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("TDX_DATA_DIR", str(_mkdisk(tmp_path)))
    rows = EngineDataDiskClient().get_xdxr("600519.SH")
    assert rows[0]["fenhong"] == 6.0 and rows[0]["songzhuangu"] == 1.0
    assert rows[0]["category"] == 1

def test_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("TDX_DATA_DIR", str(_mkdisk(tmp_path)))
    c = EngineDataDiskClient()
    assert c.get_wide("000001.SZ") == []      # 无文件 → 空,不抛异常

def test_freshness(tmp_path, monkeypatch):
    monkeypatch.setenv("TDX_DATA_DIR", str(_mkdisk(tmp_path)))
    assert str(EngineDataDiskClient().freshness("600519.SH")) == "2026-07-02"
```

- [ ] **步骤 2：失败 → 实现**

```python
"""TDX 磁盘 CSV 客户端（D-L2）。接口对齐 EngineDataClient, 由 symbol 直构路径, 零目录扫描。

TDX_DATA_DIR 对 panel 严格只读（D-L6）。get_wide 优先读 wide/ 保留 last_close/change_rate；
wide 缺文件时才降级 day/（无增强字段, mapping 可容忍缺列）。wide/ 实测无 adjustment_count；
raw 重建依赖 xdxr 事件，只有 day/ 降级路径可携带 adjustment_count 作为自检位。
"""
from __future__ import annotations
import logging
import os
from datetime import date
from pathlib import Path
import polars as pl

logger = logging.getLogger(__name__)
_MKT = {"SH": "sh", "SZ": "sz", "BJ": "bj"}


def _csv_path(base: Path, dataset: str, symbol: str) -> Path:
    code, _, suffix = symbol.partition(".")
    mkt = _MKT.get(suffix.upper(), "sh")
    return base / dataset / f"{mkt}{code[:3]}" / f"{mkt}{code}.csv"


class EngineDataDiskClient:
    def __init__(self) -> None:
        self.base = Path(os.environ.get("TDX_DATA_DIR", "/Volumes/vol3/tdx"))

    def _read(self, dataset: str, symbol: str) -> pl.DataFrame:
        p = _csv_path(self.base, dataset, symbol)
        if not p.exists():
            logger.debug("disk csv missing: %s", p)
            return pl.DataFrame()
        try:
            return pl.read_csv(p, infer_schema_length=200)
        except Exception as e:  # noqa: BLE001
            logger.warning("disk csv read failed %s: %s", p, e)
            return pl.DataFrame()

    def get_wide(self, symbol: str, limit: int = 250) -> list[dict]:
        df = self._read("wide", symbol)
        if df.is_empty():
            df = self._read("day", symbol)
        if df.is_empty():
            return []
        return df.tail(limit).with_columns(pl.col("date").cast(pl.Utf8)).to_dicts()

    def get_day(self, symbol: str, limit: int = 250) -> list[dict]:
        return self.get_wide(symbol, limit)

    def get_xdxr(self, symbol: str, limit: int = 100) -> list[dict]:
        df = self._read("xdxr", symbol)
        if df.is_empty():
            return []
        return [
            {"date": str(r["Date"]), "fenhong": float(r["FenHong"] or 0),
             "fenshu": float(r["FenShu"] or 0), "songzhuangu": float(r["SongZhuanGu"] or 0),
             "peigu": float(r["PeiGu"] or 0), "peigujia": float(r["PeiGuJia"] or 0),
             "category": int(r["Category"] or 0)}
            for r in df.tail(limit).iter_rows(named=True)
        ]

    def get_minutes(self, symbol: str, date_yyyymmdd: str, limit: int = 5000) -> list[dict]:
        return []   # P2: minutes/ 格式待 spike(设计 §5), 先空

    def get_trans(self, symbol: str, date_yyyymmdd: str, limit: int = 5000) -> list[dict]:
        return []

    def freshness(self, symbol: str = "600519.SH") -> date | None:
        """D-L6 保鲜探测: 基准股 CSV 最后日期。"""
        rows = self.get_wide(symbol, limit=1)
        return date.fromisoformat(rows[-1]["date"]) if rows else None
```

- [ ] **步骤 3：provider 侧适配调用参数**

任务 3 改过的 `_get_daily_from_engine_wide` / `get_xdxr` 调用在 disk 模式传 `sym` 而非 `code`：在 `fquant_provider.py` 加一行辅助后统一：

```python
    def _engine_key(self, symbol: str, code: str) -> str:
        return symbol if self._engine_mode == "disk" else code
```

把 `self._engine.get_wide(code, ...)` / `self._engine.get_xdxr(code)` 处改为 `self._engine.get_wide(self._engine_key(sym, code), ...)`（涉及 `_get_daily_from_engine_wide`、任务 3 插入的 xdxr 调用、`get_adj_factors` 的 xdxr 主源路径——`grep -n "_engine\.get" app/data_providers/fquant_provider.py` 逐处过一遍）。disk 模式所有按文件路径寻址的方法（`wide/xdxr/minutes/trans/fund`）都必须传带交易所后缀的 `symbol`，避免 `000001.SH` 这类指数被纯 code 猜成 SZ 路径；HTTP engine 仍传纯 code。

raw oracle / xdxr 逆运算只用于 `asset_type="stock"`；指数/ETF 直接使用磁盘行情，避免 `000001.SH` 等指数被同 code 股票 oracle 覆盖。
调用侧也必须透传资产类型：`index_sync` 的指数/ETF 同步分别传 `asset_type="index"` / `"etf"`，`api/indices.py` 单指数 daily/minute fallback 传 `asset_type="index"`；股票路径保留默认 `"stock"`。

- [ ] **步骤 4：全部测试通过；授权后可选提交**

```bash
uv run pytest tests/data_providers/ -v
git add -A backend/app/data_providers backend/tests
# 用户明确授权后再执行: git commit -m "feat(provider): EngineDataDiskClient 磁盘直读客户端(D-L2)"
```

### 任务 9：真盘冒烟（一次性验证，不进测试套件）

- [ ] **步骤 1：运行**

```bash
cd backend && set -a && source ../../fquant/.env && set +a
DATA_PROVIDER=fquant_local TDX_DATA_DIR=/Volumes/vol3/tdx uv run python -c "
from app.data_providers.registry import get_provider
p = get_provider('fquant_local')
df = p.get_daily(['600519.SH'], None, None, 'stock')
print(df.tail(3)); print('freshness:', p._engine.freshness())
adj = p.get_adj_factors(['600519.SH'], None, None, 'stock')
print('adj rows:', adj.height)"
```
预期：日 K 尾部为原始价（对照 spike oracle）；freshness=最近交易日；adj 非空。

---

## 阶段 3 — 写盘门控 + pipeline 磁盘输入（D-L5）

### 任务 10：`data_mode.py` + repository stock raw 门控（TDD）

**文件：**
- 创建：`backend/app/services/data_mode.py`
- 修改：`backend/app/tickflow/repository.py`（stock raw 写入口：`append_daily`、`append_daily_asset("stock")`、`merge_live_daily_asset("stock")`、`flush_live_daily`、`flush_live_daily_asset("stock")`；index/ETF raw 仍给现有页面和统计使用）
- 测试：`backend/tests/services/test_raw_write_gate.py`

- [ ] **步骤 1：测试**

```python
import polars as pl
from unittest.mock import patch
from app.tickflow.repository import KlineRepository

DF = pl.DataFrame({"symbol": ["600519.SH"], "date": ["2026-07-01"],
                   "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
                   "volume": [1.0], "amount": [1.0]})

@patch("app.services.data_mode.is_local_daily_mode", return_value=True)
def test_append_daily_gated(_m, tmp_path):
    repo = KlineRepository(data_dir=tmp_path)      # 构造参数以实际签名为准
    repo.append_daily(DF)
    assert not (tmp_path / "kline_daily").exists()  # 未写盘

@patch("app.services.data_mode.is_local_daily_mode", return_value=False)
def test_append_daily_passes_when_not_local(_m, tmp_path):
    repo = KlineRepository(data_dir=tmp_path)
    repo.append_daily(DF)
    assert (tmp_path / "kline_daily").exists()

@patch("app.services.data_mode.is_local_daily_mode", return_value=True)
def test_enriched_never_gated(_m, tmp_path):
    repo = KlineRepository(data_dir=tmp_path)
    repo.append_enriched(DF.with_columns(pl.lit(1.0).alias("raw_close")))
    assert (tmp_path / "kline_daily_enriched").exists()   # enriched 是计算缓存,不禁
```

（`KlineRepository` 构造签名先 `grep -n "def __init__" app/tickflow/repository.py` 确认；若不接受 data_dir 参数，用 monkeypatch settings.data_dir。）

- [ ] **步骤 2：失败 → 实现**

`app/services/data_mode.py`：

```python
"""本地磁盘数据源模式判定（设计 D-L5: stock raw mirror 禁写收口）。"""
from __future__ import annotations


def is_local_daily_mode() -> bool:
    """daily 源是否为本地磁盘（fquant_local）。env > daily preference，与 registry 同源。"""
    from app.data_providers.registry import get_active_provider_name
    try:
        return get_active_provider_name("daily") == "fquant_local"
    except Exception:  # noqa: BLE001
        return False
```

`repository.py` 在 stock raw 写入口首部（`if df.is_empty(): return` 之后）插入：

```python
        from app.services.data_mode import is_local_daily_mode
        if is_local_daily_mode():
            logger.debug("stock raw mirror write skipped (fquant_local): rows=%d", df.height)
            return
```

**不加门控**：`append_enriched`、`append_index_enriched`、`append_etf_enriched`、index/ETF raw 及一切 enriched/user_data 写入。index/ETF raw 暂不禁写，因为现有指数/ETF页面、统计和 fallback 路径仍依赖 `kline_index_daily` / `kline_etf_daily`。

- [ ] **步骤 3：验证用例清单（门控生效后逐条确认不落盘）**

上游入口作为验证清单跑一遍（mock provider 数据或真盘）：`kline_sync.sync_and_persist_daily_batch`、`kline_sync.sync_daily_by_quotes`、`daily_pipeline` A股三分支、`extend_history`、`api/kline.py /sync`、`quote_service` 全市场/自选。每条执行后 `data/kline_daily` 无新增；index/ETF raw 允许更新。

- [ ] **步骤 4：通过；授权后可选提交**

```bash
uv run pytest tests/services/test_raw_write_gate.py -v
git add -A backend/app
# 用户明确授权后再执行: git commit -m "feat(repo): 本地模式 stock raw mirror 写盘门控(D-L5)"
```

### 任务 11：pipeline 磁盘输入分支 `run_pipeline_local`

**文件：**
- 修改：`backend/app/indicators/pipeline.py`（新增函数，不动 `run_pipeline`）
- 修改：`backend/app/jobs/daily_pipeline.py`（本地模式分流）
- 测试：`backend/tests/indicators/test_run_pipeline_local.py`

**背景：** 现有 `run_pipeline` 无 `kline_daily` parquet 直接返回 0（pipeline.py:800）。本地模式数据源是 provider（磁盘 CSV + raw 重建），需要独立输入分支；enriched 输出路径/分区格式与现有完全一致。

- [ ] **步骤 1：测试**

```python
import polars as pl
from unittest.mock import MagicMock
from app.indicators.pipeline import run_pipeline_local

def _fake_provider():
    p = MagicMock()
    dates = [f"2026-06-{d:02d}" for d in range(1, 29)]
    p.get_daily.return_value = pl.DataFrame({
        "symbol": ["600519.SH"] * len(dates), "date": dates,
        "open": [100.0] * len(dates), "high": [101.0] * len(dates),
        "low": [99.0] * len(dates), "close": [100.5] * len(dates),
        "volume": [1000.0] * len(dates), "amount": [1e6] * len(dates),
    }).with_columns(pl.col("date").cast(pl.Date))
    p.get_adj_factors.return_value = pl.DataFrame()
    return p

def test_writes_enriched_partitions(tmp_path):
    n = run_pipeline_local(_fake_provider(), ["600519.SH"], data_dir=tmp_path)
    assert n > 0
    parts = list((tmp_path / "kline_daily_enriched").glob("date=*/part.parquet"))
    assert len(parts) == 28
    df = pl.read_parquet(parts[-1])
    assert "raw_close" in df.columns and "consecutive_limit_ups" in df.columns

def test_never_writes_raw_mirror(tmp_path):
    run_pipeline_local(_fake_provider(), ["600519.SH"], data_dir=tmp_path)
    assert not (tmp_path / "kline_daily").exists()
```

- [ ] **步骤 2：失败 → 实现（追加到 pipeline.py 末尾）**

```python
def run_pipeline_local(provider, symbols: list[str],
                       data_dir: Path | None = None,
                       instruments: pl.DataFrame | None = None,
                       on_batch_done: Callable[[int, int], None] | None = None) -> int:
    """本地磁盘模式管道: provider(磁盘 CSV+raw 重建) → enriched, 不经 kline_daily 镜像(D-L5)。

    每批 N 只: provider.get_daily 全历史 + get_adj_factors → compute_enriched → 按日期分区
    merge-upsert 写 kline_daily_enriched。与 run_pipeline 输出格式完全一致。
    """
    d = Path(data_dir or settings.data_dir)
    base = d / "kline_daily_enriched"
    if instruments is None:
        try:
            instruments = pl.scan_parquet(str(d / "instruments" / "**" / "*.parquet")).collect()
        except Exception:  # noqa: BLE001
            instruments = pl.DataFrame()
    from app.services import preferences as prefs_mod
    batch = prefs_mod.get_enriched_batch_size()
    total = len(symbols)
    written = 0
    for i in range(0, total, batch):
        syms = symbols[i:i + batch]
        raw = provider.get_daily(syms, None, None, "stock")
        if raw.is_empty():
            continue
        factors = provider.get_adj_factors(syms, None, None, "stock")
        inst_b = (instruments.filter(pl.col("symbol").is_in(syms))
                  if not instruments.is_empty() else instruments)
        enriched = compute_enriched(raw, factors=factors, instruments=inst_b)
        for date_df in enriched.partition_by("date"):
            dt = date_df["date"][0]
            ds = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
            out = base / f"date={ds}" / "part.parquet"
            out.parent.mkdir(parents=True, exist_ok=True)
            part = _select_storage_cols(date_df)
            if out.exists():
                existing = pl.read_parquet(out).filter(~pl.col("symbol").is_in(syms))
                part = pl.concat([existing, part], how="diagonal_relaxed")
            part.sort("symbol").write_parquet(out)
            written += date_df.height
        if on_batch_done:
            on_batch_done(i // batch + 1, (total + batch - 1) // batch)
    logger.info("run_pipeline_local: %d 只, 写入 %d 行", total, written)
    return written
```

- [ ] **步骤 3：daily_pipeline 分流**

`jobs/daily_pipeline.py` 在 A 股日 K 同步段（`pull_a_share` 判断后、`elif today_exists...` 之前）插入本地模式分支：

```python
    from app.services.data_mode import is_local_daily_mode
    if pull_a_share and is_local_daily_mode():
        from app.indicators.pipeline import run_pipeline_local
        provider = kline_sync._get_data_provider()
        fresh = provider._engine.freshness()
        if fresh is None or fresh < today:
            emit("sync_daily", 45, f"磁盘数据未更新(最后 {fresh}),跳过当日计算(D-L6)")
        else:
            emit("sync_daily", 12, "本地磁盘模式: 直算 enriched(无抓取)…")
            symbols = universe  # 与现有 batch 路径同一 universe 变量
            n = run_pipeline_local(provider, symbols,
                                   on_batch_done=lambda c, t: emit(
                                       "sync_daily", 12 + int(33 * c / t),
                                       f"enriched 批次 {c}/{t}", skip_log=True))
            emit("sync_daily", 45, f"enriched 直算完成 {n} 行")
        # 跳过后续 sync_daily 分支与全量 run_pipeline 重复计算:
        # 将本地模式标记传给后续 enriched 段(阅读该文件 run_pipeline 调用处, 本地模式跳过)
    elif ...  # 原有分支缩进不变
```

（插入时通读 `daily_pipeline.py` 的 enriched 重算段，确保本地模式不重复跑 `run_pipeline`——用同一个 `is_local_daily_mode()` 判定跳过。）

- [ ] **步骤 4：通过；授权后可选提交**

```bash
uv run pytest tests/indicators/test_run_pipeline_local.py -v
git add -A backend/app backend/tests
# 用户明确授权后再执行: git commit -m "feat(pipeline): 本地模式 provider→enriched 直算分支(D-L5)"
```

### 任务 12：单股日 K 空缓存 fallback（不落盘）

**文件：**
- 修改：`backend/app/api/kline.py`（`/daily` 空数据 live-fetch 分支）

- [ ] **步骤 1：定位分支**

运行：`grep -n "live\|fetch\|compute_enriched_single" backend/app/api/kline.py | head` 找到空缓存时调 provider 的分支。

- [ ] **步骤 2：实现**

该分支在本地模式下改为"取数 → `compute_enriched_single` → 直接返回"，禁止触发任何 sync 写入（写入已被任务 10 门控兜底，此处显式短路以免无谓调用）：

```python
        from app.services.data_mode import is_local_daily_mode
        if is_local_daily_mode():
            provider = kline_sync._get_data_provider()
            raw = provider.get_daily([symbol], None, None, asset_type or "stock")
            if raw.is_empty():
                raise HTTPException(404, "本地磁盘无该标的日K数据")
            from app.indicators.pipeline import compute_enriched_single
            return _serialize_daily(compute_enriched_single(raw))  # 序列化沿用该端点现有函数
```

- [ ] **步骤 3：手工验证；授权后可选提交**

`DATA_PROVIDER=fquant_local uv run uvicorn app.main:app --port 8000 &`，`curl 'http://127.0.0.1:8000/api/kline/daily?symbol=600519.SH' | head -c 300` 有数据；`ls ../data/kline_daily 2>/dev/null` 无新建。

```bash
git add backend/app/api/kline.py
# 用户明确授权后再执行: git commit -m "feat(api): 本地模式单股日K直读不落盘"
```

---

## 阶段 4 — realtime（D-L4，依赖任务 10 门控已上线）

### 任务 13：normalizer 显式 realtime 契约并接入 provider

**文件：**
- 修改：`backend/app/data_providers/normalizer.py`（**只追加**，不动既有）
- 测试：`backend/tests/data_providers/test_normalize_realtime.py`

- [ ] **步骤 1：先核对隐式契约**

运行：`grep -n "_quote_row" -A 25 backend/app/data_providers/fquant_provider.py | head -40` 记录字段全集与单位（重点：`change_pct` 是百分数还是小数、volume 股/手——以 `quote_service.py:441/599` 消费方为准）。

- [ ] **步骤 2：测试**

```python
import polars as pl
from app.data_providers.normalizer import REALTIME_COLS, normalize_realtime

def test_schema_and_defaults():
    df = normalize_realtime([{"symbol": "600519.SH", "last_price": 1193.0}], source="test")
    assert list(df.columns) == REALTIME_COLS
    assert df["source"][0] == "test" and df["prev_close"][0] is None

def test_empty():
    assert normalize_realtime([], source="test").is_empty()
```

- [ ] **步骤 3：实现（追加到 normalizer.py）**

```python
# ── realtime 契约(显式化, 原为 fquant_provider._quote_row 隐式字段集) ──
# 单位: 价格=元; volume=股; amount=元; change_pct=百分数值(0.63 表示 0.63%);
# timestamp=ISO8601 本地时间(Asia/Shanghai); ext=dict(change_pct/amplitude/turnover_rate 等)
REALTIME_COLS = ["symbol", "name", "last_price", "prev_close", "open", "high",
                 "low", "volume", "amount", "timestamp", "source", "ext"]


def normalize_realtime(rows: list[dict], source: str) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame()
    out = []
    for r in rows:
        row = {k: r.get(k) for k in REALTIME_COLS}
        row["source"] = r.get("source") or source
        row["ext"] = r.get("ext") or {}
        out.append(row)
    return pl.DataFrame(out, schema_overrides={"ext": pl.Object})
```

（若步骤 1 核对发现单位与注释不符，**以消费方实际语义修正注释**——注释即契约。）

- [ ] **步骤 3.5：接入 FQuantProvider**

`FQuantProvider.get_realtime` 返回前统一调用 `normalize_realtime(rows, source=self.name)`；tdx-api、fstore snapshot、后续 sina/tencent client 都只产出 dict rows，最终 schema 由 normalizer 收口。新增测试用一个缺字段 row 通过 provider 路径，确认 `quote_service` 需要的 `last_price/volume/amount/source/ext` 列稳定存在。

- [ ] **步骤 4：通过；授权后可选提交**

```bash
uv run pytest tests/data_providers/test_normalize_realtime.py -v
git add -A backend/app/data_providers backend/tests
# 用户明确授权后再执行: git commit -m "feat(provider): realtime 契约显式化(normalize_realtime)"
```

### 任务 14：sina/tencent 批量客户端（TDD，fixture 不联网）

**文件：**
- 创建：`backend/app/data_providers/fquant/sina_tencent_client.py`
- 测试：`backend/tests/data_providers/test_sina_tencent.py`

- [ ] **步骤 1：测试**

```python
from unittest.mock import patch, MagicMock
from app.data_providers.fquant.sina_tencent_client import (
    parse_tencent, parse_sina, SinaTencentClient)

TENCENT_FIXTURE = (
    'v_sh600519="1~贵州茅台~600519~1193.01~1185.49~1180.10~42473~21000~21473~'
    + "~".join(["0"] * 24) + '~1196.80~1166.33~1193.01/42473/5033838080~42473~503383~'
    + "~".join(["0"] * 10) + '";'
)
SINA_FIXTURE = (
    'var hq_str_sh600519="贵州茅台,1180.100,1185.490,1193.010,1196.800,1166.330,'
    '1192.900,1193.010,4247300,5033838080.000,' + ",".join(["0"] * 20)
    + ',2026-07-02,14:30:00,00";'
)

def test_parse_tencent():
    rows = parse_tencent(TENCENT_FIXTURE)
    r = rows[0]
    assert r["symbol"] == "600519.SH" and abs(r["last_price"] - 1193.01) < 1e-6
    assert abs(r["prev_close"] - 1185.49) < 1e-6
    assert r["volume"] == 42473 * 100          # 腾讯 volume 单位=手 → 契约股

def test_parse_sina():
    rows = parse_sina(SINA_FIXTURE, ["sh600519"])
    r = rows[0]
    assert r["symbol"] == "600519.SH" and abs(r["last_price"] - 1193.01) < 1e-6
    assert r["volume"] == 4247300               # sina volume 单位=股

def test_batch_chunking():
    c = SinaTencentClient()
    with patch.object(c, "_http_get", return_value=None) as g:
        c.get_quotes([f"{600000+i}.SH" for i in range(130)], prefer="tencent")
        assert g.call_count == 3                # 130/60 → 3 批

def test_partial_failure_keeps_success_rows():
    c = SinaTencentClient()
    with patch.object(c, "_http_get", side_effect=[TENCENT_FIXTURE, None]):
        rows = c.get_quotes(["600519.SH"] + [f"{600000+i}.SH" for i in range(60)], prefer="tencent")
        assert any(r["symbol"] == "600519.SH" for r in rows)
```

- [ ] **步骤 2：失败 → 实现**

```python
"""sina/tencent 批量实时行情客户端(D-L4)。

分工(设计 D-L4): watchlist 链 tencent 分片; full_market 链 sina 大批量为主。
输出行对齐 normalizer.REALTIME_COLS(单位: volume=股, amount=元, 价格=元)。
红线注记: 经 data_providers 抽象层受控接入, 已获用户授权(AGENTS.md 红线#2 修订)。
"""
from __future__ import annotations
import logging
import httpx

logger = logging.getLogger(__name__)
TENCENT_URL = "https://qt.gtimg.cn/q="
SINA_URL = "https://hq.sinajs.cn/list="
TENCENT_BATCH = 60
SINA_BATCH = 100
_SUFFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}


def _to_exch_code(symbol: str) -> str:
    code, _, sfx = symbol.partition(".")
    return f"{_SUFFIX.get(sfx.upper(), 'sh')}{code}"


def _to_symbol(exch_code: str) -> str:
    return f"{exch_code[2:]}.{exch_code[:2].upper()}"


def _f(v: str) -> float | None:
    try:
        x = float(v)
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def parse_tencent(text: str) -> list[dict]:
    """v_sh600519="1~名~code~现价~昨收~今开~量(手)~外盘~内盘~...~最高~最低~..." """
    rows = []
    for line in text.strip().split(";"):
        if "=" not in line:
            continue
        key, _, payload = line.partition("=")
        parts = payload.strip('"').split("~")
        if len(parts) < 40:
            continue
        exch_code = key.split("v_")[-1].strip()
        vol_hand = _f(parts[6])
        rows.append({
            "symbol": _to_symbol(exch_code), "name": parts[1],
            "last_price": _f(parts[3]), "prev_close": _f(parts[4]),
            "open": _f(parts[5]),
            "high": _f(parts[33]), "low": _f(parts[34]),
            "volume": vol_hand * 100 if vol_hand else None,   # 手→股
            "amount": (_f(parts[37]) or 0) * 1e4 or None,     # 万元→元
            "timestamp": None, "source": "tencent", "ext": {},
        })
    return rows


def parse_sina(text: str, exch_codes: list[str]) -> list[dict]:
    """var hq_str_sh600519="名,今开,昨收,现价,最高,最低,买一价,卖一价,量(股),额(元),..." """
    rows = []
    for line in text.strip().split(";"):
        if "hq_str_" not in line:
            continue
        key, _, payload = line.partition("=")
        exch_code = key.split("hq_str_")[-1].strip()
        parts = payload.strip('"').split(",")
        if len(parts) < 32:
            continue
        rows.append({
            "symbol": _to_symbol(exch_code), "name": parts[0],
            "open": _f(parts[1]), "prev_close": _f(parts[2]),
            "last_price": _f(parts[3]), "high": _f(parts[4]), "low": _f(parts[5]),
            "volume": _f(parts[8]), "amount": _f(parts[9]),
            "timestamp": f"{parts[30]}T{parts[31]}", "source": "sina", "ext": {},
        })
    return rows


class SinaTencentClient:
    def __init__(self, timeout: float = 4.0) -> None:
        self.timeout = timeout
        self._failures = 0

    def _http_get(self, url: str, headers: dict | None = None) -> str | None:
        if self._failures >= 3:
            logger.warning("sina/tencent 连续失败, 本轮退避跳过")
            return None
        try:
            resp = httpx.get(url, headers=headers or {}, timeout=self.timeout, trust_env=False)
            resp.raise_for_status()
            self._failures = 0
            return resp.text
        except Exception as e:  # noqa: BLE001
            self._failures += 1
            logger.warning("sina/tencent GET 失败: %s", e)
            return None

    def get_quotes(self, symbols: list[str], prefer: str = "tencent") -> list[dict]:
        codes = [_to_exch_code(s) for s in symbols]
        batch = SINA_BATCH if prefer == "sina" else TENCENT_BATCH
        rows: list[dict] = []
        for i in range(0, len(codes), batch):
            chunk = codes[i:i + batch]
            if prefer == "sina":
                text = self._http_get(SINA_URL + ",".join(chunk),
                                      headers={"Referer": "https://finance.sina.com.cn"})
                if text:
                    rows.extend(parse_sina(text, chunk))
                    continue
            text = self._http_get(TENCENT_URL + ",".join(chunk))
            if text:
                rows.extend(parse_tencent(text))
        return rows
```

**⚠️ 字段索引校准步骤（必做）：** fixture 是按公开口径写的；实现后跑一次真实请求核对索引再锁定 fixture：
`uv run python -c "import httpx; print(httpx.get('https://qt.gtimg.cn/q=sh600519', trust_env=False).text[:400])"`（sina 同理带 Referer）。若索引与 fixture 不符，**同时修正 parse 函数与测试 fixture**（以真实响应为准），并把真实样本粘进测试文件注释。

- [ ] **步骤 3：通过；授权后可选提交**

```bash
uv run pytest tests/data_providers/test_sina_tencent.py -v
git add -A backend/app/data_providers backend/tests
# 用户明确授权后再执行: git commit -m "feat(provider): sina/tencent 批量实时客户端(D-L4)"
```

### 任务 15：realtime 链接线

**文件：**
- 修改：`backend/app/data_providers/fquant_provider.py`（`get_realtime`）

- [ ] **步骤 1：修改 get_realtime 链**

在 tdx-api 尝试之后、fstore 兜底之前插入（watchlist=symbols 小批量→tencent；universes 全市场→sina）：

```python
        if remaining:
            from app.data_providers.fquant.sina_tencent_client import SinaTencentClient
            prefer = "sina" if universes else "tencent"     # D-L4 分工
            st_rows = SinaTencentClient().get_quotes(remaining, prefer=prefer)
            if st_rows:
                rows.extend(st_rows)
                got = {r["symbol"] for r in st_rows if r.get("symbol")}
                remaining = [s for s in remaining if s not in got]
```

- [ ] **步骤 2：验证（真网络，人工）**

```bash
DATA_PROVIDER=fquant_local uv run python -c "
from app.data_providers.registry import get_provider
df = get_provider('fquant_local').get_realtime(symbols=['600519.SH','300059.SZ'])
print(df.select('symbol','last_price','volume','source'))"
```
预期：两行报价，source=tdx-api 或 tencent；随后开 app 确认 QuoteService 盘中路径**未写** `data/kline_daily*`（任务 10 门控生效，`ls -la data/kline_daily/date=$(date +%F) 2>/dev/null` 为空）。

- [ ] **步骤 3：授权后可选提交**

```bash
git add backend/app/data_providers/fquant_provider.py
# 用户明确授权后再执行: git commit -m "feat(provider): realtime 链接入 sina/tencent(watchlist=tencent, full=sina)"
```

---

## 阶段 5 — 端到端 + 文档沉淀

### 任务 16：端到端验证 + AGENTS.md / 进度文档

**文件：**
- 修改：`AGENTS.md`（红线 #2 注记、数据源矩阵、常见排错）
- 修改：`backend/docs/FQUANT_INTEGRATION_PROGRESS.md`（新增"阶段 6：本地磁盘模式"）

- [ ] **步骤 1：端到端清单（全部通过才算完成）**

```bash
# 1. 切换: 设置页选 "FQuant 本地磁盘" 或 DATA_PROVIDER=fquant_local
# 2. capabilities: curl :8000/api/capabilities → realtime=true, depth=false
#    文案必须说明 realtime 来自 tdx-api/sina/tencent/fstore, 不是 TDX 磁盘
# 3. 盘后管道: 设置页"立即跑盘后管道" → 日志出现 "本地磁盘模式: 直算 enriched(无抓取)"
#    且 data/kline_daily 目录零新增分区
# 4. 选股页任一策略卡片扫描有结果; K线页 600519 显示原始价(除权日前无 .075769 尾巴)
# 5. 自选页加 2 只标的, 实时报价刷新; 监控规则命中弹窗正常
# 6. 回切 tickflow → 抓取链路照旧(kline_daily 有写入), 无回归
# 7. 全量测试: cd backend && uv run pytest tests/ -v → 全绿
```

- [ ] **步骤 2：AGENTS.md 修订**

- 红线 #2 追加注记：`（修订 2026-07-XX：经 data_providers 抽象层受控适配器接入 sina/tencent 实时报价已获授权；禁止的是业务层绕过抽象层直连）`
- 数据源矩阵加 `fquant_local` 行（daily=TDX disk wide/day + xdxr；realtime=tdx-api/sina/tencent/fstore snapshot；depth=false；`TDX_DATA_DIR` 配置；stock raw mirror 禁写说明）
- 常见排错加两条：磁盘未挂载 → capability 降级 warning；freshness 落后 → 管道跳过当日。

- [ ] **步骤 3：FQUANT_INTEGRATION_PROGRESS.md 新增阶段 6**

按该文档既有表格风格记录：任务 0-16 完成状态、覆盖闸门结论、raw spike 结论（含 volume/amount 语义判定）、raw 污染修复与重刷范围、stock raw 门控、已知残留（物理 `5min/` 等聚合分钟目录未接，当前由 1m 聚合生成；depth 历史缺口）。

- [ ] **步骤 4：授权后可选提交**

```bash
git add AGENTS.md backend/docs/FQUANT_INTEGRATION_PROGRESS.md
# 用户明确授权后再执行: git commit -m "docs: 本地磁盘数据源模式沉淀(阶段6) + 红线#2修订注记"
```

---

## 自检记录

- **规格覆盖度**：覆盖闸门→任务 0；D-L1→任务 5/6/6.5/7；D-L2→任务 8；D-L3→任务 1/2/3/4；D-L4→任务 13/14/15；D-L5→任务 10/11/12；D-L6→任务 8(freshness)/11(管道探测)；D-L7→任务 16 文档（HTTP 模式废弃标注并入 AGENTS.md 矩阵行说明）。设计 §5 开放项中，868 缺口构成已升为任务 0 硬闸门；minutes 格式、fund/ 资金流仍按后续项/spike 附加项处理，并在任务 16 残留清单记录。
- **占位符扫描**：所有代码块完整可落盘；两处"以实际代码为准"点（preferences 存储路径、KlineRepository 构造签名、kline.py 分支锚点）均给出了确认命令而非留白。
- **类型一致性**：`reconstruct_raw_rows(rows, events)` 任务 2 定义、任务 3 调用一致；`EngineDataDiskClient.get_wide(symbol, limit)` 任务 8 定义、任务 5 占位、任务 9 provider `_engine_key` 适配一致；`is_local_daily_mode()` 任务 10 定义、任务 11/12 调用一致；`normalize_realtime`/`REALTIME_COLS` 任务 13 定义、任务 14 输出行对齐。

---

## 附录 A：设计同步修订（2026-07-02，设计文档更新后与计划的差异，执行时以本附录为准）

**A1（改任务 8）`get_wide` 优先读 `wide/` 目录，缺文件降级 `day/`**
实测 `/Volumes/vol3/tdx/wide/sh600/sh600519.csv` 存在且活跃更新（数据到 2026-07-02，比 day/ 新一天），列 = day 基础列 + `last_close,change_rate,open_volume,open_turnz,open_unmatched,close_volume,close_turnz,close_unmatched,inner_volume,outer_volume,inner_amount,outer_amount`。注意：
- `wide/` **无 `adjustment_count` 列**（day/ 有）——raw 重建本就依赖 xdxr 事件，不依赖该列；自检位仅在 day/ 降级路径可用。
- `last_close` 同样是前复权值——`reconstruct_raw_rows` 的 `_PRICE_KEYS` 需加入 `"last_close"`（若行内存在）一并逆运算；`change_rate` 在重建后语义失真，但 `normalize_daily` 只保留 8 canonical 列会丢弃它，无需处理。
- DiskClient `get_wide`：先试 `wide/` 路径，缺文件 fallback `day/` 并 `logger.debug` 标记降级。

**A2（改任务 10）门控判定按 capability 参数化**
`registry.get_active_provider_name()` 增加可选参数 `capability: str | None = None`：有值时先查 per-capability 偏好（`preferences.get_daily_data_provider()` 等），env `DATA_PROVIDER` 仍最高优先。`data_mode.is_local_daily_mode()` 改为 `get_active_provider_name("daily") == "fquant_local"`；任务 15 realtime 链判定用 `get_active_provider_name("realtime")`。（当前 `set_data_provider` 会把 per-capability 同步为同值，行为短期等价，但接线后支持混配。）

**A3（改任务 14/15）realtime 稳定性 + 统一出口**
- `SinaTencentClient` 增加连续失败退避：同源连续失败 ≥3 次后冷却 60s 内直接跳过该源（进程内计数即可）；批次部分失败保留成功行。
- `FQuantProvider.get_realtime` 的最终输出统一过 `normalizer.normalize_realtime()`（任务 13 的显式契约），tdx-api/sina/tencent/fstore 四路行都走同一出口。
- 真实响应样本抓取后固化进测试 fixture（任务 14 校准步骤的产物要落在测试文件注释里）。
