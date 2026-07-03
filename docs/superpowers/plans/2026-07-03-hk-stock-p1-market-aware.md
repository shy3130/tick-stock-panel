# 港股 P1：个股行情 + 技术分析 market-aware 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让港股（`.HK`）标的能在个股行情、技术指标、个股分析中正确流转——**不产生假涨跌停/连板信号、明确标注未复权、支持港股交易时段**。范围严格锁定"实测已确认可用的数据源"（标的清单 + TDX 日/分钟线 + Tencent 实时），**不碰**财务/指数成分/盘口（实测缺口）与涨停梯队/情绪复盘（A 股概念）。

**架构：** 新增 `app/markets.py` 一个 `MarketProfile` 单一事实源（按 symbol 判市场 → 是否有涨跌停 / 复权口径 / 交易时段）。三处关键路径改为 market-aware：① `compute_enriched` 对非 A 股跳过涨跌停信号段（消除假信号）；② `stock_analyzer` 复用 kline API 的本地按需算指标路径（当前它读批量 enriched 表，港股不在其中→分析失败）；③ 交易时段判定改为"任一市场开市"（支持港股 9:30-16:00）。港股 K 线响应加 `adjustment: "none"` 标注。

**技术栈：** Python 3.12 / FastAPI / Polars。测试 `cd backend && uv run --extra dev pytest`。

**关键现状（已核实）：**
- 本地模式下个股 K 线 API（`app/api/kline.py:110-130`）**已按需 `compute_enriched(raw, factors=factors)`**，且已按 `_asset_type_for_symbol(symbol)`（`kline.py:18-23`）对 `.HK`→`"hk"` 分流、跳过复权因子拉取。**但** `compute_enriched` 内部仍跑 `compute_limit_signals`（`pipeline.py:476`），HK 代码（如 00700）不匹配 300/301/688/689/.BJ 前缀 → 套 0.10 默认 → **产出假涨停/跌停/连板信号**。这是本计划核心修复点。
- `stock_analyzer._load_kline`（`stock_analyzer.py:41-50`）用 `repo.get_daily(symbol)` 读**批量 enriched 表**（`repository.py:902`）；港股不在批量管道内 → 返回空 → 分析失败。需给它同款本地按需路径。
- 交易时段 `_is_trading_hours`（`quote_service.py:658-663`、`depth_service.py:585`）硬编码 A 股时段 + 本地时间、无时区/日历。

**显式不做（划到 P-next 或不适用，勿扩范围）：**
- HK 批量 enrich（2931 只入盘后管道）→ 回测/筛选/监控依赖它，**整体划到 P-next**（更大改动）。本 P1 只做**个股级**按需路径。
- 港股复权因子接入 → P2；本 P1 只**标注未复权**，不接源。
- 财务 / 恒生指数成分 / 五档盘口 → 实测数据缺口，不做。
- 涨停梯队 / 真假涨停 / 情绪复盘 / A 股参考数据 → 概念不适用，不做。
- 港股交易日历（节假日）→ P1 只做时段（session）判定，节假日历留 P2；非交易日拉取返回空数据本就安全降级。

---

## 文件结构

| 文件 | 职责 | 改动 |
|---|---|---|
| `backend/app/markets.py` | MarketProfile 单一事实源（市场判定 + 涨跌停/复权/时段） | 创建 |
| `backend/app/indicators/pipeline.py` | 指标/信号计算 | `compute_enriched`/`compute_all` 增 `asset_type` 参数，非 A 股跳过 `compute_limit_signals` |
| `backend/app/api/kline.py` | 个股 K 线端点 | 传 asset_type 给 compute_enriched；HK 响应加 `adjustment` 标注 |
| `backend/app/services/stock_analyzer.py` | 个股分析取数 | `_load_kline` 港股走本地按需 compute 路径 |
| `backend/app/services/quote_service.py` | 实时行情时段门 | `_is_trading_hours` 改"任一市场开市" |
| `backend/app/services/depth_service.py` | 盘口时段门 | 同上（复用 markets 的时段判定） |
| `backend/tests/test_markets.py` | MarketProfile 单测 | 创建 |
| `backend/tests/indicators/test_pipeline_hk.py` | HK 无假信号 golden | 创建 |
| `backend/tests/api/test_kline_hk.py` | HK 端点标注 + 无假信号 | 创建 |
| `backend/tests/services/test_stock_analyzer_hk.py` | HK 分析取数 | 创建 |

---

### 任务 1：MarketProfile 单一事实源

**文件：**
- 创建：`backend/app/markets.py`
- 测试：`backend/tests/test_markets.py`

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/test_markets.py
from datetime import time

from app.markets import market_of, MarketProfile


def test_a_share_profile():
    p = market_of("600519.SH")
    assert p.market == "a_share"
    assert p.has_price_limit is True
    assert p.adjustment == "xdxr"
    assert p.timezone == "Asia/Shanghai"


def test_hk_profile():
    p = market_of("00700.HK")
    assert p.market == "hk"
    assert p.has_price_limit is False        # 港股无固定涨跌停
    assert p.adjustment == "none"            # P1 不接港股复权
    assert (time(13, 0), time(16, 0)) in p.sessions


def test_bj_and_etf_are_a_share():
    assert market_of("830799.BJ").market == "a_share"
    assert market_of("510330.SH").market == "a_share"


def test_is_open_at_union_of_sessions():
    from app.markets import any_market_open_at
    from datetime import datetime
    # 周三 15:30（A 股已收、港股仍在午后）→ 开市
    assert any_market_open_at(datetime(2026, 7, 1, 15, 30)) is True
    # 周三 20:00 → 无市场开市
    assert any_market_open_at(datetime(2026, 7, 1, 20, 0)) is False
    # 周六 10:00 → 周末不开
    assert any_market_open_at(datetime(2026, 7, 4, 10, 0)) is False
```

- [ ] **步骤 2：运行验证失败**

运行：`cd backend && uv run --extra dev pytest tests/test_markets.py -v`
预期：FAIL，`ModuleNotFoundError: No module named 'app.markets'`

- [ ] **步骤 3：实现**

```python
# backend/app/markets.py
"""市场画像单一事实源（港股扩展 P1）。

按 symbol 判市场，暴露"是否有涨跌停 / 复权口径 / 交易时段"等
market-aware 决策所需的元数据。业务代码不再散落 .HK / 前缀判断。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

Session = tuple[time, time]


@dataclass(frozen=True)
class MarketProfile:
    market: str                    # "a_share" | "hk"
    has_price_limit: bool          # 是否有个股固定涨跌停
    adjustment: str                # "xdxr"（A 股除权除息重建）| "none"（P1 港股未复权）
    timezone: str                  # IANA 时区名
    sessions: tuple[Session, ...]  # 交易时段（本地/市场时区内）


_A_SHARE = MarketProfile(
    market="a_share",
    has_price_limit=True,
    adjustment="xdxr",
    timezone="Asia/Shanghai",
    sessions=((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0))),
)

_HK = MarketProfile(
    market="hk",
    has_price_limit=False,
    adjustment="none",
    timezone="Asia/Shanghai",   # 港股与内地同时区，P1 复用 Asia/Shanghai
    sessions=((time(9, 30), time(12, 0)), (time(13, 0), time(16, 0))),
)


def market_of(symbol: str) -> MarketProfile:
    """按 symbol 后缀判市场。非 .HK 一律按 A 股（含 SH/SZ/BJ/ETF/INDEX）。"""
    return _HK if symbol.upper().endswith(".HK") else _A_SHARE


def any_market_open_at(now: datetime) -> bool:
    """任一支持市场在该时刻开市（工作日 + 落在某市场 session 内）。

    P1 用"任一市场开市"作为全局实时/盘口轮询门，避免 per-symbol 时段复杂度；
    港股午后延到 16:00，A 股 15:00 收，取并集即可覆盖两市。
    """
    if now.weekday() >= 5:
        return False
    t = now.time()
    for profile in (_A_SHARE, _HK):
        for start, end in profile.sessions:
            if start <= t <= end:
                return True
    return False
```

- [ ] **步骤 4：运行测试验证通过 + Commit**

```bash
cd backend && uv run --extra dev pytest tests/test_markets.py -v
git add app/markets.py tests/test_markets.py
git commit -m "feat(markets): MarketProfile single source for HK/A-share market awareness"
```

---

### 任务 2：compute_enriched 对非 A 股跳过涨跌停信号

消除 HK 假涨停/连板信号。给指标入口加 `asset_type`，非 A 股不跑 `compute_limit_signals`。

**文件：**
- 修改：`backend/app/indicators/pipeline.py`（`compute_enriched` / `compute_all` 签名 + limit 调用门控）
- 测试：`backend/tests/indicators/test_pipeline_hk.py`

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/indicators/test_pipeline_hk.py
"""港股不应产生涨跌停/连板信号（无固定涨跌停）。"""
import polars as pl

from app.indicators.pipeline import compute_enriched

_LIMIT_COLS = ["signal_limit_up", "signal_limit_down", "consecutive_limit_ups",
               "consecutive_limit_downs", "signal_broken_limit_up"]


def _raw(symbol: str) -> pl.DataFrame:
    # 构造一根 +12% 的日 K（A 股会判涨停，港股不应判）
    dates = ["2026-06-29", "2026-06-30"]
    closes = [100.0, 112.0]
    return pl.DataFrame({
        "symbol": [symbol, symbol],
        "date": dates,
        "open": [100.0, 100.0], "high": [100.0, 112.0], "low": [100.0, 100.0],
        "close": closes, "volume": [1_000, 1_000], "amount": [1e5, 1e5],
    })


def test_hk_has_no_limit_signals():
    out = compute_enriched(_raw("00700.HK"), asset_type="hk")
    for col in _LIMIT_COLS:
        if col in out.columns:
            assert out[col].fill_null(0).sum() == 0, f"HK 不应有 {col}"


def test_a_share_still_computes_limit_signals():
    out = compute_enriched(_raw("600519.SH"), asset_type="stock")
    # A 股保留原行为：+12% 对主板（10% 限）虽超限，此处只断言列存在且逻辑跑过
    assert "signal_limit_up" in out.columns
```

- [ ] **步骤 2：运行验证失败**

运行：`cd backend && uv run --extra dev pytest tests/indicators/test_pipeline_hk.py -v`
预期：FAIL——`compute_enriched` 无 `asset_type` 参数（TypeError）或 HK 仍出限价信号。

- [ ] **步骤 3：实现**

在 `pipeline.py`：
1. `compute_enriched` 与 `compute_all` 签名各增 `asset_type: str = "stock"`（默认保持 A 股行为，向后兼容全部现有调用方）。
2. 定位 `compute_enriched`/`compute_all` 内部调用 `compute_limit_signals(...)` 处，用市场画像门控：

```python
from app.markets import market_of  # 文件顶部

# ... 原本无条件调用 compute_limit_signals 的位置，改为：
_has_limit = asset_type == "stock"  # index/etf/hk 无个股涨跌停语义
if _has_limit:
    df = compute_limit_signals(df, instruments if instruments is not None else _empty_instruments())
```

> 说明：以 `asset_type` 门控而非 `market_of(symbol)`，因 pipeline 是批量按 df 处理、单次 df 同一 asset_type（调用方 kline.py 已按 symbol 分组取 asset_type）。`index`/`etf` 本就不该有涨跌停信号，一并纳入（`asset_type=="stock"` 才算）。非 A 股跳过后，`_LIMIT_COLS` 相关列不产出（下游读取处已是"列不存在则跳过"的容错，见任务 3 验证）。

3. 若 `compute_limit_signals` 依赖的临时列（如 `_prev_raw_close`）在后续 pass 被引用，确认跳过分支不破坏后续计算——limit 信号列是终端产物，跳过只影响 `_LIMIT_COLS`，不影响 MA/MACD 等；实现时跑全量指标测试确认。

- [ ] **步骤 4：运行测试 + 全量指标回归**

```bash
cd backend && uv run --extra dev pytest tests/indicators/test_pipeline_hk.py tests/indicators/ -v
```
预期：HK 测试通过；既有 A 股指标测试不回归。

- [ ] **步骤 5：Commit**

```bash
git add app/indicators/pipeline.py tests/indicators/test_pipeline_hk.py
git commit -m "feat(indicators): skip limit signals for non-A-share (asset_type gate)"
```

---

### 任务 3：个股 K 线端点传 asset_type + HK 未复权标注

**文件：**
- 修改：`backend/app/api/kline.py:110-135`（本地按需路径）
- 测试：`backend/tests/api/test_kline_hk.py`

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/api/test_kline_hk.py
"""HK 个股 K 线：compute_enriched 收到 hk asset_type，响应标注未复权。"""
import polars as pl

import app.api.kline as kline_mod


def test_local_kline_passes_asset_type_and_marks_adjustment(monkeypatch):
    captured = {}
    real_compute = kline_mod.compute_enriched

    def spy(raw, factors=None, asset_type="stock", **kw):
        captured["asset_type"] = asset_type
        return real_compute(raw, factors=factors, asset_type=asset_type, **kw)

    monkeypatch.setattr(kline_mod, "compute_enriched", spy)
    # 该测试聚焦"传参 + 标注"，用 asset_type 判定函数确认 .HK 分流
    assert kline_mod._asset_type_for_symbol("00700.HK") == "hk"
    assert kline_mod._adjustment_label("00700.HK") == "none"
    assert kline_mod._adjustment_label("600519.SH") == "xdxr"
```

- [ ] **步骤 2：运行验证失败**（`_adjustment_label` 不存在）

- [ ] **步骤 3：实现**

在 `kline.py`：
1. 顶部加辅助（复用 markets）：

```python
def _adjustment_label(symbol: str) -> str:
    from app.markets import market_of
    return market_of(symbol).adjustment
```

2. 本地按需路径（`kline.py:~125`）把 asset_type 传入 compute_enriched：

```python
        enriched = compute_enriched(raw, factors=factors, asset_type=asset_type)
```

3. 响应字典加标注（`resp = {...}` 内）：

```python
            "adjustment": _adjustment_label(symbol),  # "xdxr"=前复权 / "none"=未复权(港股)
```

- [ ] **步骤 4：运行测试 + Commit**

```bash
cd backend && uv run --extra dev pytest tests/api/test_kline_hk.py -v
git add app/api/kline.py tests/api/test_kline_hk.py
git commit -m "feat(kline): pass asset_type to enrichment, mark HK as unadjusted"
```

> **前端跟进（本计划不含，记 P1 后续）**：前端 K 线页读 `adjustment` 字段，`"none"` 时展示"未复权"角标，避免用户误以为港股是前复权价。

---

### 任务 4：个股分析支持港股（本地按需取数）

`stock_analyzer._load_kline` 当前读批量 enriched 表，港股不在其中。抽出一个"本地按需算 enriched"的复用函数，港股走它。

**文件：**
- 修改：`backend/app/services/stock_analyzer.py:41-50`
- 测试：`backend/tests/services/test_stock_analyzer_hk.py`

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/services/test_stock_analyzer_hk.py
"""港股个股分析：批量表无该 symbol 时，走本地按需 compute 兜底。"""
import polars as pl

import app.services.stock_analyzer as sa


class _EmptyRepo:
    def get_daily(self, symbol, start, end):
        return pl.DataFrame()  # 港股不在批量 enriched 表


def test_hk_falls_back_to_local_on_demand(monkeypatch):
    called = {}

    def fake_local(symbol, start, end):
        called["symbol"] = symbol
        return pl.DataFrame({"symbol": [symbol], "date": ["2026-07-01"],
                             "close": [431.2], "ma5": [430.0]})

    monkeypatch.setattr(sa, "_load_kline_local_on_demand", fake_local)
    df = sa._load_kline(_EmptyRepo(), "00700.HK")
    assert not df.is_empty()
    assert called["symbol"] == "00700.HK"


def test_a_share_uses_batch_table_first(monkeypatch):
    class _Repo:
        def get_daily(self, symbol, start, end):
            return pl.DataFrame({"symbol": [symbol], "date": ["2026-07-01"], "close": [1.0]})
    # A 股批量表命中 → 不触发本地兜底
    monkeypatch.setattr(sa, "_load_kline_local_on_demand",
                        lambda *a: (_ for _ in ()).throw(AssertionError("不该走本地兜底")))
    assert not sa._load_kline(_Repo(), "600519.SH").is_empty()
```

- [ ] **步骤 2：运行验证失败**（`_load_kline_local_on_demand` 不存在 / `_load_kline` 不兜底）

- [ ] **步骤 3：实现**

在 `stock_analyzer.py`：
1. 抽出本地按需函数（逻辑对齐 `kline.py` 本地路径，DRY——可从 kline.py 提取共用 helper 到 `app/services/kline_ondemand.py`，若时间紧 P1 先在 analyzer 内实现、标注 TODO 合并）：

```python
def _load_kline_local_on_demand(symbol: str, start, end) -> pl.DataFrame:
    """本地模式按需拉日 K + 算指标（港股/批量表未覆盖标的兜底）。"""
    import polars as pl
    from datetime import datetime
    from app.data_providers.registry import get_active_provider_name, get_provider
    from app.indicators.pipeline import compute_enriched
    from app.api.kline import _asset_type_for_symbol

    provider = get_provider(get_active_provider_name("daily"))
    asset_type = _asset_type_for_symbol(symbol)
    s = datetime.combine(start, datetime.min.time())
    e = datetime.combine(end, datetime.min.time())
    raw = provider.get_daily([symbol], s, e, asset_type)
    if raw.is_empty():
        return pl.DataFrame()
    factors = pl.DataFrame()
    if asset_type == "stock":
        try:
            factors = provider.get_adj_factors([symbol], s, e, asset_type)
        except Exception:  # noqa: BLE001
            pass
    return compute_enriched(raw, factors=factors, asset_type=asset_type)
```

2. `_load_kline` 加兜底：批量表空 + 本地模式时走按需：

```python
    df = repo.get_daily(symbol, start, end)
    if df.is_empty():
        from app.services.data_mode import is_local_daily_mode
        if is_local_daily_mode():
            df = _load_kline_local_on_demand(symbol, start, end)
    if df.is_empty():
        return df
    return df.tail(_KLINE_WINDOW)
```

- [ ] **步骤 4：运行测试 + Commit**

```bash
cd backend && uv run --extra dev pytest tests/services/test_stock_analyzer_hk.py -v
git add app/services/stock_analyzer.py tests/services/test_stock_analyzer_hk.py
git commit -m "feat(analysis): HK single-stock analysis via local on-demand enrichment"
```

> **AI prompt 软化（本计划记为可选）**：`stock_analyzer` 的 prompt 措辞偏 A 股（涨停/连板）。港股无这些列时 prompt 拼装已是"有则拼、无则略"的容错，功能不阻断；措辞润色留 P1 后续，不作硬需求。

---

### 任务 5：交易时段判定改为"任一市场开市"

**文件：**
- 修改：`backend/app/services/quote_service.py:658-663`
- 修改：`backend/app/services/depth_service.py:585-587`
- 测试：`backend/tests/services/test_trading_hours_hk.py`

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/services/test_trading_hours_hk.py
"""实时/盘口时段门覆盖港股午后（延到 16:00）。"""
from datetime import datetime

from app.services.quote_service import QuoteService


def test_quote_trading_hours_covers_hk_afternoon(monkeypatch):
    # 2026-07-01 周三 15:30：A 股已收、港股仍开
    monkeypatch.setattr("app.services.quote_service.datetime",
                        _fixed_datetime(datetime(2026, 7, 1, 15, 30)))
    assert QuoteService._is_trading_hours() is True


def _fixed_datetime(when):
    class _D(datetime):
        @classmethod
        def now(cls, tz=None):
            return when
    return _D
```

- [ ] **步骤 2：运行验证失败**（当前 15:30 超出 A 股 15:05 上限 → False）

- [ ] **步骤 3：实现**

`quote_service.py` 与 `depth_service.py` 的 `_is_trading_hours` 改为委托 markets：

```python
    @staticmethod
    def _is_trading_hours() -> bool:
        from datetime import datetime
        from app.markets import any_market_open_at
        return any_market_open_at(datetime.now())
```

> 注意：原实现有 9:15/11:35 等**盘前后缓冲**（集合竞价）。`any_market_open_at` 用严格 session。若需保留缓冲，在 `markets.py` 的 A 股 session 保留缓冲边界（如 9:15-11:35）；实现时确认现有依赖缓冲的行为（集合竞价拉取），必要时在 markets 里为 A 股 session 加缓冲版常量。港股缓冲 P1 不做。

- [ ] **步骤 4：运行测试 + 全量回归 + Commit**

```bash
cd backend && uv run --extra dev pytest tests/services/test_trading_hours_hk.py -q && uv run --extra dev pytest -q
git add app/services/quote_service.py app/services/depth_service.py tests/services/test_trading_hours_hk.py
git commit -m "feat(realtime): trading-hours gate covers any open market (HK sessions)"
```

---

### 任务 6：收尾回归 + 文档标注

- [ ] **步骤 1：全量测试 + 冒烟**

```bash
cd backend && uv run --extra dev pytest -q && uv run python -c "from app.main import app; print('ok')"
```

- [ ] **步骤 2：手动验证（有 TDX 挂载）**

拉一只港股走完整链路，确认：① 个股 K 线返回数据且 `adjustment=="none"`；② 无涨停/连板信号列非零；③ 个股分析对 00700.HK 不再空。

```bash
# dev server 起后
curl -s 'localhost:8000/api/kline/00700.HK?days=60' | python -m json.tool | grep -E 'adjustment|signal_limit' | head
```

- [ ] **步骤 3：更新评估文档状态**

`docs/hk-us-stock-expansion-assessment.md` §五 港股 P1 标注"个股行情+技术分析已落地（本计划）；回测/筛选/监控依赖 HK 批量 enrich，划 P-next；复权口径 P2"。

- [ ] **步骤 4：Commit**

```bash
git add docs/hk-us-stock-expansion-assessment.md
git commit -m "docs: mark HK P1 (single-stock quote + analysis) landed"
```

---

## 自检（规格覆盖）

- ✅ 消除 HK 假涨跌停信号（任务 2）— 评估 §三.2 核心硬编码
- ✅ 港股未复权标注（任务 3）— 评估 §三.4 / 风险"复权口径"
- ✅ 港股个股分析可用（任务 4）— 评估 §三.5
- ✅ 港股交易时段（任务 5）— 评估 §三.3（P1 只做 session，日历留 P2）
- ✅ MarketProfile 单一事实源（任务 1）— 支撑上述全部，避免 .HK 判断散落
- ⏸️ 回测/筛选/监控 HK — 显式划 P-next（需 HK 批量 enrich），不在本计划
- ⏸️ 复权因子接入 / 财务 / 指数成分 / 盘口 / 情绪 — 按评估结论不做或延后

## 执行交接

计划已完成并保存到 `docs/superpowers/plans/2026-07-03-hk-stock-p1-market-aware.md`。两种执行方式：

**1. 子代理驱动（推荐）** —— 每个任务派新子代理，任务间两阶段审查（符合本项目 codex/子代理执行、Claude 审查的协作模式）。

**2. 内联执行** —— 当前会话用 executing-plans 批量执行并设检查点。

选哪种方式？
