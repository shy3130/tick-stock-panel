# C1 Phase 1 — 行为诊断丰富化 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 给 Trade Journal 现有 4 类行为诊断（处置效应/过度交易/追涨买入/浮亏加仓）补上"判定阈值 + 触发明细"的展开展示，全部纯统计、不经 LLM。

**架构：** 后端 `diagnose()` 在现有聚合数字之外，为每类诊断增加一个 `detail` 块（含阈值文案 + 贡献明细列表）；`GET /ledger` 读 `source.json` 的原始 fills 按需重算 diagnosis，使已导入的旧台账无需重新上传即可拿到 detail；前端 `TradeJournal.tsx` 的 4 张诊断卡改为可展开，渲染 detail。

**技术栈：** 后端 Python 3.13 / polars / pytest（`asyncio_mode=auto`）；前端 React + TypeScript + Vite + Tailwind。

## 全局约束（隐私红线，逐字来自已评审 spec，每个任务都隐含包含）

- **行为诊断纯统计，不经 LLM。** `detail` 只做统计聚合，绝不调用任何 AI provider。
- **`methodology_context` 不得持久化进 ledger。** `write_ledger` 现有的剔除逻辑（`api/trade_journal.py:124`）不得破坏。
- **detail 只含用户自己的成交字段（symbol/date/price/持有天数/pos_20d），在用户自己 UI 回显，绝不发送给任何 LLM。** 本阶段不新增任何对外发送。
- **不新增持久化文件。** 只复用现有 `ledger.json` / `source.json`。
- commit 需用户授权（批准本计划=授权）；永不 push。

---

### 任务 1：后端 `diagnose()` 增加 4 类 `detail` 块

**文件：**
- 修改：`backend/app/services/trade_journal/diagnose.py`
- 测试：`backend/tests/services/trade_journal/test_diagnose.py`

**接口：**
- Consumes：`diagnose(trips: list[Roundtrip], fills: list[Fill], price_lookup: dict[tuple[str,str],dict]) -> dict`（签名不变）。`price_lookup[(symbol,date)]` 形如 `{"pos_20d": float, "close": float}`（见 `pricepos.py:45`）。
- Produces（Task 3 前端据此渲染）：返回 dict 的每类诊断新增 `detail` 键：
  - `disposition.detail = {"threshold": str, "win_trips": [{"symbol","name","holding_days","pnl_pct"}], "loss_trips": [同结构]}`（win_trips 按 holding_days 升序取前 10；loss_trips 按 holding_days 降序取前 10）
  - `overtrading.detail = {"threshold": str, "by_month": [{"month": "YYYY-MM", "roundtrips": int}], "fees": float, "abs_pnl": float}`（by_month 按 roundtrips 降序）
  - `chasing.detail = {"threshold": str, "chasing_buys": [{"symbol","date","pos_20d"}]}`（取前 20）
  - `anchoring.detail = {"threshold": str, "loss_adds": [{"symbol","date","price","avg_cost"}]}`（取前 20）

- [ ] **步骤 1：编写失败的测试**

在 `test_diagnose.py` 末尾追加：

```python
def test_disposition_detail_lists_trips_and_threshold():
    trips = [
        _rt("600000.SH", "2024-01-02", "2024-01-03", 1000, 2),
        _rt("600001.SH", "2024-01-02", "2024-01-20", -1000, 15),
    ]
    detail = diagnose(trips, [], {})["disposition"]["detail"]
    assert detail["threshold"]
    assert [t["symbol"] for t in detail["win_trips"]] == ["600000.SH"]
    assert [t["symbol"] for t in detail["loss_trips"]] == ["600001.SH"]
    assert detail["loss_trips"][0]["holding_days"] == 15


def test_overtrading_detail_by_month():
    trips = [
        _rt("600000.SH", "2024-01-02", "2024-01-03", 1000, 2),
        _rt("600001.SH", "2024-01-02", "2024-01-20", -1000, 15),
        _rt("600002.SH", "2024-02-01", "2024-02-05", 500, 4),
    ]
    detail = diagnose(trips, [], {})["overtrading"]["detail"]
    months = {row["month"]: row["roundtrips"] for row in detail["by_month"]}
    assert months == {"2024-01": 2, "2024-02": 1}
    assert detail["by_month"][0]["month"] == "2024-01"  # 按数量降序


def test_chasing_detail_lists_chasing_buys():
    fills = [
        Fill("2024-01-02", "", "600000.SH", "A", "buy", 100, 10.0, -1000.0, 1.0),
        Fill("2024-01-03", "", "600000.SH", "A", "buy", 100, 9.0, -900.0, 1.0),
    ]
    lookup = {
        ("600000.SH", "2024-01-02"): {"pos_20d": 0.95},
        ("600000.SH", "2024-01-03"): {"pos_20d": 0.1},
    }
    detail = diagnose([], fills, lookup)["chasing"]["detail"]
    assert [b["date"] for b in detail["chasing_buys"]] == ["2024-01-02"]
    assert detail["chasing_buys"][0]["pos_20d"] == 0.95


def test_anchoring_detail_lists_loss_adds():
    fills = [
        Fill("2024-01-02", "", "600000.SH", "A", "buy", 100, 10.0, -1000.0, 1.0),
        Fill("2024-01-03", "", "600000.SH", "A", "buy", 100, 9.0, -900.0, 1.0),
    ]
    detail = diagnose([], fills, {})["anchoring"]["detail"]
    assert len(detail["loss_adds"]) == 1
    assert detail["loss_adds"][0]["symbol"] == "600000.SH"
    assert detail["loss_adds"][0]["price"] == 9.0
    assert detail["loss_adds"][0]["avg_cost"] == 10.0
```

- [ ] **步骤 2：运行测试验证失败**

运行：`cd backend && uv run --extra dev pytest tests/services/trade_journal/test_diagnose.py -q`
预期：4 个新测试 FAIL，报 `KeyError: 'detail'`

- [ ] **步骤 3：编写最少实现代码**

改 `diagnose.py`。把 `_anchoring_counts` 改为同时返回明细，并在 `diagnose()` 组装 4 个 `detail`：

```python
"""Trade Journal 纯统计诊断。"""
from __future__ import annotations

from collections import Counter, defaultdict

from app.services.trade_journal.models import Fill, Roundtrip


def diagnose(
    trips: list[Roundtrip],
    fills: list[Fill],
    price_lookup: dict[tuple[str, str], dict] | None = None,
) -> dict:
    price_lookup = price_lookup or {}
    wins = [t for t in trips if t.total_pnl > 0]
    losses = [t for t in trips if t.total_pnl < 0]
    avg_win_hold = _avg([t.holding_days for t in wins])
    avg_loss_hold = _avg([t.holding_days for t in losses])
    hold_ratio = avg_loss_hold / avg_win_hold if avg_win_hold else 0.0

    months = {t.close_date[:7] for t in trips}
    month_count = max(len(months), 1)
    total_pnl_abs = abs(sum(t.total_pnl for t in trips))
    fees = sum(t.fees for t in trips)
    month_counter = Counter(t.close_date[:7] for t in trips)

    buy_fills = [f for f in fills if f.side == "buy"]
    covered_buys = [f for f in buy_fills if (f.symbol, f.date) in price_lookup]
    chasing = [
        f
        for f in covered_buys
        if float(price_lookup[(f.symbol, f.date)].get("pos_20d", 0.0)) > 0.9
    ]

    add_count, loss_add_count, loss_add_items = _anchoring_counts(buy_fills)
    return {
        "disposition": {
            "avg_win_holding_days": avg_win_hold,
            "avg_loss_holding_days": avg_loss_hold,
            "loss_to_win_holding_ratio": hold_ratio,
            "flag": hold_ratio > 1.5,
            "detail": {
                "threshold": "亏损/盈利平均持有天数 > 1.5",
                "win_trips": [_trip_row(t) for t in sorted(wins, key=lambda t: t.holding_days)[:10]],
                "loss_trips": [_trip_row(t) for t in sorted(losses, key=lambda t: t.holding_days, reverse=True)[:10]],
            },
        },
        "overtrading": {
            "monthly_roundtrips": len(trips) / month_count,
            "fee_to_abs_pnl": fees / total_pnl_abs if total_pnl_abs else 0.0,
            "flag": (len(trips) / month_count) > 20 or (fees / total_pnl_abs if total_pnl_abs else 0.0) > 0.2,
            "detail": {
                "threshold": "月均 roundtrip > 20 或 手续费/绝对盈亏 > 0.2",
                "by_month": [
                    {"month": m, "roundtrips": c}
                    for m, c in sorted(month_counter.items(), key=lambda kv: (-kv[1], kv[0]))
                ],
                "fees": fees,
                "abs_pnl": total_pnl_abs,
            },
        },
        "chasing": {
            "covered_buys": len(covered_buys),
            "chasing_buys": len(chasing),
            "ratio": len(chasing) / len(covered_buys) if covered_buys else 0.0,
            "uncovered_buys": len(buy_fills) - len(covered_buys),
            "flag": (len(chasing) / len(covered_buys)) > 0.4 if covered_buys else False,
            "detail": {
                "threshold": "pos_20d>0.9 的买入 占 已覆盖买入 > 0.4",
                "chasing_buys": [
                    {
                        "symbol": f.symbol,
                        "date": f.date,
                        "pos_20d": round(float(price_lookup[(f.symbol, f.date)].get("pos_20d", 0.0)), 4),
                    }
                    for f in chasing[:20]
                ],
            },
        },
        "anchoring": {
            "add_buys": add_count,
            "loss_add_buys": loss_add_count,
            "ratio": loss_add_count / add_count if add_count else 0.0,
            "flag": (loss_add_count / add_count) > 0.5 if add_count else False,
            "detail": {
                "threshold": "亏损加仓次数 / 总加仓次数 > 0.5",
                "loss_adds": loss_add_items[:20],
            },
        },
    }


def _trip_row(t: Roundtrip) -> dict:
    return {
        "symbol": t.symbol,
        "name": t.name,
        "holding_days": t.holding_days,
        "pnl_pct": round(t.pnl_pct, 4),
    }


def _avg(values: list[float | int]) -> float:
    return sum(values) / len(values) if values else 0.0


def _anchoring_counts(fills: list[Fill]) -> tuple[int, int, list[dict]]:
    by_symbol: dict[str, list[Fill]] = defaultdict(list)
    for fill in sorted(fills, key=lambda f: (f.date, f.time)):
        by_symbol[fill.symbol].append(fill)

    adds = 0
    loss_adds = 0
    loss_add_items: list[dict] = []
    for sfills in by_symbol.values():
        qty = 0.0
        cost = 0.0
        for f in sfills:
            if qty > 0:
                adds += 1
                avg_cost = cost / qty
                if f.price and f.price < avg_cost:
                    loss_adds += 1
                    loss_add_items.append({
                        "symbol": f.symbol,
                        "date": f.date,
                        "price": f.price,
                        "avg_cost": round(avg_cost, 4),
                    })
            qty += f.qty
            cost += -f.amount
    return adds, loss_adds, loss_add_items
```

- [ ] **步骤 4：运行测试验证通过**

运行：`cd backend && uv run --extra dev pytest tests/services/trade_journal/test_diagnose.py -q`
预期：全部 PASS（原有 2 个 + 新增 4 个）

- [ ] **步骤 5：Commit**

```bash
git add backend/app/services/trade_journal/diagnose.py backend/tests/services/trade_journal/test_diagnose.py
git commit -m "feat(journal): diagnose() emits per-diagnosis detail blocks"
```

---

### 任务 2：`GET /ledger` 用 source.json 按需重算 diagnosis

**文件：**
- 修改：`backend/app/api/trade_journal.py:128-138`（`get_ledger`）
- 测试：`backend/tests/api/test_trade_journal.py`

**接口：**
- Consumes：`store.read_source(data_dir) -> {"imports":[...], "fills":[asdict(Fill)], "events":[...]} | None`；`build_price_lookup(fills, data_dir) -> (lookup, uncovered)`（`pricepos.py:11`）；`diagnose(trips, fills, lookup)`（Task 1 增强版）。ledger 里的 `trips` 是 `asdict(Roundtrip) | 额外 metric 键` 的 dict 列表。
- Produces：`GET /ledger` 返回体的 `diagnosis` 含 Task 1 的 `detail`（对已导入台账也生效）。

- [ ] **步骤 1：编写失败的测试**

在 `test_trade_journal.py` 末尾追加（复用文件已有的 `_upload(...)` / client fixture 模式——先读该文件顶部现有 helper 名称并对齐；下方假设已有一个提交上传后再 GET /ledger 的路径）：

```python
async def test_get_ledger_recomputes_diagnosis_detail(tmp_path, monkeypatch):
    # 复用本文件已有的上传 helper 完成一次 commit 上传（append=false），
    # 使 source.json / ledger.json 落盘；helper 名称以文件现有为准。
    await _do_commit_upload(tmp_path, monkeypatch)  # 见文件内现有等价 helper

    from app.api.trade_journal import get_ledger
    ledger = get_ledger()
    assert "detail" in ledger["diagnosis"]["disposition"]
    assert "threshold" in ledger["diagnosis"]["disposition"]["detail"]
    # 红线：GET /ledger 重算不得把 methodology_context 写回磁盘
    stored = store.read_ledger(settings.data_dir)
    assert "methodology_context" not in stored
```

> 注：实现前先读 `test_trade_journal.py` 顶部，套用其现有的 monkeypatch（`settings.data_dir`、skill_context）与上传调用方式，把上面的 `_do_commit_upload` 替换为文件里等价的真实调用；不要新造 fixture 体系。

- [ ] **步骤 2：运行测试验证失败**

运行：`cd backend && uv run --extra dev pytest tests/api/test_trade_journal.py::test_get_ledger_recomputes_diagnosis_detail -q`
预期：FAIL，`assert "detail" in ...` KeyError/AssertionError（旧 ledger 的 diagnosis 无 detail，或 get_ledger 未重算）

- [ ] **步骤 3：编写最少实现代码**

改 `get_ledger`（`api/trade_journal.py`）。在返回前，若 source.json 存在则重算 diagnosis：

```python
@router.get("/ledger")
def get_ledger():
    ledger = store.read_ledger(settings.data_dir)
    if ledger is None:
        raise HTTPException(status_code=404, detail="尚未导入交易复盘台账")

    # 用 source.json 原始 fills 按需重算 diagnosis，使旧台账也拿到 detail。
    # 纯统计、不落盘、不经 LLM。
    source = store.read_source(settings.data_dir)
    if source and source.get("fills"):
        from dataclasses import fields as _dc_fields
        fills = [Fill(**row) for row in source["fills"]]
        rt_field_names = {f.name for f in _dc_fields(Roundtrip)}
        trips = [
            Roundtrip(**{k: v for k, v in t.items() if k in rt_field_names})
            for t in ledger.get("trips", [])
        ]
        price_lookup, _ = build_price_lookup(fills, settings.data_dir)
        ledger["diagnosis"] = diagnose(trips, fills, price_lookup)

    from app.services.skill_context import load_skill_context_safe

    methodology_context = load_skill_context_safe("trade_journal", max_chars=4000, warnings=ledger.setdefault("warnings", []))
    if methodology_context:
        ledger["methodology_context"] = methodology_context
    return ledger
```

确认文件顶部已 import 了 `Fill`、`Roundtrip`、`build_price_lookup`、`diagnose`、`store`、`settings`（`Fill`/`Roundtrip` 来自 `app.services.trade_journal.models`；若缺 `Roundtrip` 则补 import）。`get_ledger` 只往内存 `ledger` 里塞重算结果，**不调用 `store.write_ledger`**，故不落盘、红线不破。

- [ ] **步骤 4：运行测试验证通过**

运行：`cd backend && uv run --extra dev pytest tests/api/test_trade_journal.py -q`
预期：全部 PASS（含原有 methodology_context 红线断言与新增重算断言）

- [ ] **步骤 5：Commit**

```bash
git add backend/app/api/trade_journal.py backend/tests/api/test_trade_journal.py
git commit -m "feat(journal): GET /ledger recomputes enriched diagnosis from source.json"
```

---

### 任务 3：前端诊断卡可展开渲染 detail

**文件：**
- 修改：`frontend/src/pages/TradeJournal.tsx`（`Diagnosis` 组件 `:260-267` + 4 个调用点 `:212-215`）

**接口：**
- Consumes：`ledger.diagnosis.{disposition,overtrading,chasing,anchoring}.detail`（Task 1 定义的形状）。

- [ ] **步骤 1：改造 `Diagnosis` 组件支持展开 detail**

把 `Diagnosis`（`:260-267`）替换为：

```tsx
function Diagnosis({ title, flag, value, detail }: { title: string; flag?: boolean; value: string; detail?: any }) {
  const rows: { label: string; items: any[]; cols: { key: string; head: string; fmt?: (v: any) => string }[] }[] = []
  if (detail?.win_trips || detail?.loss_trips) {
    rows.push({ label: '盈利单(短持有)', items: detail.win_trips ?? [], cols: [
      { key: 'symbol', head: '代码' }, { key: 'holding_days', head: '持有天' }, { key: 'pnl_pct', head: '收益率', fmt: (v) => pct(v) },
    ] })
    rows.push({ label: '亏损单(长持有)', items: detail.loss_trips ?? [], cols: [
      { key: 'symbol', head: '代码' }, { key: 'holding_days', head: '持有天' }, { key: 'pnl_pct', head: '收益率', fmt: (v) => pct(v) },
    ] })
  }
  if (detail?.by_month) {
    rows.push({ label: '各月 roundtrip', items: detail.by_month, cols: [
      { key: 'month', head: '月份' }, { key: 'roundtrips', head: '次数' },
    ] })
  }
  if (detail?.chasing_buys) {
    rows.push({ label: '追涨买入(pos_20d>0.9)', items: detail.chasing_buys, cols: [
      { key: 'symbol', head: '代码' }, { key: 'date', head: '日期' }, { key: 'pos_20d', head: '分位', fmt: (v) => pct(v) },
    ] })
  }
  if (detail?.loss_adds) {
    rows.push({ label: '亏损加仓', items: detail.loss_adds, cols: [
      { key: 'symbol', head: '代码' }, { key: 'date', head: '日期' }, { key: 'price', head: '加仓价' }, { key: 'avg_cost', head: '持仓成本' },
    ] })
  }
  return (
    <div className="rounded-card border border-border bg-base p-3">
      <div className="text-xs text-muted">{title}</div>
      <div className={flag ? 'mt-1 text-sm font-semibold text-bear' : 'mt-1 text-sm font-semibold text-foreground'}>{value}</div>
      {detail?.threshold && (
        <details className="mt-2">
          <summary className="cursor-pointer text-[11px] text-muted">判定依据 / 明细</summary>
          <div className="mt-1 text-[11px] text-muted">阈值：{detail.threshold}</div>
          {rows.map((r, ri) => (
            <div key={ri} className="mt-2">
              <div className="text-[11px] text-secondary">{r.label}（{r.items.length}）</div>
              {r.items.length > 0 && (
                <table className="mt-1 min-w-full text-left text-[10px]">
                  <thead className="text-muted"><tr>{r.cols.map(c => <th key={c.key} className="px-1 py-0.5">{c.head}</th>)}</tr></thead>
                  <tbody>
                    {r.items.slice(0, 10).map((it, ii) => (
                      <tr key={ii}>{r.cols.map(c => <td key={c.key} className="px-1 py-0.5">{c.fmt ? c.fmt(it[c.key]) : String(it[c.key] ?? '')}</td>)}</tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          ))}
        </details>
      )}
    </div>
  )
}
```

- [ ] **步骤 2：4 个调用点传入 detail**

把 `:212-215` 四行改为传 `detail`：

```tsx
          <Diagnosis title="处置效应" flag={d.disposition?.flag} value={`${num(d.disposition?.loss_to_win_holding_ratio)}x`} detail={d.disposition?.detail} />
          <Diagnosis title="过度交易" flag={d.overtrading?.flag} value={`${num(d.overtrading?.monthly_roundtrips)} / 月`} detail={d.overtrading?.detail} />
          <Diagnosis title="追涨买入" flag={d.chasing?.flag} value={pct(d.chasing?.ratio)} detail={d.chasing?.detail} />
          <Diagnosis title="浮亏加仓" flag={d.anchoring?.flag} value={pct(d.anchoring?.ratio)} detail={d.anchoring?.detail} />
```

- [ ] **步骤 3：类型检查**

运行：`cd frontend && pnpm tsc --noEmit`
预期：exit 0，无报错

- [ ] **步骤 4：构建验证**

运行：`cd frontend && pnpm build`
预期：成功（仅既有 chunk-size / dynamic import 警告）

- [ ] **步骤 5：Commit**

```bash
git add frontend/src/pages/TradeJournal.tsx
git commit -m "feat(journal): expandable diagnosis cards with detail tables"
```

---

## 自检

**1. 规格覆盖度：** spec「子项目3」阶段1要求"丰富现有 4 类诊断的展示细节/解释/触发明细（展开每类的判定依据、涉及的具体 roundtrip 列表、阈值说明）"——Task 1 产出 4 类 detail（阈值+贡献明细），Task 3 展开渲染，Task 2 让旧台账也生效。覆盖。

**2. 占位符扫描：** Task 2 步骤 1 的测试 helper 名称留了"以文件现有为准"的替换说明（因未逐行读 test 文件顶部），非代码占位——已明确要求实现者对齐现有 helper，不新造体系。其余步骤均含完整实际代码。

**3. 类型一致性：** `detail` 形状在 Task 1 的 Produces 定义，Task 3 按同名键（win_trips/loss_trips/by_month/chasing_buys/loss_adds/threshold）消费；`_anchoring_counts` 返回值从 2 元组改 3 元组，唯一调用点在 `diagnose()` 内同步更新。一致。

**4. 红线：** 无 LLM 调用；`get_ledger` 不 `write_ledger`；detail 仅统计字段。三条红线在 Task 2 步骤 3 显式保持，并有测试断言 `methodology_context not in stored`。
