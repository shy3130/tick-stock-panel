# Trade Journal MVP 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

## 执行状态（2026-07-03 Codex）

- 已完成：后端 `trade_journal` 服务层、`/api/journal/*` API、oracle 对拍脚本、宽基回填脚本、前端 `/journal` 页面、导航入口、API 类型契约。
- 已验证：`cd backend && uv run --extra dev pytest -q` → `125 passed`；scoped Ruff → passed；`cd frontend && pnpm build` → passed（仅既有 Vite chunk 警告）。
- 真实样本：`~/Downloads/银河.xlsx` oracle 对拍通过，`fills=1881 events=328 trips=446 oracle=446 open=5 missing=0 extra=0 pnl_diff=0 whitelisted=1`。
- 临时 HTTP E2E：使用 `DATA_DIR=/tmp/tickflow-journal-e2e` 上传真实 `银河.xlsx`，预览 `row_count=2209`，commit 后 `trips=446`、`open=5`、`benchmark=沪深300`、`benchmark_return=0.8473429951690821`、`excess=-0.8500780204822614`、`chasing_covered=8`、`warnings=0`，临时目录只生成 `ledger.json`。
- 已授权后执行：任务 0 真实宽基回填完成，`回填完成: +9950 行`，常驻指数为 `000300.SH,000688.SH,000905.SH,399006.SZ`，`000300.SH` 2024 年校验 `rows=242`。
- 未执行：各任务拆分 commit 步骤；本次改为一个收口 commit。

**现状证据：**
- 真实样本 `~/Downloads/银河.xlsx` 已确认 3 个 sheet：持仓数据、已清仓、交易记录；事实源采用交易记录逐笔成交，已清仓只作 oracle 对拍。
- FIFO/position-cycle 口径已用 oracle 验证：`trips=446 oracle=446 missing=0 extra=0 pnl_diff=0`，说明计划里的配对和同花顺已清仓 sheet 可逐笔对上。
- 基准数据已通过本地 `kline_index_daily` 回填验证，四个宽基指数可供 benchmark lookup 使用。
- Trade Journal 与 Shadow Account 已拆分：当前只持久化报告/台账 payload，不把影子回放规则抽取塞进 MVP。

**目标：** 用户上传券商成交流水（同花顺投资账本 xlsx / 通用 CSV）→ FIFO 配对成 roundtrip 台账 → 纯统计行为诊断 + 基准超额报告。

**架构：** 独立新模块 `backend/app/services/trade_journal/`（parser/presets/fifo/diagnose/benchmark/store 六个纯函数文件 + models），IO 只发生在 API 层；前端新增 `TradeJournal.tsx` 页面。**不碰** `backtest/engine.py`（那是延后的 Shadow Account 的事，见 CONTEXT.md「交易复盘领域」与迁移文档 R1-R6）。

**技术栈：** Python 3.12 / FastAPI / Polars（`pl.read_excel` 走已有依赖 fastexcel）/ dataclasses；前端 React+TS。测试 `uv run --extra dev pytest`。

**设计红线（来自 grilling 决策，不得违反）：**
1. 行为诊断纯统计，**不调用任何 LLM**（R3）。
2. 上传的原始 xlsx/CSV **解析完即弃**，只持久化归一化台账（R5）。
3. 事实源 = 「交易记录」sheet 的逐笔成交；「已清仓」sheet 只作对拍 oracle，且真实流水**不进 repo**——oracle 对拍是本地脚本，测试用合成 fixture（R4）。
4. 港股回合不出基准超额（恒指数据未确认），只出绝对盈亏（R6）。

**已用真实样本（银河.xlsx）验证的口径事实（实现必须对齐）：**
- 同花顺「总盈亏」= Σ卖出发生金额 + Σ买入发生金额（发生金额买入为负、含费，即纯现金差）。验证：赛力斯 12334.48 − 11221.23 = 1113.25 ✓
- 「买入/卖出均价」= |发生金额合计| / 数量（含费）。验证：11221.23/200 = 56.11 ✓
- 「持仓天数」= 交易日历下含两端的交易日数。验证：张江高科 2024-03-01→03-25 = 17 ✓
- 「交易记录」sheet 的 `交易类别` 实测 10 种：`买入` `卖出`（→成交）；`银行转证券` `证券转银行`（→转账）；`融券回购` `融券购回` `通用回购逆回购` `通用回购逆回购购回`（→现金管理）；`除权除息`（现金分红，正金额、数量空）；`股息个税征收`（负金额）。
- 代码列 A/HK 混合：5 位前导零（02577/06088）= 港股；6 位 = A 股。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| 创建 `backend/app/services/trade_journal/__init__.py` | 空包标记 |
| 创建 `backend/app/services/trade_journal/models.py` | `Fill` / `CashEvent` / `Roundtrip` / `LedgerSummary` dataclass 契约 |
| 创建 `backend/app/services/trade_journal/presets.py` | 列映射结构 + 同花顺投资账本预设 + 映射猜测 |
| 创建 `backend/app/services/trade_journal/parser.py` | xlsx/CSV → 归一化 `Fill`/`CashEvent` 列表（含行分类、符号归一） |
| 创建 `backend/app/services/trade_journal/fifo.py` | lot 级 FIFO 配对 + position-cycle 聚合成 `Roundtrip` |
| 创建 `backend/app/services/trade_journal/diagnose.py` | 四项行为偏差纯统计 |
| 创建 `backend/app/services/trade_journal/pricepos.py` | 追涨诊断行情分位（读 kline_daily 算 pos_20d） |
| 创建 `backend/app/services/trade_journal/benchmark.py` | 账户-区间超额（主）+ 逐笔超额（辅），可选基准 |
| 创建 `backend/app/services/trade_journal/store.py` | 台账持久化 `data/user_data/trade_journal/ledger.json` |
| 创建 `backend/app/api/trade_journal.py` | 上传/预览/入账/查询/删除 端点（唯一 IO 层） |
| 修改 `backend/app/main.py`（≈第 270 行 include_router 区）| 注册 router |
| 创建 `backend/scripts/backfill_broad_benchmarks.py` | 任务 0：宽基指数历史回填（基准超额前置，High 3） |
| 创建 `backend/scripts/validate_trade_journal_oracle.py` | 本地 oracle 对拍脚本（吃真实 xlsx，比对已清仓 sheet） |
| 创建 `backend/tests/services/trade_journal/test_presets.py` 等 | 每模块一个测试文件 |
| 修改 `backend/pyproject.toml` dev extra | 加 `xlsxwriter`（测试造 xlsx fixture 用） |
| 修改 `frontend/src/lib/api.ts` | journal API 客户端 + 类型 |
| 创建 `frontend/src/pages/TradeJournal.tsx` | 上传/映射预览/台账/诊断报告 页面 |
| 修改 `frontend/src/router.tsx`（第 11/79 行附近）| 新增 `journal` 路由（**不**动 trading 路由）|

---

### 任务 0：宽基指数历史回填（High 3 前置，阻塞基准超额）

**背景（core-review 确认）：** 本地 `kline_index_enriched`/`kline_index_daily` 里四个宽基基准（000300.SH/000905.SH/399006.SZ/000688.SH）**全区间只有 2026-07-02 一天**（历史分区都是 RPS `.INDEX` 概念指数）。但**上游 TDX 磁盘有完整历史**（`/Volumes/vol3/tdx/day/sh000/sh000300.csv` 5220 行、2005→2026；399006 在 `sz399` 桶）。fquant_local 正读这块盘。基准超额（benchmark.py）依赖这些历史 close，故须先回填。

**文件：**
- 创建：`backend/scripts/backfill_broad_benchmarks.py`
- 修改：无（复用 `index_sync.sync_and_persist_index_daily` + `preferences` 常驻指数列表）

**目标：** 一次性把四个宽基 2015-01-01→今回填到 `kline_index_daily` + `kline_index_enriched`，并加入常驻 index instruments 让盘后 pipeline 自动保鲜。

- [ ] **步骤 1：smoke-fetch 确认 provider 能取到宽基历史（de-risk）**

运行：
```bash
cd backend && uv run python -c "
from app.services import kline_sync
from datetime import datetime
df = kline_sync.sync_daily_batch(['000300.SH'], start_time=datetime(2024,1,1), end_time=datetime(2024,3,1), asset_type='index')
print('rows=', df.height, 'range=', df['date'].min(), df['date'].max() if not df.is_empty() else None)
"
```
预期：`rows` 远大于 1（约 38 个交易日），range 覆盖 2024-01~03。**若 rows≤1 或空**：说明 provider 的 index 磁盘映射有问题（检查 `engine_data_disk._tdx_name` 对 `000300.SH→sh000300` 的桶路径、`fquant_provider` 的 index asset_type 分支），先修通再继续，不要盲目全量。

- [ ] **步骤 2：编写回填脚本**

```python
# backend/scripts/backfill_broad_benchmarks.py
"""一次性回填宽基基准历史日K + 加入常驻指数列表 (High 3 前置)。

用法: cd backend && uv run python scripts/backfill_broad_benchmarks.py
上游: fquant_local 读 TDX 磁盘 (day/sh000/sh000300.csv 等), 历史 2005 起。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BROAD = ["000300.SH", "000905.SH", "399006.SZ", "000688.SH"]


def main() -> int:
    from app.main import app  # 复用已初始化的 repo/capset
    repo = app.state.repo
    capset = app.state.capabilities

    from app.services import index_sync, preferences

    n = index_sync.sync_and_persist_index_daily(
        repo, capset,
        symbols_override=BROAD,
        start_date=datetime(2015, 1, 1),
        end_date=datetime.now(),
    )
    print(f"回填完成: +{n} 行")

    # 加入常驻 index 列表, 让盘后 pipeline 自动保鲜
    current = set(preferences.get_pipeline_index_symbols() or [])
    merged = sorted(current | set(BROAD))
    preferences.set_pipeline_index_symbols(merged)
    print(f"常驻指数已更新: {merged}")

    # 校验: 000300.SH 区间可用
    df = repo.get_index_daily("000300.SH", datetime(2024, 1, 1).date(), datetime(2024, 12, 31).date(),
                              columns=["date", "close"])
    print(f"校验 000300.SH 2024 年: rows={df.height} (预期 ~242)")
    return 0 if df.height > 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

**注意（实现者）：** `preferences.set_pipeline_index_symbols` / `get_pipeline_index_symbols` 的确切函数名以 `backend/app/services/preferences.py` 为准（前面 review 见过 `get_pipeline_index_symbols`），若命名不同按实际改；若常驻列表机制不同（例如走 index instruments 表），改为对应的 `sync_index_instruments` + 持久化方式。回填是一次性运维脚本，不进 pytest。

- [ ] **步骤 3：执行回填并校验**

运行：`cd backend && uv run python scripts/backfill_broad_benchmarks.py`
预期：`+N 行`（四符号 × ~2500 交易日 ≈ 1 万行量级），末行 `校验 000300.SH 2024 年: rows≈242`。

- [ ] **步骤 4：Commit**

```bash
git add backend/scripts/backfill_broad_benchmarks.py
git commit -m "feat(journal): 宽基指数历史回填脚本 (基准超额前置)"
```

---

### 任务 1：数据模型 models.py

**文件：**
- 创建：`backend/app/services/trade_journal/__init__.py`（空文件）
- 创建：`backend/app/services/trade_journal/models.py`
- 测试：`backend/tests/services/trade_journal/__init__.py`（空）、`backend/tests/services/trade_journal/test_models.py`

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/services/trade_journal/test_models.py
from app.services.trade_journal.models import CashEvent, Fill, Roundtrip


def test_fill_is_frozen_and_normalized_fields():
    f = Fill(date="2024-02-05", time="14:53:08", symbol="601127.SH", name="赛力斯",
             side="buy", qty=200.0, price=56.1, amount=-11221.23, fee=1.23)
    assert f.side == "buy"
    assert f.amount < 0  # 买入发生金额为负


def test_roundtrip_pnl_is_net_cash_diff():
    rt = Roundtrip(symbol="601127.SH", name="赛力斯",
                   open_date="2024-02-05", close_date="2024-02-06",
                   qty=200.0, buy_net=11221.23, sell_net=12334.48,
                   fees=8.75, dividend=0.0, holding_days=2)
    assert abs(rt.pnl - 1113.25) < 1e-9          # 纯现金差
    assert abs(rt.total_pnl - 1113.25) < 1e-9    # 无分红时二者相等
    assert abs(rt.buy_avg - 56.106115) < 1e-6    # buy_net/qty
    assert abs(rt.pnl_pct - 1113.25 / 11221.23) < 1e-9


def test_total_pnl_includes_dividend_matches_ths():
    """High 2: 同花顺总盈亏含分红。银河样本 江淮汽车 = 现金差 29.74 + 净分红 33.6 = 63.34。"""
    rt = Roundtrip(symbol="600418.SH", name="江淮汽车",
                   open_date="2024-07-08", close_date="2024-07-22",
                   qty=2000.0, buy_net=41984.62, sell_net=42014.36,
                   fees=30.26, dividend=33.6, holding_days=11)  # dividend = 42 - 8.4 税
    assert abs(rt.pnl - 29.74) < 1e-9            # 纯现金差
    assert abs(rt.total_pnl - 63.34) < 1e-9      # 同花顺已清仓口径


def test_cash_event_kinds():
    ev = CashEvent(date="2024-07-18", symbol="600418.SH", kind="dividend", amount=42.0)
    assert ev.kind in {"dividend", "dividend_tax", "transfer_in", "transfer_out", "repo", "other"}
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd backend && uv run --extra dev pytest tests/services/trade_journal/test_models.py -v`
预期：FAIL，`ModuleNotFoundError: app.services.trade_journal`

- [ ] **步骤 3：编写实现**

```python
# backend/app/services/trade_journal/models.py
"""Trade Journal 数据契约。

口径（已用银河.xlsx 真实样本验证, 见迁移文档 R4/计划头部）:
- Fill.amount = 券商「发生金额」原样: 买入负、卖出正、含费。
- Roundtrip.pnl = sell_net - buy_net (纯现金差, 不含 dividend)。
- Roundtrip.total_pnl = pnl + dividend (含分红/税, 同花顺「已清仓·总盈亏」口径, oracle 对拍用此)。
- buy_avg/sell_avg = 净额/数量 (含费, 同花顺「买入/卖出均价」口径)。
- holding_days = 交易日历下含两端的交易日数。
"""
from __future__ import annotations

from dataclasses import dataclass, field

CASH_KINDS = ("dividend", "dividend_tax", "transfer_in", "transfer_out", "repo", "other")


@dataclass(frozen=True)
class Fill:
    """归一化后的一笔成交。"""
    date: str      # ISO YYYY-MM-DD
    time: str      # HH:MM:SS, 可空
    symbol: str    # 601127.SH / 02577.HK
    name: str
    side: str      # "buy" | "sell"
    qty: float
    price: float   # 券商「成交价格」(不含费)
    amount: float  # 券商「发生金额」: 买负卖正、含费
    fee: float


@dataclass(frozen=True)
class CashEvent:
    """非成交现金事件（分红/税/转账/回购）。"""
    date: str
    symbol: str    # 转账/回购类为空串
    kind: str      # CASH_KINDS 之一
    amount: float  # 带符号


@dataclass
class Roundtrip:
    """一个持仓周期（仓位 0 → >0 → 0）。"""
    symbol: str
    name: str
    open_date: str
    close_date: str
    qty: float          # 累计买入股数
    buy_net: float      # Σ|买入发生金额|
    sell_net: float     # Σ卖出发生金额
    fees: float
    dividend: float     # 周期内现金分红 - 股息税（单列, 不并入 pnl）
    holding_days: int

    @property
    def pnl(self) -> float:
        """纯现金差(不含分红)。"""
        return self.sell_net - self.buy_net

    @property
    def total_pnl(self) -> float:
        """含分红/股息税的总盈亏——同花顺「已清仓·总盈亏」口径(oracle 对拍用此)。

        High 2 修正: 已用银河样本验证 江淮汽车 现金差 29.74 + 分红 42 - 税 8.4 = 63.34
        = 同花顺总盈亏。对拍/展示头条用 total_pnl, 纯现金差用 pnl。
        """
        return self.pnl + self.dividend

    @property
    def pnl_pct(self) -> float:
        return self.total_pnl / self.buy_net if self.buy_net else 0.0

    @property
    def buy_avg(self) -> float:
        return self.buy_net / self.qty if self.qty else 0.0

    @property
    def sell_avg(self) -> float:
        return self.sell_net / self.qty if self.qty else 0.0


@dataclass
class LedgerSummary:
    """台账汇总。"""
    total_trips: int = 0
    win_trips: int = 0
    total_pnl: float = 0.0
    total_dividend: float = 0.0
    total_fees: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0        # 盈利单平均盈利
    avg_loss: float = 0.0       # 亏损单平均亏损(负数)
    profit_factor: float = 0.0  # Σ盈利 / |Σ亏损|
    open_positions: list[dict] = field(default_factory=list)  # 未平仓(不含在 trips 内)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`cd backend && uv run --extra dev pytest tests/services/trade_journal/test_models.py -v`
预期：3 passed

- [ ] **步骤 5：Commit**

```bash
git add backend/app/services/trade_journal/ backend/tests/services/trade_journal/
git commit -m "feat(journal): Trade Journal 数据契约 models"
```

---

### 任务 2：列映射与同花顺预设 presets.py

**文件：**
- 创建：`backend/app/services/trade_journal/presets.py`
- 测试：`backend/tests/services/trade_journal/test_presets.py`

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/services/trade_journal/test_presets.py
from app.services.trade_journal.presets import (
    CANONICAL_FIELDS, THS_PRESET, guess_mapping,
)


def test_ths_preset_covers_required_fields():
    required = {"date", "code", "category", "qty", "price", "amount"}
    assert required <= set(THS_PRESET["mapping"].values())
    assert THS_PRESET["sheet"] == "交易记录"


def test_guess_mapping_exact_ths_columns():
    cols = ["成交日期", "成交时间", "代码", "名称", "交易类别",
            "成交数量", "成交价格", "发生金额", "成交金额", "费用", "备注"]
    m = guess_mapping(cols)
    assert m["成交日期"] == "date"
    assert m["交易类别"] == "category"
    assert m["发生金额"] == "amount"
    assert m["费用"] == "fee"
    assert "备注" not in m  # 不认识的列不猜


def test_guess_mapping_generic_variants():
    m = guess_mapping(["交易日期", "证券代码", "操作", "成交量", "成交均价", "发生金额"])
    assert m["交易日期"] == "date"
    assert m["证券代码"] == "code"
    assert m["操作"] == "category"
    assert m["成交量"] == "qty"
```

- [ ] **步骤 2：运行验证失败**

运行：`cd backend && uv run --extra dev pytest tests/services/trade_journal/test_presets.py -v`
预期：FAIL，`No module named 'app.services.trade_journal.presets'`

- [ ] **步骤 3：编写实现**

```python
# backend/app/services/trade_journal/presets.py
"""列映射: 通用映射为地基, 券商预设只是自动填好的映射表 (grilling Q4 方案 C)。

CANONICAL_FIELDS 是归一化目标字段; 用户(或预设)把源文件列名映射到这些字段。
"""
from __future__ import annotations

# 归一字段: date 必填; code/category/qty/price/amount 必填; time/name/fee 可选
CANONICAL_FIELDS = ("date", "time", "code", "name", "category", "qty", "price", "amount", "fee")

# 同花顺投资账本导出 (银河.xlsx 实测): 3-sheet xlsx, 事实源 = 交易记录 sheet
THS_PRESET = {
    "id": "ths_journal",
    "label": "同花顺投资账本",
    "sheet": "交易记录",
    "mapping": {
        "成交日期": "date",
        "成交时间": "time",
        "代码": "code",
        "名称": "name",
        "交易类别": "category",
        "成交数量": "qty",
        "成交价格": "price",
        "发生金额": "amount",
        "费用": "fee",
    },
}

PRESETS = [THS_PRESET]

# 列名猜测词典: 源列名(去空白) → 归一字段
_GUESS: dict[str, str] = {}
for _syns, _field in [
    (("成交日期", "交易日期", "日期", "委托日期"), "date"),
    (("成交时间", "时间", "委托时间"), "time"),
    (("代码", "证券代码", "股票代码"), "code"),
    (("名称", "证券名称", "股票名称"), "name"),
    (("交易类别", "操作", "业务名称", "买卖标志", "委托类别"), "category"),
    (("成交数量", "成交量", "数量", "成交股数"), "qty"),
    (("成交价格", "成交均价", "价格", "成交价"), "price"),
    (("发生金额", "清算金额", "资金发生数"), "amount"),
    (("费用", "手续费", "佣金"), "fee"),
]:
    for _s in _syns:
        _GUESS[_s] = _field


def guess_mapping(columns: list[str]) -> dict[str, str]:
    """按词典猜测 源列名→归一字段; 认不出的列不出现在结果里。"""
    out: dict[str, str] = {}
    used: set[str] = set()
    for col in columns:
        field = _GUESS.get(str(col).strip())
        if field and field not in used:
            out[str(col)] = field
            used.add(field)
    return out
```

- [ ] **步骤 4：运行验证通过**

运行：`cd backend && uv run --extra dev pytest tests/services/trade_journal/test_presets.py -v`
预期：3 passed

- [ ] **步骤 5：Commit**

```bash
git add backend/app/services/trade_journal/presets.py backend/tests/services/trade_journal/test_presets.py
git commit -m "feat(journal): 通用列映射 + 同花顺投资账本预设"
```

---

### 任务 3：解析与行分类 parser.py

**文件：**
- 创建：`backend/app/services/trade_journal/parser.py`
- 测试：`backend/tests/services/trade_journal/test_parser.py`
- 修改：`backend/pyproject.toml`（dev extra 加 `"xlsxwriter>=3.0"`，测试造 xlsx 用）

- [ ] **步骤 1：dev 依赖**

在 `backend/pyproject.toml` 的 `[project.optional-dependencies] dev` 列表加一行 `"xlsxwriter>=3.0",`，然后 `cd backend && uv sync --extra dev`。

- [ ] **步骤 2：编写失败的测试**

```python
# backend/tests/services/trade_journal/test_parser.py
import polars as pl
import pytest

from app.services.trade_journal.parser import (
    normalize_code, normalize_rows, read_upload,
)
from app.services.trade_journal.presets import THS_PRESET

THS_COLS = ["成交日期", "成交时间", "代码", "名称", "交易类别",
            "成交数量", "成交价格", "发生金额", "成交金额", "费用", "备注"]

ROWS = [
    # 非交易行: 银行转证券 (代码空、数量 0)
    ["2024-02-01", "", "", "", "银行转证券", 0, 0, 13000, None, None, ""],
    ["2024-02-05", "14:53:08", "601127", "赛力斯", "买入", 200, 56.1, -11221.23, 11220, 1.23, ""],
    ["2024-02-06", "14:54:53", "601127", "赛力斯", "卖出", 200, 61.71, 12334.48, 12342, 7.52, ""],
    # 港股: 5 位前导零
    ["2025-06-20", "10:00:00", "02577", "英诺赛科", "买入", 200, 72.7, -14542.6, 14540, 2.6, ""],
    # 现金分红与税
    ["2024-07-18", "16:00:00", "600418", "江淮汽车", "除权除息", None, None, 42, 42, None, ""],
    ["2024-07-23", "09:00:00", "600418", "江淮汽车", "股息个税征收", None, None, -8.4, None, None, ""],
    # 逆回购
    ["2024-03-05", "", "204001", "GC001", "融券回购", 0, 0, -50000, None, None, ""],
]


def _df():
    return pl.DataFrame([dict(zip(THS_COLS, r)) for r in ROWS])


def test_normalize_code():
    assert normalize_code("601127") == "601127.SH"
    assert normalize_code("000988") == "000988.SZ"
    assert normalize_code("300433") == "300433.SZ"
    assert normalize_code("688347") == "688347.SH"
    assert normalize_code("830799") == "830799.BJ"
    assert normalize_code("02577") == "02577.HK"   # 5 位前导零 = HK
    assert normalize_code("06088") == "06088.HK"


def test_normalize_rows_splits_fills_and_cash_events():
    fills, events, warnings = normalize_rows(_df(), THS_PRESET["mapping"])
    assert len(fills) == 3
    buy = fills[0]
    assert (buy.symbol, buy.side, buy.qty, buy.amount) == ("601127.SH", "buy", 200.0, -11221.23)
    assert fills[2].symbol == "02577.HK"
    kinds = sorted(ev.kind for ev in events)
    assert kinds == ["dividend", "dividend_tax", "repo", "transfer_in"]
    div = next(ev for ev in events if ev.kind == "dividend")
    assert (div.symbol, div.amount) == ("600418.SH", 42.0)
    assert warnings == []


def test_normalize_rows_warns_on_unknown_category():
    df = pl.DataFrame([dict(zip(THS_COLS,
        ["2024-01-01", "", "600000", "浦发银行", "担保品划入", 100, 8.0, -800, None, None, ""]))])
    fills, events, warnings = normalize_rows(df, THS_PRESET["mapping"])
    assert fills == [] and len(events) == 1 and events[0].kind == "other"
    assert len(warnings) == 1 and "担保品划入" in warnings[0]


def test_read_upload_xlsx_multi_sheet(tmp_path):
    import xlsxwriter
    path = tmp_path / "journal.xlsx"
    wb = xlsxwriter.Workbook(str(path))
    for sheet in ("持仓数据", "已清仓"):
        ws = wb.add_worksheet(sheet)
        ws.write_row(0, 0, ["代码", "名称"])
    ws = wb.add_worksheet("交易记录")
    ws.write_row(0, 0, THS_COLS)
    for i, r in enumerate(ROWS, start=1):
        ws.write_row(i, 0, ["" if v is None else v for v in r])
    wb.close()

    sheets, df = read_upload(path.read_bytes(), "journal.xlsx", sheet="交易记录")
    assert sheets == ["持仓数据", "已清仓", "交易记录"]
    assert df.height == len(ROWS)
    assert "成交日期" in df.columns


def test_read_upload_csv():
    csv = "成交日期,代码,交易类别,成交数量,成交价格,发生金额\n2024-02-05,601127,买入,200,56.1,-11221.23\n"
    sheets, df = read_upload(csv.encode("utf-8"), "a.csv", sheet=None)
    assert sheets == []
    assert df.height == 1
```

- [ ] **步骤 3：运行验证失败**

运行：`cd backend && uv run --extra dev pytest tests/services/trade_journal/test_parser.py -v`
预期：FAIL，import error

- [ ] **步骤 4：编写实现**

```python
# backend/app/services/trade_journal/parser.py
"""上传文件 → 归一化 Fill / CashEvent。

红线: 本模块只吃 bytes/DataFrame, 不落盘任何原始文件 (R5)。
交易类别 → 分类 (银河.xlsx 实测 10 种, 见计划头部)。
"""
from __future__ import annotations

import io
import logging

import polars as pl

from app.services.trade_journal.models import CashEvent, Fill

logger = logging.getLogger(__name__)

# 交易类别 → 处理方式
_BUY = {"买入", "证券买入", "担保买入"}
_SELL = {"卖出", "证券卖出", "担保卖出"}
_CASH_KIND = {
    "银行转证券": "transfer_in",
    "证券转银行": "transfer_out",
    "除权除息": "dividend",
    "股息个税征收": "dividend_tax",
    "融券回购": "repo",
    "融券购回": "repo",
    "通用回购逆回购": "repo",
    "通用回购逆回购购回": "repo",
}


def normalize_code(code: str) -> str:
    """A/HK 混合代码归一 (银河样本: 5 位前导零 = 港股)。"""
    code = str(code or "").strip()
    if "." in code:
        return code
    if len(code) == 5:
        return f"{code}.HK"
    if code.startswith(("6", "5", "9")):
        return f"{code}.SH"
    if code.startswith(("8", "4")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def read_upload(data: bytes, filename: str, sheet: str | None) -> tuple[list[str], pl.DataFrame]:
    """读上传文件 → (sheet 名列表, 选中 sheet 的 DataFrame)。CSV 无 sheet 概念。"""
    if filename.lower().endswith((".xlsx", ".xls")):
        frames = pl.read_excel(io.BytesIO(data), sheet_id=0, infer_schema_length=0)  # 全 sheet, 全字符串
        sheets = list(frames.keys())
        pick = sheet if sheet in frames else ("交易记录" if "交易记录" in frames else sheets[0])
        return sheets, frames[pick]
    return [], pl.read_csv(io.BytesIO(data), infer_schema_length=0)


def _f(v: object) -> float:
    try:
        s = str(v).strip().replace(",", "")
        return float(s) if s not in ("", "None", "-") else 0.0
    except (TypeError, ValueError):
        return 0.0


def normalize_rows(
    df: pl.DataFrame, mapping: dict[str, str],
) -> tuple[list[Fill], list[CashEvent], list[str]]:
    """按映射归一。返回 (成交, 现金事件, 警告)。"""
    fills: list[Fill] = []
    events: list[CashEvent] = []
    warnings: list[str] = []
    unknown_cats: set[str] = set()

    inv = {v: k for k, v in mapping.items()}  # 归一字段 → 源列名

    def col(row: dict, field: str) -> object:
        src = inv.get(field)
        return row.get(src) if src else None

    for row in df.iter_rows(named=True):
        date = str(col(row, "date") or "").strip()[:10]
        if not date:
            continue
        cat = str(col(row, "category") or "").strip()
        code = str(col(row, "code") or "").strip()
        symbol = normalize_code(code) if code else ""
        amount = _f(col(row, "amount"))

        if cat in _BUY or cat in _SELL:
            fills.append(Fill(
                date=date, time=str(col(row, "time") or "").strip(),
                symbol=symbol, name=str(col(row, "name") or "").strip(),
                side="buy" if cat in _BUY else "sell",
                qty=_f(col(row, "qty")), price=_f(col(row, "price")),
                amount=amount, fee=_f(col(row, "fee")),
            ))
            continue

        kind = _CASH_KIND.get(cat)
        if kind is None:
            unknown_cats.add(cat)
            kind = "other"
        events.append(CashEvent(date=date, symbol=symbol, kind=kind, amount=amount))

    for cat in sorted(unknown_cats):
        warnings.append(f"未识别的交易类别「{cat}」已归为 other 现金事件, 不参与配对")
    return fills, events, warnings
```

- [ ] **步骤 5：运行验证通过**

运行：`cd backend && uv run --extra dev pytest tests/services/trade_journal/test_parser.py -v`
预期：5 passed
（若 `pl.read_excel(sheet_id=0)` 返回类型与预期不符——旧版 polars 返回 dict、新版可能变化——按当前 polars 版本文档调整 `read_upload`，测试不变。）

- [ ] **步骤 6：Commit**

```bash
git add backend/app/services/trade_journal/parser.py backend/tests/services/trade_journal/test_parser.py backend/pyproject.toml uv.lock
git commit -m "feat(journal): xlsx/CSV 解析 + 交易类别分类 + A/HK 代码归一"
```

---

### 任务 4：FIFO 配对 fifo.py

**文件：**
- 创建：`backend/app/services/trade_journal/fifo.py`
- 测试：`backend/tests/services/trade_journal/test_fifo.py`

**算法说明（给实现者）：** **单层 position-cycle**（grilling Q1 决策：砍掉 lot 级，统一到周期口径）。同一 symbol 仓位从 0 → >0 → 归 0 为一个 `Roundtrip`（同花顺「已清仓」同口径，对拍用）。真实样本确认同花顺就是这么切的（赛力斯在「已清仓」里出现十几行，每次清零一行）。追涨/锚定诊断**不依赖 roundtrip 单位**，直接在 fills 流上算，所以不需要 lot 级 roundtrip。周期内的 `dividend`/`dividend_tax` 现金事件按日期归属到该周期。持仓天数 = 交易日历含两端；调用方传入排序好的交易日列表，不传则退化为自然日差+1。卖出数量超过持仓（数据缺失/融券）→ 该 symbol 标记 warning 并跳过多余部分。

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/services/trade_journal/test_fifo.py
from app.services.trade_journal.fifo import pair_roundtrips
from app.services.trade_journal.models import CashEvent, Fill


def _fill(date, sym, side, qty, amount, fee=0.0, price=0.0):
    return Fill(date=date, time="", symbol=sym, name=sym, side=side,
                qty=qty, price=price, amount=amount, fee=fee)


def test_single_cycle_matches_ths_numbers():
    """银河样本第一笔: 买 200@-11221.23 卖 200@+12334.48 → pnl=1113.25。"""
    fills = [
        _fill("2024-02-05", "601127.SH", "buy", 200, -11221.23, fee=1.23),
        _fill("2024-02-06", "601127.SH", "sell", 200, 12334.48, fee=7.52),
    ]
    trips, open_pos, warnings = pair_roundtrips(fills, [], trading_days=None)
    assert len(trips) == 1 and open_pos == [] and warnings == []
    t = trips[0]
    assert abs(t.pnl - 1113.25) < 1e-9
    assert abs(t.fees - 8.75) < 1e-9
    assert t.open_date == "2024-02-05" and t.close_date == "2024-02-06"
    assert t.holding_days == 2  # 自然日差+1 (未传交易日历)


def test_holding_days_uses_trading_calendar():
    days = ["2024-03-01", "2024-03-04", "2024-03-05", "2024-03-06", "2024-03-07",
            "2024-03-08", "2024-03-11", "2024-03-12", "2024-03-13", "2024-03-14",
            "2024-03-15", "2024-03-18", "2024-03-19", "2024-03-20", "2024-03-21",
            "2024-03-22", "2024-03-25"]
    fills = [
        _fill("2024-03-01", "600895.SH", "buy", 2200, -48163.30),
        _fill("2024-03-25", "600895.SH", "sell", 2200, 47051.28),
    ]
    trips, _, _ = pair_roundtrips(fills, [], trading_days=days)
    assert trips[0].holding_days == 17  # 银河样本: 张江高科 17 个交易日含两端


def test_multi_buy_single_cycle_aggregates():
    fills = [
        _fill("2024-02-08", "601127.SH", "buy", 900, -63276.96),
        _fill("2024-02-08", "601127.SH", "buy", 100, -7011.07),
        _fill("2024-03-26", "601127.SH", "sell", 1000, 91940.0),
    ]
    trips, open_pos, _ = pair_roundtrips(fills, [], trading_days=None)
    assert len(trips) == 1
    assert trips[0].qty == 1000
    assert abs(trips[0].buy_net - 70288.03) < 1e-9


def test_open_position_not_a_trip():
    fills = [_fill("2024-05-01", "000988.SZ", "buy", 900, -120000.0)]
    trips, open_pos, _ = pair_roundtrips(fills, [], trading_days=None)
    assert trips == []
    assert len(open_pos) == 1 and open_pos[0]["symbol"] == "000988.SZ" and open_pos[0]["qty"] == 900


def test_two_separate_cycles_same_symbol():
    fills = [
        _fill("2024-02-05", "601127.SH", "buy", 200, -11221.23),
        _fill("2024-02-06", "601127.SH", "sell", 200, 12334.48),
        _fill("2024-04-09", "601127.SH", "buy", 1000, -85920.0),
        _fill("2024-04-10", "601127.SH", "sell", 1000, 83050.0),
    ]
    trips, _, _ = pair_roundtrips(fills, [], trading_days=None)
    assert len(trips) == 2
    assert trips[0].close_date == "2024-02-06" and trips[1].close_date == "2024-04-10"


def test_same_day_rebuy_does_not_close_cycle():
    """High 1 对拍规则: 300738 真实场景——11-15 卖光 1200 同日又买回 2200,
    同花顺并入一段 11-11→12-03; 跨日买回才另起。"""
    fills = [
        _fill("2024-11-11", "300738.SZ", "buy", 1200, -16321.63),
        _fill("2024-11-15", "300738.SZ", "sell", 1200, 16418.15),   # 归零
        _fill("2024-11-15", "300738.SZ", "buy", 1100, -14686.47),   # 同日买回 → 不平仓
        _fill("2024-11-15", "300738.SZ", "buy", 1100, -14620.46),
        _fill("2024-11-27", "300738.SZ", "sell", 1100, 13741.74),
        _fill("2024-12-03", "300738.SZ", "sell", 1100, 14137.52),
    ]
    trips, _, _ = pair_roundtrips(fills, [], trading_days=None)
    assert len(trips) == 1
    assert trips[0].open_date == "2024-11-11" and trips[0].close_date == "2024-12-03"
    assert trips[0].qty == 3400  # 1200 + 1100 + 1100 全并入


def test_dividend_attributed_to_containing_cycle():
    fills = [
        _fill("2024-07-01", "600418.SH", "buy", 600, -10000.0),
        _fill("2024-08-01", "600418.SH", "sell", 600, 10500.0),
    ]
    events = [
        CashEvent(date="2024-07-18", symbol="600418.SH", kind="dividend", amount=42.0),
        CashEvent(date="2024-07-23", symbol="600418.SH", kind="dividend_tax", amount=-8.4),
    ]
    trips, _, _ = pair_roundtrips(fills, events, trading_days=None)
    assert abs(trips[0].dividend - 33.6) < 1e-9
    assert abs(trips[0].pnl - 500.0) < 1e-9  # pnl 不含 dividend, 单列


def test_oversell_warns_and_skips_excess():
    fills = [
        _fill("2024-01-02", "600000.SH", "buy", 100, -800.0),
        _fill("2024-01-03", "600000.SH", "sell", 300, 2500.0),
    ]
    trips, _, warnings = pair_roundtrips(fills, [], trading_days=None)
    assert len(warnings) == 1 and "600000.SH" in warnings[0]
```

- [ ] **步骤 2：运行验证失败**

运行：`cd backend && uv run --extra dev pytest tests/services/trade_journal/test_fifo.py -v`
预期：FAIL，import error

- [ ] **步骤 3：编写实现**

```python
# backend/app/services/trade_journal/fifo.py
"""FIFO 配对: fills → position-cycle Roundtrip 列表。

同花顺「已清仓」同口径: 仓位 0 → >0 → 归 0 为一个周期 (对拍 oracle 依据)。
纯函数, 无 IO。trading_days 由调用方注入 (API 层从 kline_daily 分区取)。
"""
from __future__ import annotations

import bisect
from collections import defaultdict

from app.services.trade_journal.models import CashEvent, Fill, Roundtrip

_EPS = 1e-6


def _holding_days(open_date: str, close_date: str, trading_days: list[str] | None) -> int:
    if trading_days:
        lo = bisect.bisect_left(trading_days, open_date)
        hi = bisect.bisect_right(trading_days, close_date)
        n = hi - lo
        if n > 0:
            return n
    from datetime import date
    try:
        return (date.fromisoformat(close_date) - date.fromisoformat(open_date)).days + 1
    except ValueError:
        return 1


def pair_roundtrips(
    fills: list[Fill],
    events: list[CashEvent],
    trading_days: list[str] | None,
) -> tuple[list[Roundtrip], list[dict], list[str]]:
    """返回 (已平仓周期, 未平仓持仓, 警告)。"""
    warnings: list[str] = []
    by_symbol: dict[str, list[Fill]] = defaultdict(list)
    for f in sorted(fills, key=lambda f: (f.date, f.time)):
        by_symbol[f.symbol].append(f)

    div_by_symbol: dict[str, list[CashEvent]] = defaultdict(list)
    for ev in events:
        if ev.kind in ("dividend", "dividend_tax") and ev.symbol:
            div_by_symbol[ev.symbol].append(ev)

    trips: list[Roundtrip] = []
    open_positions: list[dict] = []

    for symbol, sfills in by_symbol.items():
        pos = 0.0
        cycle: list[Fill] = []
        for idx, f in enumerate(sfills):
            if f.side == "sell" and f.qty > pos + _EPS:
                warnings.append(
                    f"{symbol} {f.date} 卖出 {f.qty:g} 超过持仓 {pos:g}, 超出部分跳过(可能缺早期流水)")
                if pos <= _EPS:
                    continue
                scale = pos / f.qty
                f = Fill(date=f.date, time=f.time, symbol=f.symbol, name=f.name,
                         side="sell", qty=pos, price=f.price,
                         amount=f.amount * scale, fee=f.fee * scale)
            cycle.append(f)
            pos += f.qty if f.side == "buy" else -f.qty
            if pos <= _EPS and cycle:
                # High 1 修正(已用银河样本 446 行对拍验证): 仓位归零后若"同日"又买回,
                # 同花顺不视为平仓、并入同一段(如 300738 的 2024-11-15)。跨交易日买回才另起一段。
                nxt = sfills[idx + 1] if idx + 1 < len(sfills) else None
                if nxt is not None and nxt.side == "buy" and nxt.date == f.date:
                    continue
                trips.append(_close_cycle(symbol, cycle, div_by_symbol.get(symbol, []), trading_days))
                cycle, pos = [], 0.0
        if cycle and pos > _EPS:
            buy_net = sum(-f.amount for f in cycle if f.side == "buy")
            open_positions.append({
                "symbol": symbol, "name": cycle[-1].name, "qty": pos,
                "open_date": cycle[0].date, "cost_net": round(buy_net, 2),
            })

    trips.sort(key=lambda t: t.close_date)
    return trips, open_positions, warnings


def _close_cycle(
    symbol: str, cycle: list[Fill], divs: list[CashEvent], trading_days: list[str] | None,
) -> Roundtrip:
    open_date, close_date = cycle[0].date, cycle[-1].date
    dividend = sum(ev.amount for ev in divs if open_date <= ev.date <= close_date)
    return Roundtrip(
        symbol=symbol, name=cycle[-1].name,
        open_date=open_date, close_date=close_date,
        qty=sum(f.qty for f in cycle if f.side == "buy"),
        buy_net=sum(-f.amount for f in cycle if f.side == "buy"),
        sell_net=sum(f.amount for f in cycle if f.side == "sell"),
        fees=sum(f.fee for f in cycle),
        dividend=dividend,
        holding_days=_holding_days(open_date, close_date, trading_days),
    )
```

**注意（实现者必读）：** 股息税(`股息个税征收`)常发生在清仓之后几天（银河样本：卖出后 T+N 补扣），本实现按日期窗口归属会漏掉这部分——**先按窗口实现并通过测试**，oracle 对拍脚本（任务 7）会量化这个偏差；若对拍显示同花顺把清仓后的税也算进该笔，再把归属规则改为「归属到该 symbol 最近一个已平仓周期」。

- [ ] **步骤 4：运行验证通过**

运行：`cd backend && uv run --extra dev pytest tests/services/trade_journal/test_fifo.py -v`
预期：7 passed

- [ ] **步骤 5：Commit**

```bash
git add backend/app/services/trade_journal/fifo.py backend/tests/services/trade_journal/test_fifo.py
git commit -m "feat(journal): FIFO position-cycle 配对 (同花顺已清仓同口径)"
```

---

### 任务 5：行为诊断 diagnose.py

**文件：**
- 创建：`backend/app/services/trade_journal/diagnose.py`
- 测试：`backend/tests/services/trade_journal/test_diagnose.py`

**四项闭式指标（R3：纯统计，零 LLM）：**
1. **处置效应**：亏损单平均持仓天数 / 盈利单平均持仓天数。>1.5 = 明显「拿着亏的、卖掉赚的」。
2. **过度交易**：月均完成周期数 + 总费用占总盈亏绝对值比。
3. **追涨**：买入日收盘价处于此前 20 个交易日 high-low 区间的分位 >0.9 的买入占比。需要外部行情，由调用方注入 `price_lookup: dict[(symbol, date), dict]`（内含 `pos_20d`: 0-1 分位，API 层预计算），缺数据的买入跳过。
4. **锚定加仓**：在浮亏状态下加仓的买入笔数占全部加仓（非首笔）买入的比例。浮亏判定：加仓价 < 当前周期已持仓部分的均价。

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/services/trade_journal/test_diagnose.py
from app.services.trade_journal.diagnose import diagnose
from app.services.trade_journal.models import Fill, Roundtrip


def _trip(pnl_sign, holding_days, symbol="600000.SH"):
    buy = 10000.0
    return Roundtrip(symbol=symbol, name=symbol, open_date="2024-01-01",
                     close_date="2024-01-10", qty=1000, buy_net=buy,
                     sell_net=buy + pnl_sign * 500, fees=10.0, dividend=0.0,
                     holding_days=holding_days)


def _fill(date, sym, side, qty, price):
    return Fill(date=date, time="", symbol=sym, name=sym, side=side, qty=qty,
                price=price, amount=-qty * price if side == "buy" else qty * price, fee=0.0)


def test_disposition_ratio_losers_held_longer():
    trips = [_trip(+1, 2), _trip(+1, 4), _trip(-1, 12), _trip(-1, 18)]
    d = diagnose(trips, fills=[], price_lookup={})
    assert abs(d["disposition"]["ratio"] - 5.0) < 1e-9  # (12+18)/2 ÷ (2+4)/2
    assert d["disposition"]["flag"] is True


def test_overtrading_stats():
    trips = [_trip(+1, 2) for _ in range(6)]  # 全部 close 在 2024-01
    d = diagnose(trips, fills=[], price_lookup={})
    assert d["overtrading"]["trips_per_month"] == 6.0
    assert d["overtrading"]["fee_ratio"] > 0


def test_chasing_high_ratio():
    fills = [
        _fill("2024-01-05", "600000.SH", "buy", 100, 10.0),
        _fill("2024-01-08", "600001.SH", "buy", 100, 10.0),
    ]
    lookup = {
        ("600000.SH", "2024-01-05"): {"pos_20d": 0.95},  # 追高
        ("600001.SH", "2024-01-08"): {"pos_20d": 0.30},
    }
    d = diagnose([], fills=fills, price_lookup=lookup)
    assert abs(d["chasing"]["ratio"] - 0.5) < 1e-9
    assert d["chasing"]["n_covered"] == 2


def test_anchoring_add_on_losers():
    fills = [
        _fill("2024-01-02", "600000.SH", "buy", 100, 10.0),
        _fill("2024-01-03", "600000.SH", "buy", 100, 9.0),   # 浮亏加仓
        _fill("2024-01-04", "600000.SH", "buy", 100, 11.0),  # 浮盈加仓
    ]
    d = diagnose([], fills=fills, price_lookup={})
    assert abs(d["anchoring"]["ratio"] - 0.5) < 1e-9  # 2 次加仓, 1 次在浮亏
```

- [ ] **步骤 2：运行验证失败**

运行：`cd backend && uv run --extra dev pytest tests/services/trade_journal/test_diagnose.py -v`
预期：FAIL

- [ ] **步骤 3：编写实现**

```python
# backend/app/services/trade_journal/diagnose.py
"""四项行为偏差诊断。纯统计闭式计算, 零 LLM (红线 R3)。"""
from __future__ import annotations

from collections import defaultdict

from app.services.trade_journal.models import Fill, Roundtrip

_EPS = 1e-9


def diagnose(
    trips: list[Roundtrip],
    fills: list[Fill],
    price_lookup: dict[tuple[str, str], dict],
) -> dict:
    return {
        "disposition": _disposition(trips),
        "overtrading": _overtrading(trips),
        "chasing": _chasing(fills, price_lookup),
        "anchoring": _anchoring(fills),
    }


def _disposition(trips: list[Roundtrip]) -> dict:
    win = [t.holding_days for t in trips if t.total_pnl > 0]
    loss = [t.holding_days for t in trips if t.total_pnl <= 0]
    if not win or not loss:
        return {"ratio": None, "flag": False, "note": "盈利或亏损样本不足"}
    ratio = (sum(loss) / len(loss)) / max(sum(win) / len(win), _EPS)
    return {
        "ratio": round(ratio, 2), "flag": ratio > 1.5,
        "avg_days_win": round(sum(win) / len(win), 1),
        "avg_days_loss": round(sum(loss) / len(loss), 1),
    }


def _overtrading(trips: list[Roundtrip]) -> dict:
    if not trips:
        return {"trips_per_month": 0.0, "fee_ratio": 0.0, "flag": False}
    months = {t.close_date[:7] for t in trips}
    per_month = len(trips) / max(len(months), 1)
    total_fees = sum(t.fees for t in trips)
    gross = sum(abs(t.total_pnl) for t in trips)
    fee_ratio = total_fees / max(gross, _EPS)
    return {
        "trips_per_month": round(per_month, 1),
        "fee_ratio": round(fee_ratio, 4),
        "total_fees": round(total_fees, 2),
        "flag": per_month > 20 or fee_ratio > 0.3,
    }


def _chasing(fills: list[Fill], price_lookup: dict[tuple[str, str], dict]) -> dict:
    buys = [f for f in fills if f.side == "buy"]
    covered, high = 0, 0
    for f in buys:
        info = price_lookup.get((f.symbol, f.date))
        if not info or info.get("pos_20d") is None:
            continue
        covered += 1
        if float(info["pos_20d"]) > 0.9:
            high += 1
    ratio = high / covered if covered else None
    return {"ratio": None if ratio is None else round(ratio, 2),
            "n_covered": covered, "flag": bool(ratio and ratio > 0.5)}


def _anchoring(fills: list[Fill]) -> dict:
    state: dict[str, tuple[float, float]] = defaultdict(lambda: (0.0, 0.0))  # qty, cost
    adds, adds_losing = 0, 0
    for f in sorted(fills, key=lambda f: (f.date, f.time)):
        qty, cost = state[f.symbol]
        if f.side == "buy":
            if qty > _EPS:  # 加仓 (非首笔)
                adds += 1
                if f.price < cost / qty - _EPS:
                    adds_losing += 1
            state[f.symbol] = (qty + f.qty, cost + f.qty * f.price)
        else:
            new_qty = max(qty - f.qty, 0.0)
            state[f.symbol] = (new_qty, cost * (new_qty / qty) if qty > _EPS else 0.0)
    ratio = adds_losing / adds if adds else None
    return {"ratio": None if ratio is None else round(ratio, 2),
            "n_adds": adds, "flag": bool(ratio and ratio > 0.6)}
```

- [ ] **步骤 4：运行验证通过**

运行：`cd backend && uv run --extra dev pytest tests/services/trade_journal/test_diagnose.py -v`
预期：4 passed

- [ ] **步骤 5：Commit**

```bash
git add backend/app/services/trade_journal/diagnose.py backend/tests/services/trade_journal/test_diagnose.py
git commit -m "feat(journal): 四项行为偏差纯统计诊断"
```

---

### 任务 5B：追涨行情分位 price_lookup（grilling Q3：MVP 就接活）

**文件：**
- 创建：`backend/app/services/trade_journal/pricepos.py`
- 测试：`backend/tests/services/trade_journal/test_pricepos.py`

**目的：** 为「追涨」诊断提供 `price_lookup[(symbol, date)] = {"pos_20d": 0-1}`——买入日收盘价在此前 20 个交易日 high-low 区间的分位。经 `repo.get_daily`（读 `kline_daily_enriched`，项目主读法）取 A 股日K；**港股本地无 parquet → 显式跳过并列入 uncovered（Med 1）**。`build_price_lookup` 是唯一读盘处，纯函数 `compute_pos` 可单测，返回 `(lookup, uncovered)`。

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/services/trade_journal/test_pricepos.py
from app.services.trade_journal.models import Fill
from app.services.trade_journal.pricepos import compute_pos


def _fill(date, sym):
    return Fill(date=date, time="", symbol=sym, name=sym, side="buy",
                qty=100, price=0.0, amount=-1000.0, fee=0.0)


def test_compute_pos_at_range_high():
    # 前 20 日 low=10 high=20, 买入日 close=20 → 分位 1.0
    hist = [{"date": f"2024-01-{i:02d}", "high": 20.0, "low": 10.0} for i in range(1, 21)]
    assert abs(compute_pos(20.0, hist) - 1.0) < 1e-9


def test_compute_pos_at_range_low():
    hist = [{"date": f"2024-01-{i:02d}", "high": 20.0, "low": 10.0} for i in range(1, 21)]
    assert abs(compute_pos(10.0, hist) - 0.0) < 1e-9


def test_compute_pos_midpoint():
    hist = [{"date": f"2024-01-{i:02d}", "high": 20.0, "low": 10.0} for i in range(1, 21)]
    assert abs(compute_pos(15.0, hist) - 0.5) < 1e-9


def test_compute_pos_insufficient_history_returns_none():
    assert compute_pos(15.0, [{"date": "2024-01-01", "high": 20.0, "low": 10.0}] * 3) is None
```

- [ ] **步骤 2：运行验证失败**

运行：`cd backend && uv run --extra dev pytest tests/services/trade_journal/test_pricepos.py -v`
预期：FAIL，import error

- [ ] **步骤 3：编写实现**

```python
# backend/app/services/trade_journal/pricepos.py
"""追涨诊断的行情分位: 买入日 close 在前 N 个交易日 high-low 区间的位置。

build_price_lookup 是唯一读盘处, 走 repo.get_daily(读 kline_daily_enriched, 与项目主读法一致,
Med 1 修正: 不再硬扫 raw kline_daily glob)。compute_pos 是可单测纯函数。
港股(.HK)本地 parquet 无数据 → 显式跳过并计入 uncovered, 不静默丢失(Med 1)。
"""
from __future__ import annotations

import logging
from datetime import date

from app.services.trade_journal.models import Fill

logger = logging.getLogger(__name__)

_WINDOW = 20
_MIN_HISTORY = 10


def compute_pos(close: float, hist: list[dict]) -> float | None:
    """close 在 hist(前 N 日 high/low)区间的 0-1 分位。历史不足 _MIN_HISTORY 返回 None。"""
    if len(hist) < _MIN_HISTORY:
        return None
    hi = max(float(r["high"]) for r in hist)
    lo = min(float(r["low"]) for r in hist)
    if hi - lo < 1e-9:
        return 0.5
    return max(0.0, min(1.0, (close - lo) / (hi - lo)))


def build_price_lookup(fills: list[Fill], repo) -> tuple[dict[tuple[str, str], dict], list[str]]:
    """为每个 A 股买入 fill 算 pos_20d。返回 (lookup, uncovered_symbols)。

    repo 由 API 层从 request.app.state.repo 注入。港股显式跳过并列入 uncovered(Med 1)。
    """
    from datetime import timedelta

    lookup: dict[tuple[str, str], dict] = {}
    uncovered: set[str] = set()
    buys_by_symbol: dict[str, list[Fill]] = {}
    for f in fills:
        if f.side == "buy":
            buys_by_symbol.setdefault(f.symbol, []).append(f)

    for symbol, buys in buys_by_symbol.items():
        if symbol.endswith(".HK"):
            uncovered.add(symbol)  # 港股本地无 parquet, 显式跳过
            continue
        try:
            lo_d = date.fromisoformat(min(f.date for f in buys)) - timedelta(days=60)
            hi_d = date.fromisoformat(max(f.date for f in buys))
            df = repo.get_daily(symbol, lo_d, hi_d, columns=["date", "high", "low", "close"])
            if df.is_empty():
                uncovered.add(symbol)
                continue
            rows = df.sort("date").with_columns(pl_col_date()).to_dicts()
            date_idx = {r["date"]: i for i, r in enumerate(rows)}
            covered_any = False
            for f in buys:
                i = date_idx.get(f.date)
                if i is None or i < _MIN_HISTORY:
                    continue
                pos = compute_pos(float(rows[i]["close"]), rows[max(0, i - _WINDOW):i])
                if pos is not None:
                    lookup[(symbol, f.date)] = {"pos_20d": pos}
                    covered_any = True
            if not covered_any:
                uncovered.add(symbol)
        except Exception as e:  # noqa: BLE001
            logger.debug("price_lookup %s 跳过: %s", symbol, e)
            uncovered.add(symbol)
    return lookup, sorted(uncovered)


def pl_col_date():
    """date 列转字符串(隔离 polars import, 便于上面纯逻辑阅读)。"""
    import polars as pl
    return pl.col("date").cast(pl.Utf8)
```

**注意（实现者）：** `repo.get_daily(symbol, start, end, columns)` 读 `kline_daily_enriched`（`backend/app/tickflow/repository.py:887`），与项目主读法一致，已含 warmup 预热，返回 date/high/low/close。测试只测 `compute_pos` 纯函数，不碰 repo，路径细节不影响测试通过。

- [ ] **步骤 4：运行验证通过**

运行：`cd backend && uv run --extra dev pytest tests/services/trade_journal/test_pricepos.py -v`
预期：4 passed

- [ ] **步骤 5：Commit**

```bash
git add backend/app/services/trade_journal/pricepos.py backend/tests/services/trade_journal/test_pricepos.py
git commit -m "feat(journal): 追涨诊断行情分位 price_lookup"
```

---

### 任务 6：基准超额 benchmark.py

**文件：**
- 创建：`backend/app/services/trade_journal/benchmark.py`
- 测试：`backend/tests/services/trade_journal/test_benchmark.py`

**口径（R6）：** 主指标 = 账户-区间超额：台账窗口（首笔 open_date → 末笔 close_date）内，`总已实现收益 / 总投入买入净额` vs 同窗口基准涨幅。辅指标 = 逐笔超额（附表 + 噪声警示语）。港股回合 `benchmark_pct=None`。基准行情由调用方注入 `index_closes: dict[str, float]`（date→close，API 层从 `repo.get_index_daily("000300.SH", ...)` 取），纯函数无 IO。

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/services/trade_journal/test_benchmark.py
from app.services.trade_journal.benchmark import account_excess, per_trip_excess
from app.services.trade_journal.models import Roundtrip


def _trip(sym, open_d, close_d, buy, sell):
    return Roundtrip(symbol=sym, name=sym, open_date=open_d, close_date=close_d,
                     qty=100, buy_net=buy, sell_net=sell, fees=0.0, dividend=0.0,
                     holding_days=2)


CLOSES = {"2024-01-02": 3400.0, "2024-01-10": 3468.0, "2024-02-01": 3536.72}


def test_account_excess_vs_benchmark():
    trips = [
        _trip("600000.SH", "2024-01-02", "2024-01-10", 10000, 10800),  # +8%
        _trip("000001.SZ", "2024-01-10", "2024-02-01", 10000, 10200),  # +2%
    ]
    r = account_excess(trips, CLOSES)
    assert abs(r["account_return"] - 0.05) < 1e-9        # 1000/20000
    assert abs(r["benchmark_return"] - (3536.72 / 3400.0 - 1)) < 1e-9
    assert abs(r["excess"] - (0.05 - (3536.72 / 3400.0 - 1))) < 1e-9
    assert r["window"] == ["2024-01-02", "2024-02-01"]


def test_account_excess_empty():
    assert account_excess([], CLOSES)["account_return"] is None


def test_per_trip_excess_and_hk_none():
    trips = [
        _trip("600000.SH", "2024-01-02", "2024-01-10", 10000, 10800),
        _trip("02577.HK", "2024-01-02", "2024-01-10", 10000, 9000),
    ]
    rows = per_trip_excess(trips, CLOSES)
    assert abs(rows[0]["benchmark_pct"] - 0.02) < 1e-9   # 3468/3400-1
    assert abs(rows[0]["excess"] - 0.06) < 1e-9
    assert rows[1]["benchmark_pct"] is None and rows[1]["excess"] is None  # HK 留空 (R6)


def test_missing_benchmark_dates_fall_back_to_nearest_prior():
    trips = [_trip("600000.SH", "2024-01-03", "2024-01-11", 10000, 10800)]
    rows = per_trip_excess(trips, CLOSES)  # 01-03/01-11 缺 → 用 01-02/01-10
    assert abs(rows[0]["benchmark_pct"] - 0.02) < 1e-9
```

- [ ] **步骤 2：运行验证失败**

运行：`cd backend && uv run --extra dev pytest tests/services/trade_journal/test_benchmark.py -v`
预期：FAIL

- [ ] **步骤 3：编写实现**

```python
# backend/app/services/trade_journal/benchmark.py
"""基准超额: 账户-区间为主, 逐笔为辅 (R6)。纯函数, 基准 close 序列由调用方注入。"""
from __future__ import annotations

import bisect

from app.services.trade_journal.models import Roundtrip

NOISE_NOTE = "单笔短周期超额噪声大, 勿据此评判选股能力"


def _close_at_or_before(closes: dict[str, float], date: str) -> float | None:
    if date in closes:
        return closes[date]
    keys = sorted(closes)
    i = bisect.bisect_right(keys, date) - 1
    return closes[keys[i]] if i >= 0 else None


def _bench_return(closes: dict[str, float], start: str, end: str) -> float | None:
    c0, c1 = _close_at_or_before(closes, start), _close_at_or_before(closes, end)
    if not c0 or not c1:
        return None
    return c1 / c0 - 1


def account_excess(trips: list[Roundtrip], index_closes: dict[str, float]) -> dict:
    if not trips:
        return {"account_return": None, "benchmark_return": None, "excess": None, "window": None}
    start = min(t.open_date for t in trips)
    end = max(t.close_date for t in trips)
    invested = sum(t.buy_net for t in trips)
    pnl = sum(t.total_pnl for t in trips)  # 含分红, 与同花顺头条一致
    acct = pnl / invested if invested else None
    bench = _bench_return(index_closes, start, end)
    return {
        "account_return": acct,
        "benchmark_return": bench,
        "excess": (acct - bench) if (acct is not None and bench is not None) else None,
        "window": [start, end],
    }


def per_trip_excess(trips: list[Roundtrip], index_closes: dict[str, float]) -> list[dict]:
    rows = []
    for t in trips:
        if t.symbol.endswith(".HK"):
            bench = None  # R6: 港股基准存疑, 留空不硬凑
        else:
            bench = _bench_return(index_closes, t.open_date, t.close_date)
        rows.append({
            "symbol": t.symbol, "name": t.name,
            "open_date": t.open_date, "close_date": t.close_date,
            "pnl": round(t.pnl, 2), "total_pnl": round(t.total_pnl, 2),
            "pnl_pct": round(t.pnl_pct, 4),
            "benchmark_pct": bench if bench is None else round(bench, 4),
            "excess": None if bench is None else round(t.pnl_pct - bench, 4),
            "holding_days": t.holding_days,
        })
    return rows
```

- [ ] **步骤 4：运行验证通过**

运行：`cd backend && uv run --extra dev pytest tests/services/trade_journal/test_benchmark.py -v`
预期：4 passed

- [ ] **步骤 5：Commit**

```bash
git add backend/app/services/trade_journal/benchmark.py backend/tests/services/trade_journal/test_benchmark.py
git commit -m "feat(journal): 账户区间超额(主) + 逐笔超额(辅), HK 留空"
```

---

### 任务 7：持久化 store.py + API 端点

**文件：**
- 创建：`backend/app/services/trade_journal/store.py`
- 创建：`backend/app/api/trade_journal.py`
- 修改：`backend/app/main.py`（import 区 + `app.include_router(rps.router)` 后加一行）
- 测试：`backend/tests/services/trade_journal/test_store.py`、`backend/tests/api/test_trade_journal.py`

**API 设计（两步式，原始文件零落盘）：**
- `POST /api/journal/upload?commit=false`（FormData: file, 可选 sheet/preset）→ 返回 `{sheets, columns, guessed_mapping, preview_rows(前20), warnings}`——纯预览，不落任何东西。
- `POST /api/journal/upload?commit=true`（FormData 同上 + mapping JSON 字段）→ 解析→FIFO→诊断→基准→写 `ledger.json` → 返回完整报告。原始 bytes 用完即弃。
- `GET /api/journal/ledger` → 已保存台账（trips/summary/diagnosis/benchmark/warnings/imported_at）。
- `DELETE /api/journal/ledger` → 删除。
- MVP 语义：**每次 commit 覆盖整本台账**（单账本）。多次导入合并去重是后续项，YAGNI。

- [ ] **步骤 1：编写 store 失败测试**

```python
# backend/tests/services/trade_journal/test_store.py
from app.services.trade_journal import store


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    payload = {"trips": [{"symbol": "601127.SH", "pnl": 1113.25}], "summary": {"total_trips": 1}}
    store.save_ledger(payload)
    loaded = store.load_ledger()
    assert loaded["trips"][0]["symbol"] == "601127.SH"
    assert "imported_at" in loaded
    assert store.delete_ledger() is True
    assert store.load_ledger() is None
```

- [ ] **步骤 2：运行验证失败**，然后实现：

```python
# backend/app/services/trade_journal/store.py
"""台账持久化: data/user_data/trade_journal/ledger.json (对齐 strategy_cache 模式)。

红线 R5: 只存归一化台账, 永不存原始上传文件。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _path() -> Path:
    from app.config import settings
    p = settings.data_dir / "user_data" / "trade_journal" / "ledger.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def save_ledger(payload: dict) -> dict:
    payload = dict(payload)
    payload["imported_at"] = datetime.now().isoformat(timespec="seconds")
    _path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("trade journal ledger saved: %d trips", len(payload.get("trips", [])))
    return payload


def load_ledger() -> dict | None:
    p = _path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("ledger.json unreadable: %s", e)
        return None


def delete_ledger() -> bool:
    p = _path()
    if p.exists():
        p.unlink()
        return True
    return False
```

- [ ] **步骤 3：编写 API 失败测试**

```python
# backend/tests/api/test_trade_journal.py
import io

import xlsxwriter
from fastapi.testclient import TestClient

from app.main import app

THS_COLS = ["成交日期", "成交时间", "代码", "名称", "交易类别",
            "成交数量", "成交价格", "发生金额", "成交金额", "费用", "备注"]
ROWS = [
    ["2024-02-05", "14:53:08", "601127", "赛力斯", "买入", 200, 56.1, -11221.23, 11220, 1.23, ""],
    ["2024-02-06", "14:54:53", "601127", "赛力斯", "卖出", 200, 61.71, 12334.48, 12342, 7.52, ""],
]


def _xlsx_bytes() -> bytes:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("交易记录")
    ws.write_row(0, 0, THS_COLS)
    for i, r in enumerate(ROWS, start=1):
        ws.write_row(i, 0, r)
    wb.close()
    return buf.getvalue()


def test_upload_preview_then_commit(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    client = TestClient(app)

    r = client.post("/api/journal/upload?commit=false",
                    files={"file": ("j.xlsx", _xlsx_bytes(),
                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200
    body = r.json()
    assert body["guessed_mapping"]["成交日期"] == "date"
    assert len(body["preview_rows"]) == 2

    r = client.post("/api/journal/upload?commit=true",
                    files={"file": ("j.xlsx", _xlsx_bytes(),
                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200
    report = r.json()
    assert report["summary"]["total_trips"] == 1
    assert abs(report["trips"][0]["pnl"] - 1113.25) < 1e-6

    r = client.get("/api/journal/ledger")
    assert r.status_code == 200 and r.json()["summary"]["total_trips"] == 1

    assert client.delete("/api/journal/ledger").status_code == 200
    assert client.get("/api/journal/ledger").status_code == 404
```

- [ ] **步骤 4：运行验证失败**，然后实现 API：

```python
# backend/app/api/trade_journal.py
"""Trade Journal API — 模块唯一 IO 层。

上传文件只在请求内存中处理, 解析完即弃 (红线 R5)。
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.services.trade_journal import store
from app.services.trade_journal.benchmark import NOISE_NOTE, account_excess, per_trip_excess
from app.services.trade_journal.diagnose import diagnose
from app.services.trade_journal.fifo import pair_roundtrips
from app.services.trade_journal.models import LedgerSummary
from app.services.trade_journal.parser import normalize_rows, read_upload
from app.services.trade_journal.presets import PRESETS, THS_PRESET, guess_mapping
from app.services.trade_journal.pricepos import build_price_lookup

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/journal", tags=["trade-journal"])

_MAX_UPLOAD = 20 * 1024 * 1024  # 20MB

# grilling Q4: 可选基准, 限本地已同步的指数。默认沪深300。
BENCHMARKS = {
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
}
_DEFAULT_BENCHMARK = "000300.SH"


def _trading_days_and_closes(
    repo, start: str, end: str, benchmark: str,
) -> tuple[list[str], dict[str, float]]:
    """从指数日 K 取交易日历 + 所选基准 close。任何失败都降级为空 (自然日口径)。

    High 4 修正: repo 由调用方从 request.app.state.repo 注入, 不再 from app.main import app
    (那是错误入口, 易循环导入/拿到未初始化 state)。
    """
    try:
        from datetime import date, timedelta

        s = date.fromisoformat(start) - timedelta(days=45)
        df = repo.get_index_daily(benchmark, s, date.fromisoformat(end), columns=["date", "close"])
        if df.is_empty():
            return [], {}
        days = [str(d) for d in df["date"].to_list()]
        closes = dict(zip(days, (float(c) for c in df["close"].to_list())))
        return [d for d in days if d >= start], closes
    except Exception as e:  # noqa: BLE001
        logger.warning("journal 基准/日历读取失败, 降级自然日口径: %s", e)
        return [], {}


@router.get("/presets")
def list_presets() -> dict:
    return {"presets": PRESETS, "benchmarks": BENCHMARKS}


@router.post("/upload")
async def upload(
    request: Request,
    commit: bool = False,
    file: UploadFile = File(...),
    sheet: str | None = Form(default=None),
    mapping: str | None = Form(default=None),
    benchmark: str = Form(default=_DEFAULT_BENCHMARK),
) -> dict:
    if benchmark not in BENCHMARKS:
        benchmark = _DEFAULT_BENCHMARK
    data = await file.read()
    if len(data) > _MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="文件超过 20MB")
    try:
        sheets, df = read_upload(data, file.filename or "upload.xlsx", sheet)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"文件解析失败: {e}") from e
    finally:
        del data  # 原始 bytes 即弃 (R5)

    col_mapping: dict[str, str] = json.loads(mapping) if mapping else (
        THS_PRESET["mapping"] if set(THS_PRESET["mapping"]) <= set(df.columns)
        else guess_mapping(list(df.columns))
    )

    if not commit:
        return {
            "sheets": sheets,
            "columns": list(df.columns),
            "guessed_mapping": col_mapping,
            "preview_rows": df.head(20).to_dicts(),
            "row_count": df.height,
        }

    required = {"date", "code", "category", "qty", "price", "amount"}
    missing = required - set(col_mapping.values())
    if missing:
        raise HTTPException(status_code=400, detail=f"映射缺少必填字段: {sorted(missing)}")

    fills, events, warnings = normalize_rows(df, col_mapping)
    if not fills:
        raise HTTPException(status_code=400, detail="未解析出任何买入/卖出成交")

    start = min(f.date for f in fills)
    end = max(f.date for f in fills)
    trading_days, closes = _trading_days_and_closes(request.app.state.repo, start, end, benchmark)
    trips, open_positions, pair_warnings = pair_roundtrips(fills, events, trading_days or None)
    warnings += pair_warnings

    price_lookup, uncovered_syms = build_price_lookup(fills, request.app.state.repo)  # Q3 追涨接活
    if uncovered_syms:
        warnings.append(f"追涨诊断: {len(uncovered_syms)} 只标的无本地日K(含港股)未覆盖")

    # 官方盈亏用 total_pnl (含分红, 同花顺口径); pnl(纯现金差)作明细保留
    wins = [t for t in trips if t.total_pnl > 0]
    losses = [t for t in trips if t.total_pnl <= 0]
    summary = LedgerSummary(
        total_trips=len(trips), win_trips=len(wins),
        total_pnl=round(sum(t.total_pnl for t in trips), 2),
        total_dividend=round(sum(t.dividend for t in trips), 2),
        total_fees=round(sum(t.fees for t in trips), 2),
        win_rate=round(len(wins) / len(trips), 4) if trips else 0.0,
        avg_win=round(sum(t.total_pnl for t in wins) / len(wins), 2) if wins else 0.0,
        avg_loss=round(sum(t.total_pnl for t in losses) / len(losses), 2) if losses else 0.0,
        profit_factor=round(sum(t.total_pnl for t in wins) / abs(sum(t.total_pnl for t in losses)), 2)
        if losses and sum(t.total_pnl for t in losses) != 0 else 0.0,
        open_positions=open_positions,
    )

    payload = {
        "trips": [
            {"symbol": t.symbol, "name": t.name, "open_date": t.open_date,
             "close_date": t.close_date, "qty": t.qty,
             "buy_avg": round(t.buy_avg, 4), "sell_avg": round(t.sell_avg, 4),
             "pnl": round(t.pnl, 2), "total_pnl": round(t.total_pnl, 2),
             "pnl_pct": round(t.pnl_pct, 4),
             "fees": round(t.fees, 2), "dividend": round(t.dividend, 2),
             "holding_days": t.holding_days}
            for t in trips
        ],
        "summary": summary.__dict__,
        "diagnosis": diagnose(trips, fills, price_lookup),
        "benchmark": {
            "code": benchmark,
            "name": BENCHMARKS[benchmark],
            "account": account_excess(trips, closes),
            "per_trip": per_trip_excess(trips, closes),
            "noise_note": NOISE_NOTE,
        },
        "warnings": warnings,
        "source_filename": file.filename,
    }
    return store.save_ledger(payload)


@router.get("/ledger")
def get_ledger() -> dict:
    ledger = store.load_ledger()
    if ledger is None:
        raise HTTPException(status_code=404, detail="尚未导入交易流水")
    return ledger


@router.delete("/ledger")
def delete_ledger() -> dict:
    return {"deleted": store.delete_ledger()}
```

`backend/app/main.py` 修改（对照现有 import/注册区式样）：

```python
from app.api import trade_journal  # import 区, 与 rps 等并列
# ...
app.include_router(trade_journal.router)  # app.include_router(rps.router) 之后
```

**注意（实现者）：** repo 已按 High 4 修正从 `request.app.state.repo` 注入（对照 `backend/app/api/indices.py` 的 `get_index_daily` 端点式样）。测试里 `TestClient(app)` 会带真实 `app.state.repo`；若测试环境无指数数据，`_trading_days_and_closes` 走降级分支（closes 为空 → benchmark 为 None），断言不受影响。

- [ ] **步骤 5：运行验证通过**

运行：`cd backend && uv run --extra dev pytest tests/services/trade_journal/ tests/api/test_trade_journal.py -v`
预期：全部 passed

- [ ] **步骤 6：跑全量测试**

运行：`cd backend && uv run --extra dev pytest -q`
预期：全部 passed（≥97 + 新增）

- [ ] **步骤 7：Commit**

```bash
git add backend/app/services/trade_journal/store.py backend/app/api/trade_journal.py backend/app/main.py backend/tests/
git commit -m "feat(journal): 台账持久化 + upload/ledger API (原始文件零落盘)"
```

---

### 任务 8：oracle 对拍脚本（本地跑，不进 CI）

**文件：**
- 创建：`backend/scripts/validate_trade_journal_oracle.py`

**目的（R4）：** 吃真实同花顺投资账本 xlsx，用「交易记录」跑我方 FIFO，逐笔对拍「已清仓」sheet（同花顺自己的配对结果）。**真实流水不进 repo**——这是脚本不是测试。

- [ ] **步骤 1：编写脚本**

```python
# backend/scripts/validate_trade_journal_oracle.py
"""对拍: 我方 FIFO vs 同花顺「已清仓」oracle。

用法: cd backend && uv run python scripts/validate_trade_journal_oracle.py ~/Downloads/银河.xlsx

比对键: (symbol, 清仓日期)。比对字段: 总盈亏(total_pnl) / 持仓天数 / 买入均价 / 卖出均价。

已知基准(核查阶段用银河样本实测, 供执行者校验实现是否退化):
- 同日买回不平仓规则(fifo.py High 1)生效后: closed trips 应 = 446(=已清仓行数),
  按(代码,建仓日,清仓日)命中 445/446。
- 唯一白名单 1 行: 601127.SH 我方清仓 2026-06-17, 同花顺 2026-06-18
  (同花顺把清仓日顺延到当天的股息个税事件日; 表现为 1 个 MISS + 1 个 ORACLE-ONLY), 属预期, 不算失败。
- 总盈亏用 total_pnl(含分红)对拍(High 2): 江淮汽车 63.34 = 现金差 29.74 + 净分红 33.6。
若 mismatch 远多于上述, 说明实现退化, 需排查。
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.trade_journal.fifo import pair_roundtrips  # noqa: E402
from app.services.trade_journal.parser import normalize_code, normalize_rows, read_upload  # noqa: E402
from app.services.trade_journal.presets import THS_PRESET  # noqa: E402


def main(path: str) -> int:
    data = Path(path).expanduser().read_bytes()
    _, trades_df = read_upload(data, path, sheet="交易记录")
    fills, events, warnings = normalize_rows(trades_df, THS_PRESET["mapping"])
    for w in warnings:
        print(f"[warn] {w}")

    # 交易日历: 用本仓库 kline_daily 分区目录名 (仓库根/data), 缺则自然日口径
    days: list[str] = []
    kd = Path(__file__).resolve().parents[2] / "data" / "kline_daily"
    if kd.exists():
        days = sorted(p.name.split("=")[1] for p in kd.iterdir() if p.name.startswith("date="))
    trips, open_pos, pw = pair_roundtrips(fills, events, days or None)
    for w in pw:
        print(f"[warn] {w}")

    _, oracle_df = read_upload(data, path, sheet="已清仓")
    oracle = {}
    for r in oracle_df.iter_rows(named=True):
        key = (normalize_code(str(r["代码"])), str(r["清仓日期"])[:10])
        oracle[key] = r

    matched = mismatched = missing = 0
    for t in trips:
        key = (t.symbol, t.close_date)
        o = oracle.pop(key, None)
        if o is None:
            missing += 1
            print(f"[MISS] {key} 我方有、oracle 无  pnl={t.pnl:.2f}")
            continue
        diffs = []
        for label, mine, theirs in [
            ("总盈亏", t.total_pnl, float(o["总盈亏"] or 0)),  # High 2: 用含分红口径对拍
            ("持仓天数", float(t.holding_days), float(o["持仓天数"] or 0)),
            ("买入均价", t.buy_avg, float(o["买入均价"] or 0)),
            ("卖出均价", t.sell_avg, float(o["卖出均价"] or 0)),
        ]:
            tol = 0.02 if "均价" in label else (1.5 if label == "持仓天数" else 0.02)
            if abs(mine - theirs) > tol:
                diffs.append(f"{label}: 我={mine:.4g} 彼={theirs:.4g} (div={t.dividend:.2f})")
        if diffs:
            mismatched += 1
            print(f"[DIFF] {key}  " + "; ".join(diffs))
        else:
            matched += 1

    print(f"\n===== 对拍结果 =====")
    print(f"匹配: {matched}  不匹配: {mismatched}  我方多出: {missing}  oracle 剩余: {len(oracle)}")
    print(f"未平仓: {len(open_pos)} 只")
    for key in list(oracle)[:10]:
        print(f"[ORACLE-ONLY] {key}")
    # 已知白名单: 分红顺延清仓日的 601127 那 1 行(1 MISS + 1 ORACLE-ONLY)属预期
    return 0 if mismatched == 0 and missing <= 1 and len(oracle) <= 1 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
```

- [ ] **步骤 2：本地跑真实文件**

运行：`cd backend && uv run python scripts/validate_trade_journal_oracle.py ~/Downloads/银河.xlsx`
预期：447 笔 oracle 大部分匹配。**逐类分析 DIFF**：
- 若「总盈亏」diff ≈ 该笔 `dividend` → 同花顺含分红，把 API 层报告里 `pnl` 展示口径改为 `pnl + dividend`（models 不动，展示层加）。
- 若「持仓天数」±1 → 检查两端含闭规则。
- 融券/回购标的（204001 等）出现在 oracle → 属现金管理，确认已被归为 CashEvent 而非成交。
- 把发现写进 commit message 与本计划文件的「对拍结论」附注。

- [ ] **步骤 3：Commit**

```bash
git add backend/scripts/validate_trade_journal_oracle.py
git commit -m "feat(journal): 同花顺已清仓 oracle 对拍脚本 + 对拍结论"
```

---

### 任务 9：前端页面

**文件：**
- 修改：`frontend/src/lib/api.ts`（追加 journal 客户端）
- 创建：`frontend/src/pages/TradeJournal.tsx`
- 修改：`frontend/src/router.tsx`（import + `{ path: 'journal', element: <TradeJournal /> }`，放在 `trading` 路由旁；**不动** trading）
- 修改：导航（看 `frontend/src/components/Layout.tsx` 中现有 nav 项定义处，加「交易复盘」入口，图标用 `NotebookPen`）

- [ ] **步骤 1：api.ts 追加**

```typescript
// ===== Trade Journal =====
export interface JournalPreview {
  sheets: string[]
  columns: string[]
  guessed_mapping: Record<string, string>
  preview_rows: Record<string, unknown>[]
  row_count: number
}
export interface JournalTrip {
  symbol: string; name: string; open_date: string; close_date: string
  qty: number; buy_avg: number; sell_avg: number
  pnl: number; total_pnl: number; pnl_pct: number; fees: number; dividend: number; holding_days: number
}
export interface JournalLedger {
  trips: JournalTrip[]
  summary: {
    total_trips: number; win_trips: number; total_pnl: number; total_dividend: number
    total_fees: number; win_rate: number; avg_win: number; avg_loss: number
    profit_factor: number; open_positions: Record<string, unknown>[]
  }
  diagnosis: Record<string, Record<string, unknown>>
  benchmark: {
    code: string
    name: string
    account: { account_return: number | null; benchmark_return: number | null; excess: number | null; window: string[] | null }
    per_trip: (JournalTrip & { benchmark_pct: number | null; excess: number | null })[]
    noise_note: string
  }
  warnings: string[]
  imported_at: string
}

export function journalUpload(file: File, commit: boolean, mapping?: Record<string, string>, sheet?: string, benchmark?: string) {
  const fd = new FormData()
  fd.append('file', file)
  if (mapping) fd.append('mapping', JSON.stringify(mapping))
  if (sheet) fd.append('sheet', sheet)
  if (benchmark) fd.append('benchmark', benchmark)
  return request<JournalLedger | JournalPreview>(`/api/journal/upload?commit=${commit}`, { method: 'POST', body: fd })
}
export const journalLedger = () => request<JournalLedger>('/api/journal/ledger')
export const journalDelete = () => request<{ deleted: boolean }>('/api/journal/ledger', { method: 'DELETE' })
```

- [ ] **步骤 2：TradeJournal.tsx**

页面三个状态区（样式对齐现有页面：`PageHeader` + `rounded-card border border-border bg-surface` 卡片）：
1. **空态**：上传框（`<input type="file" accept=".xlsx,.csv">`）→ 选中后调 `journalUpload(file,false)` 显示预览表 + 猜测映射（每列一个下拉选归一字段，默认取 `guessed_mapping`）+ **基准下拉**（沪深300/中证500/创业板指/科创50，默认沪深300，选项取自 `/api/journal/presets` 的 `benchmarks`）→「确认导入」按钮调 `journalUpload(file,true,mapping,sheet,benchmark)`。
2. **报告态**（有 ledger）：顶部汇总卡（总盈亏[= total_pnl 含分红]/胜率/盈亏比/费用/分红 + 账户超额 vs `benchmark.name`）；四项诊断卡（每项显示 ratio + flag 红黄绿 + 一句**启发式提示**文案——Med 2：措辞标为「提示/heuristic」而非「结论」，过度交易尤其是简单指标；追涨卡显示 `n_covered` 覆盖笔数，港股/无日K 未覆盖）；roundtrip 表格（symbol/日期/total_pnl/pnl_pct/基准/超额/持仓天数，HK 行超额显示「—」）；表格上方灰字显示 `noise_note`；warnings 黄条。
3. **重新导入 / 删除** 按钮（删除调 `journalDelete` 回空态）。

组件不引入新依赖，表格用现有页面的原生 table 模式（参考 `frontend/src/pages/Review.tsx` 的表格写法）。

- [ ] **步骤 3：router.tsx + Layout nav 注册**

```typescript
import { TradeJournal } from './pages/TradeJournal'
// routes children 内, trading 旁:
{ path: 'journal', element: <TradeJournal /> },
```

Layout 导航数组加 `{ to: '/journal', label: '交易复盘', icon: NotebookPen }`（对照现有 nav 项的真实结构写）。

- [ ] **步骤 4：类型检查 + 构建**

运行：`cd frontend && pnpm tsc --noEmit && pnpm build`
预期：0 错误（Vite chunk 警告可忽略）

- [ ] **步骤 5：Commit**

```bash
git add frontend/src/lib/api.ts frontend/src/pages/TradeJournal.tsx frontend/src/router.tsx frontend/src/components/Layout.tsx
git commit -m "feat(journal): 交易复盘页面 — 上传/列映射/台账/诊断报告"
```

---

### 任务 10：收尾验证

- [ ] **步骤 1：后端全量测试**

运行：`cd backend && uv run --extra dev pytest -q`
预期：全部 passed

- [ ] **步骤 2：oracle 对拍（任务 8）达标**

`uv run python scripts/validate_trade_journal_oracle.py ~/Downloads/银河.xlsx` → closed trips = 446、匹配 445、仅 601127 分红顺延那 1 行白名单（脚本返回 0）。

- [ ] **步骤 3：真实文件端到端**

前提：任务 0 回填已跑（`get_index_daily("000300.SH", 2024…)` 有数）。启动 dev 服务，上传 `~/Downloads/银河.xlsx` → 映射自动命中 THS 预设 + 基准默认沪深300 → 确认导入 → 报告页：**446 笔 roundtrip、未平仓 ≈6 只、账户超额有真实数值（非 null）、四项诊断全绿（追涨对 A 股有覆盖、港股列 uncovered）**。抽查 3 笔 total_pnl 对上同花顺已清仓。

- [ ] **步骤 4：红线自查**

- `data/user_data/trade_journal/` 下只有 `ledger.json`，无任何 xlsx——R5 ✓
- 代码 grep `ai_provider|generate_ai_text`,`trade_journal/` 目录零命中——R3 ✓
- HK 行 benchmark 为 null、追涨 uncovered——R6/Med1 ✓
- 账户超额非 null（任务 0 回填生效）——High 3 ✓

---

## 显式不做（YAGNI，与 grilling 决策对齐）

- Shadow Account（规则抽取/回放）——独立立项，见 CONTEXT.md。
- 多账本/多次导入合并去重——MVP 单账本覆盖式（grilling Q2：确认单账户 + 全量导出）。
- 送股（除权除息带数量）的股数调整——银河样本无此情况，出现时 parser 会因数量列有值而落到 warning。
- 中证1000/全指/2000 基准——本地未同步（grilling Q4 已核实），基准选项限沪深300/中证500/创业板指/科创50。
- LLM 叙事报告（Hybrid）——后续 opt-in。
- 港股基准——待确认恒指数据后另做。

## 对拍结论（任务 8 执行后回填）

真实样本 `~/Downloads/银河.xlsx` 已执行：

- `sheets=['持仓数据', '已清仓', '交易记录']`
- `fills=1881 events=328 trips=446 oracle=446 open=5`
- `warnings=0 missing=0 extra=0 pnl_diff=0 whitelisted=1`
- 白名单 1 行：`601127.SH 2026-05-28` 周期，我方清仓日 `2026-06-17`，同花顺清仓日 `2026-06-18`，原因是同花顺把股息个税顺延日作为清仓日。
- 口径修正：`Roundtrip.total_pnl = pnl + dividend`；周期内分红计入该周期；清仓后的股息个税归属到最近已平仓周期；若股息税日期等于下一轮建仓日，则不归入下一轮，避免污染新周期。
