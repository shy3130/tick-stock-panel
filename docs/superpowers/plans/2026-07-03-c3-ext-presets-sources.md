# C3：A 股参考数据剩余源接 ext_presets 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 按优先序接入 6 个东财参考数据源到扩展数据（ext_data）：解禁 → 股东户数 → 两融 → 大宗 → 研报/EPS → 新闻。北向资金**已剔除**（2024-08 起停止披露日度净流入，R7 裁决）。

**架构：** 完全复用 P1 龙虎榜模式：`ExtConfig` 预设 + 「接口结构→本地 schema」flatten + `rows_to_parquet` 落盘 + 前端扩展数据页手动"获取数据"。新源的 preset/flatten/fetcher 放独立模块 `ext_presets_em.py`（`ext_presets.py` 已 324 行，避免膨胀），由 `ext_presets.py` 的 dispatch 表聚合。接口配方**全部取自 Vibe-Trading 已验证实现**（文件路径见各任务），不凭空猜字段。

**技术栈：** Python 3.12 / httpx / polars。测试 `cd backend && uv run --extra dev pytest`。

**现状证据：**
- `backend/app/services/ext_presets.py` 已有 P1 龙虎榜预设、fetch/flatten/落盘入口，C3 应复用这套 `ExtConfig`/`fetch_preset`/`rows_to_parquet` 管道。
- `backend/app/services/eastmoney_client.py` 当前只有基础 `get_json` 和全局节流；接 6 个 EM 源前必须先补 host 白名单、per-host 节流和 datacenter 翻页。
- `write_ext_parquet` 的去重键会把有 `symbol` 列的数据按 symbol 去重；所有 EM 预设统一用 `stock_symbol` + 首列 `uid`，避免新闻/大宗/研报这类一股多行被误去重。
- Vibe-Trading 已有 6 类接口配方，本计划只搬接口结构和字段 flatten，不搬运行时/代理/外部执行框架。

**范围决策：**
- **全市场按日拉取**：解禁（filter FREE_DATE 区间）、股东户数（最新披露快照）、大宗（filter TRADE_DATE）。
- **自选股（watchlist）范围逐股拉取**：两融明细、研报/EPS、新闻——这三个接口本质是 per-stock 查询（Vibe 亦如此用），全市场遍历既慢又易触限流；自选股范围即覆盖用户真实需求。
- 每源独立 commit，随做随用；做完前 3 个即可交付一版。

**已知坑（设计期已定对策）：**
1. `write_ext_parquet` 的去重键硬编码为"有 symbol 列则按 symbol"（`ext_data.py:408,421`）。新闻/大宗/研报都可能一股一天多行，若含 `symbol` 列会被去重成 1 行——EM 预设统一**不设 `symbol` 列**，首列放稳定唯一键 `uid`，个股关联用 `stock_symbol`，`symbol_map={"type":"mapped","col":"stock_symbol"}`。
2. timeseries 模式按 `snapshot_date` 分区（`ext_data.py:412-425`）；对两融/大宗等"一次拉多天"的数据，**按数据自身日期分组、逐日期调用 `rows_to_parquet(snapshot_date=该日期)`**，不要全塞进拉取当天分区。
3. EM datacenter `pageSize` 上限 500，全市场源必须翻页循环（任务 0 的 `get_datacenter_paged`）。

---

## 文件结构

| 文件 | 职责 | 改动 |
|---|---|---|
| `backend/app/services/eastmoney_client.py` | EM HTTP 助手 | 升级：host 白名单 + per-host 节流 + 翻页助手 |
| `backend/app/services/ext_presets_em.py` | 6 个新源的 preset/flatten/fetcher | 创建 |
| `backend/app/services/ext_presets.py` | 预设注册与 dispatch | `_presets()`/`fetch_preset` 改 dispatch 表并聚合新模块 |
| `backend/tests/services/test_ext_presets_em.py` | flatten 黄金 fixture 测试 | 创建 |
| `backend/tests/services/fixtures/em_*.json` | 各源真实响应样本 | 逐源创建 |

---

### 任务 0：eastmoney_client 加固（白名单 + per-host 节流 + 翻页）

**文件：**
- 修改：`backend/app/services/eastmoney_client.py`（现 34 行，全局单锁 0.35s 节流已有）
- 测试：`backend/tests/services/test_eastmoney_client.py`

- [x] **步骤 1：编写失败的测试**

```python
# backend/tests/services/test_eastmoney_client.py
import pytest

from app.services import eastmoney_client as em


def test_disallowed_host_rejected():
    with pytest.raises(ValueError, match="host not allowed"):
        em.get_json("https://evil.example.com/api")


def test_allowed_hosts_pass_validation():
    for url in (
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        "https://reportapi.eastmoney.com/report/list",
        "https://search-api-web.eastmoney.com/search/jsonp",
    ):
        em._check_host(url)  # 不抛即通过


def test_paged_fetch_merges_pages(monkeypatch):
    pages = {
        1: {"result": {"pages": 2, "data": [{"i": 1}]}},
        2: {"result": {"pages": 2, "data": [{"i": 2}]}},
    }

    def fake_get_json(url, params=None):
        return pages[int(params["pageNumber"])]

    monkeypatch.setattr(em, "get_json", fake_get_json)
    rows = em.get_datacenter_paged("https://datacenter-web.eastmoney.com/api/data/v1/get",
                                   {"reportName": "X"}, max_pages=5)
    assert [r["i"] for r in rows] == [1, 2]
```

- [x] **步骤 2：运行验证失败**

运行：`cd backend && uv run --extra dev pytest tests/services/test_eastmoney_client.py -v`
预期：FAIL（`_check_host`/`get_datacenter_paged` 不存在）

- [x] **步骤 3：实现（参考 Vibe `agent/backtest/loaders/_http.py:46` 的 HostThrottle 语义：per-bucket 最小间隔+抖动；现文件的全局锁节流升级为按 host 分桶）**

```python
"""Tiny Eastmoney HTTP helper for ext presets."""
from __future__ import annotations

import random
import threading
import time
from typing import Any
from urllib.parse import urlparse

import httpx

_ALLOWED_HOSTS = {
    "datacenter-web.eastmoney.com",
    "reportapi.eastmoney.com",
    "search-api-web.eastmoney.com",
    "files.688798.xyz",           # ths 概念/行业种子源（既有）
}

_LOCK = threading.Lock()
_LAST_TS: dict[str, float] = {}
_MIN_INTERVAL = 0.35
_JITTER = 0.15


def _check_host(url: str) -> None:
    host = urlparse(url).hostname or ""
    if host not in _ALLOWED_HOSTS:
        raise ValueError(f"host not allowed: {host}")


def _throttle(host: str) -> None:
    with _LOCK:
        now = time.monotonic()
        wait = _MIN_INTERVAL + random.uniform(0, _JITTER) - (now - _LAST_TS.get(host, 0.0))
        fire_at = now + max(wait, 0.0)
        _LAST_TS[host] = fire_at
    if wait > 0:
        time.sleep(wait)


def get_json(url: str, params: dict[str, Any] | None = None) -> dict:
    _check_host(url)
    _throttle(urlparse(url).hostname or "")
    resp = httpx.get(
        url,
        params=params or {},
        timeout=10.0,
        headers={"User-Agent": "Mozilla/5.0"},
        trust_env=False,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError(f"Eastmoney response is not object: {type(data)}")
    return data


def get_datacenter_paged(url: str, params: dict[str, Any], max_pages: int = 20) -> list[dict]:
    """datacenter-web 翻页拉取：循环 pageNumber 直到 result.pages 或 max_pages。"""
    rows: list[dict] = []
    page = 1
    while page <= max_pages:
        payload = get_json(url, params={**params, "pageNumber": str(page), "pageSize": "500"})
        result = payload.get("result") if isinstance(payload, dict) else None
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list) or not data:
            break
        rows.extend(data)
        total_pages = int(result.get("pages") or 1)
        if page >= total_pages:
            break
        page += 1
    return rows
```

（注意：原 `get_json` 的调用方——`ext_presets._seed_dragon_tiger`、Vibe 式新 fetcher——签名不变，行为兼容。`files.688798.xyz` 走 `_fetch_json`(httpx 直连) 的概念/行业预设不经此模块，白名单仅为防御未来接入。）

- [x] **步骤 4：运行测试验证通过 + 全量回归 + Commit**

```bash
cd backend && uv run --extra dev pytest tests/services/test_eastmoney_client.py -v && uv run --extra dev pytest -q
git add -A && git commit -m "feat(ext): harden eastmoney client with host allowlist, per-host throttle, pagination"
```

---

### 任务 0.5：ext_presets dispatch 表化（为 6 源接入做位）

**文件：**
- 修改：`backend/app/services/ext_presets.py:127-128,302-324`

- [x] **步骤 1：改造 `_presets()` 与 `fetch_preset`**

```python
# ext_presets.py 顶部补
from typing import Awaitable, Callable

# id → (ExtConfig 工厂, 拉取器)。拉取器签名: async (config, data_dir) -> int
_REGISTRY: dict[str, tuple[Callable[[], ExtConfig], Callable[..., Awaitable[int]]]] = {}


def register_preset(factory: Callable[[], ExtConfig],
                    fetcher: Callable[..., Awaitable[int]]) -> None:
    cfg = factory()
    _REGISTRY[cfg.id] = (factory, fetcher)
```

`_presets()` 改为：

```python
def _presets() -> list[ExtConfig]:
    _ensure_registered()
    return [factory() for factory, _ in _REGISTRY.values()]


def _ensure_registered() -> None:
    if _REGISTRY:
        return
    register_preset(_concept_preset, lambda c, d: _seed_one(c, _flatten_concept_rows, d))
    register_preset(_industry_preset, lambda c, d: _seed_one(c, _flatten_industry_rows, d))
    register_preset(_dragon_tiger_preset, _seed_dragon_tiger)
    from app.services import ext_presets_em
    ext_presets_em.register_all(register_preset)
```

`fetch_preset` 末段 if/elif 改为：

```python
    _ensure_registered()
    _factory, fetcher = _REGISTRY[config_id]
    n = await fetcher(config, data_dir)
```

- [x] **步骤 2：创建空壳 `ext_presets_em.py`**

```python
# backend/app/services/ext_presets_em.py
"""东财参考数据预设（C3）：解禁/股东户数/两融/大宗/研报EPS/新闻。"""
from __future__ import annotations


def register_all(register) -> None:  # 各任务逐源填充
    pass
```

- [x] **步骤 3：回归（既有 3 预设行为不变）+ Commit**

```bash
cd backend && uv run --extra dev pytest -q && uv run python -c "
import asyncio; from app.services.ext_presets import get_preset
assert get_preset('ext_lhb_em') is not None; print('ok')"
git add -A && git commit -m "refactor(ext): registry-based preset dispatch, em presets module stub"
```

---

### 任务 1：解禁 `ext_lockup_em`（全市场，timeseries 按 FREE_DATE 分区）

**接口配方（源：Vibe `agent/src/tools/lockup_expiry_tool.py:30-37,200-203`）：** datacenter `reportName=RPT_LIFT_STOCK`，columns `SECURITY_CODE,SECURITY_NAME_ABBR,FREE_DATE,FREE_SHARES_TYPE,FREE_SHARES,CURRENT_FREE_SHARES,ABLE_FREE_SHARES,LIFT_MARKET_CAP,FREE_RATIO,TOTAL_RATIO`，filter `(FREE_DATE>='{start}')(FREE_DATE<='{end}')`，sortColumns `FREE_DATE`。默认窗口：今天 → +30 天（连板/短线风险前瞻）。

**文件：**
- 修改：`backend/app/services/ext_presets_em.py`
- 测试：`backend/tests/services/test_ext_presets_em.py` + fixture `backend/tests/services/fixtures/em_lockup.json`

- [x] **步骤 1：抓真实响应落 fixture（先跑接口，字段以实测为准）**

```bash
cd backend && uv run python - <<'EOF'
import json
from app.services import eastmoney_client as em
rows = em.get_datacenter_paged(
    "https://datacenter-web.eastmoney.com/api/data/v1/get",
    {"reportName": "RPT_LIFT_STOCK",
     "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,FREE_DATE,FREE_SHARES_TYPE,FREE_SHARES,CURRENT_FREE_SHARES,ABLE_FREE_SHARES,LIFT_MARKET_CAP,FREE_RATIO,TOTAL_RATIO",
     "filter": "(FREE_DATE>='2026-07-03')(FREE_DATE<='2026-08-02')",
     "sortColumns": "FREE_DATE", "sortTypes": "1", "source": "WEB", "client": "WEB"},
    max_pages=2)
print(len(rows)); json.dump(rows[:5], open("tests/services/fixtures/em_lockup.json", "w"), ensure_ascii=False, indent=1)
EOF
```
预期：行数 > 0；打开 fixture 核对字段名与上述 columns 一致（不一致以实测为准并回改本计划代码）。

- [x] **步骤 2：编写失败的测试**

```python
# backend/tests/services/test_ext_presets_em.py
import json
from pathlib import Path

from app.services import ext_presets_em as em

FIXTURES = Path(__file__).parent / "fixtures"


def test_flatten_lockup_rows():
    raw = json.loads((FIXTURES / "em_lockup.json").read_text())
    rows = em._flatten_lockup_rows(raw)
    assert rows, "fixture 不应 flatten 成空"
    r = rows[0]
    assert set(r) == {"symbol", "code", "free_date", "name", "shares_type",
                      "free_shares", "lift_market_cap", "free_ratio", "total_ratio"}
    assert r["symbol"].endswith((".SH", ".SZ", ".BJ"))
    assert len(r["free_date"]) == 10  # YYYY-MM-DD
```

- [x] **步骤 3：运行验证失败**（`_flatten_lockup_rows` 不存在）

- [x] **步骤 4：实现 preset + flatten + fetcher**

在 `ext_presets_em.py` 填充：

```python
import logging
from datetime import date, timedelta
from pathlib import Path

from app.services.ext_data import ExtConfig, ExtField, PullConfig, rows_to_parquet

logger = logging.getLogger(__name__)

_EM_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _a_symbol(code: str) -> str:
    code = str(code or "").strip()
    if code.startswith(("8", "4", "92")):
        return f"{code}.BJ"
    if code.startswith(("60", "68", "90", "11", "13")):
        return f"{code}.SH"
    return f"{code}.SZ"


def _lockup_preset() -> ExtConfig:
    return ExtConfig(
        id="ext_lockup_em",
        label="限售解禁",
        mode="timeseries",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("code", "string", "代码"),
            ExtField("free_date", "string", "解禁日期"),
            ExtField("name", "string", "名称"),
            ExtField("shares_type", "string", "解禁股类型"),
            ExtField("free_shares", "float", "解禁股数"),
            ExtField("lift_market_cap", "float", "解禁市值"),
            ExtField("free_ratio", "float", "占流通比%"),
            ExtField("total_ratio", "float", "占总股本比%"),
        ],
        description="东方财富限售解禁（未来30天窗口，手动获取）",
        symbol_map={"type": "mapped", "col": "symbol"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(url=_EM_DATACENTER, method="GET", schedule_minutes=1440, enabled=False),
    )


def _flatten_lockup_rows(raw_rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in raw_rows:
        code = str(r.get("SECURITY_CODE") or "").strip()
        if not code:
            continue
        out.append({
            "symbol": _a_symbol(code),
            "code": code,
            "free_date": str(r.get("FREE_DATE") or "")[:10],
            "name": r.get("SECURITY_NAME_ABBR") or "",
            "shares_type": r.get("FREE_SHARES_TYPE") or "",
            "free_shares": r.get("ABLE_FREE_SHARES"),
            "lift_market_cap": r.get("LIFT_MARKET_CAP"),
            "free_ratio": r.get("FREE_RATIO"),
            "total_ratio": r.get("TOTAL_RATIO"),
        })
    return out


async def _fetch_lockup(config: ExtConfig, data_dir: Path) -> int:
    from app.services import eastmoney_client

    start, end = date.today(), date.today() + timedelta(days=30)
    raw = eastmoney_client.get_datacenter_paged(_EM_DATACENTER, {
        "reportName": "RPT_LIFT_STOCK",
        "columns": ("SECURITY_CODE,SECURITY_NAME_ABBR,FREE_DATE,FREE_SHARES_TYPE,"
                    "FREE_SHARES,CURRENT_FREE_SHARES,ABLE_FREE_SHARES,LIFT_MARKET_CAP,"
                    "FREE_RATIO,TOTAL_RATIO"),
        "filter": f"(FREE_DATE>='{start}')(FREE_DATE<='{end}')",
        "sortColumns": "FREE_DATE", "sortTypes": "1", "source": "WEB", "client": "WEB",
    })
    rows = _flatten_lockup_rows(raw)
    if not rows:
        raise ValueError("解禁接口返回 0 行")
    # 按解禁日期分区写（坑2 对策）
    total = 0
    by_date: dict[str, list[dict]] = {}
    for r in rows:
        by_date.setdefault(r["free_date"], []).append(r)
    for ds, chunk in sorted(by_date.items()):
        total += rows_to_parquet(chunk, config, data_dir, snapshot_date=date.fromisoformat(ds))
    return total


def register_all(register) -> None:
    register(_lockup_preset, _fetch_lockup)
```

- [x] **步骤 5：运行测试通过 + 手动全链路**

```bash
cd backend && uv run --extra dev pytest tests/services/test_ext_presets_em.py -v
uv run python - <<'EOF'
import asyncio
from pathlib import Path
from app.config import settings
from app.services.ext_presets import fetch_preset
print(asyncio.run(fetch_preset("ext_lockup_em", Path(settings.data_dir))))
EOF
```
预期：写入行数 > 0；`data/ext_data/ext_lockup_em/timeseries/date=*/part.parquet` 生成；前端扩展数据页可见该表。

- [x] **步骤 6：Commit** `git commit -am "feat(ext): lockup expiry preset ext_lockup_em (C3-1)"`

---

### 任务 2：股东户数 `ext_holder_em`（全市场最新披露，timeseries 按 END_DATE 分区）

**接口配方（源：Vibe `agent/src/tools/shareholder_count_tool.py:24-31`）：** `reportName=RPT_HOLDERNUMLATEST`，columns `SECUCODE,SECURITY_CODE,END_DATE,HOLDER_NUM,HOLDER_NUM_CHANGE,HOLDER_NUM_RATIO,AVG_HOLD_AMT,AVG_HOLD_NUM,TOTAL_MARKET_CAP`，sortColumns `END_DATE`，全市场翻页（约 5000+ 行 → max_pages=15）。

**步骤模板同任务 1（fixture → 失败测试 → 实现 → 全链路 → commit），差异点：**

- [x] **步骤 1：fixture** `em_holder_count.json`（同款探测脚本，reportName/columns 换上面的，无 filter）
- [x] **步骤 2：测试** `test_flatten_holder_maps_core_fields`：断言 `stock_symbol/code/end_date/holder_count/holder_count_change/holder_count_change_pct/avg_hold_amount/avg_hold_shares/total_market_cap`。
- [x] **步骤 3：实现**：preset `id="ext_holder_em"`, label 股东户数, **mode="timeseries"**；`uid + stock_symbol` 作为稳定标识，按 `end_date` 分区写入；flatten 直接字段搬运（END_DATE 截 10 位）；fetcher 用 `get_datacenter_paged(..., max_pages=20)`，无日期 filter。
- [x] **步骤 4：全链路** `fetch_preset("ext_holder_em", ...)` 行数 ≳ 5000
- [x] **步骤 5：Commit** `git commit -am "feat(ext): shareholder count preset (C3-2)"`

---

### 任务 3：两融明细 `ext_margin_em`（自选股范围，timeseries 按 DATE 分区）

**接口配方（源：Vibe `agent/src/tools/margin_trading_tool.py:29-46,187-191`）：** `reportName=RPTA_WEB_RZRQ_GGMX`，columns `ALL`，filter `(SCODE="600519")`，sortColumns `DATE` desc，取近 30 天。字段映射：DATE→trade_date、RZYE→financing_balance、RZMRE→financing_buy、RZCHE→financing_repay、RQYE→short_balance、RQYL→short_volume、RZRQYE→margin_total_balance。

- [x] **步骤 1：fixture** `em_margin.json`（探测 600519）
- [x] **步骤 2：测试** `test_flatten_margin_rows`：键集 `{"symbol","code","trade_date","financing_balance","financing_buy","financing_repay","short_balance","short_volume","margin_total_balance"}`
- [x] **步骤 3：实现**：preset `id="ext_margin_em"`, mode="timeseries"；fetcher 遍历 watchlist（读法抄 `app/services/extend_history.py:_load_watchlist_symbols`，即 `data/user_data/watchlist.parquet` 的 symbol 列），仅 A 股后缀（.SH/.SZ/.BJ），逐股 `filter=(SCODE="{六位code}")`、每股 1 页 pageSize=30；聚所有股票行后按 trade_date 分组逐日期 `rows_to_parquet`（分区内一 symbol 一行，去重键成立）。watchlist 为空时抛 `ValueError("自选股为空，两融明细按自选股拉取")`。
- [x] **步骤 4：全链路 + Commit** `git commit -am "feat(ext): margin trading preset, watchlist-scoped (C3-3)"`

---

### 任务 4：大宗交易 `ext_block_em`（全市场按日，timeseries）

**接口配方（源：Vibe `agent/src/tools/block_trades_tool.py:30-44,243-246`）：** `reportName=RPT_DATA_BLOCKTRADE`，columns `TRADE_DATE,SECURITY_CODE,SECURITY_NAME_ABBR,CLOSE_PRICE,DEAL_PRICE,PREMIUM_RATIO,DEAL_VOLUME,DEAL_AMT,BUYER_NAME,SELLER_NAME`，filter `(TRADE_DATE='{今天}')`，sortColumns `DEAL_AMT` desc。

- [x] **步骤 1-3：** fixture `em_block_trade.json` → 测试键集 `{"uid","stock_symbol","code","trade_date","name","close_price","deal_price","premium_ratio","deal_volume","deal_amount","buyer_seat","seller_seat"}` → 实现（mode="timeseries"，fetcher 拉近 30 天并按 `trade_date` 分区；**同一 symbol 当天可有多笔大宗，必须保留明细行**，`uid=block:{code}:{TRADE_DATE}:{DEAL_PRICE}:{DEAL_AMT}` 防止按 symbol 去重塌行。测试覆盖两笔同 symbol 生成两个 uid。）
- [x] **步骤 4：全链路 + Commit** `git commit -am "feat(ext): block trade preset with per-symbol aggregation (C3-4)"`

---

### 任务 5：研报/EPS `ext_research_em`（自选股范围，timeseries 按 publish_date 分区）

**接口配方（源：Vibe `agent/src/tools/research_reports_tool.py:40,110-135`）：** `https://reportapi.eastmoney.com/report/list?code={六位code}&qType=0&pageSize=10&pageNo=1`。返回 data 数组含 title/orgSName/publishDate/rating（实测字段以 fixture 为准）。THS 一致预期（`basic.10jqka.com.cn`）**不接**——跨站 Referer 伪装不符合 panel 合规基线，EPS 一致预期字段用 reportapi 返回里的预测字段（fixture 核实后定列）。

- [x] **步骤 1：fixture** `em_research.json`（探测 600519，记录完整字段）
- [x] **步骤 2：测试**：键集（以 fixture 定稿为准）`{"uid","stock_symbol","code","publish_date","title","brokerage","analyst","rating","eps_this_year","eps_next_year","pe_this_year","pe_next_year"}`；EPS/PE 字段缺失时容 None。
- [x] **步骤 3：实现**：preset `id="ext_research_em"`, mode="timeseries"；fetcher 遍历 watchlist A 股逐股拉取最新 N=10 篇，按 `publish_date` 分区写入，`uid=research:{code}:{infoCode/title}:{publish_date}` 保留逐篇研报。白名单已含 reportapi（任务 0）。
- [x] **步骤 4：全链路 + Commit** `git commit -am "feat(ext): research/EPS consensus preset, watchlist-scoped (C3-5)"`

---

### 任务 6：新闻 `ext_news_em`（自选股范围，timeseries，uid 保留多篇）

**接口配方（源：Vibe `agent/src/tools/stock_news_tool.py:42,170-193`）：** `https://search-api-web.eastmoney.com/search/jsonp`，param 为 JSON 字符串（keyword=股票名或代码，type=cmsArticleWebOld，pageSize=limit），`cb=""` 时仍可能包 jsonp 壳——移植 Vibe `_decode_jsonp`（同文件内）。

- [x] **步骤 1：fixture** `em_news.json`（探测"贵州茅台"）
- [x] **步骤 2：测试**：`test_flatten_news_trims_content` 键集 `{"uid","stock_symbol","code","published","title","source","url","snippet"}`；断言 `uid` 来自 art_code/url/title，`stock_symbol` 保留标的映射，摘要截断到 280 字符。
- [x] **步骤 3：实现**：preset `id="ext_news_em"`, mode="timeseries"；fields 首列 `uid`，`symbol_map={"type":"mapped","col":"stock_symbol"}`；fetcher 遍历 watchlist、每股 pageSize=10，按 `published` 分区写。
- [x] **步骤 4：全链路验证分区内多篇文章共存（同一 `stock_symbol` 可有多个 `uid`）+ Commit** `git commit -am "feat(ext): per-stock news preset with uid dedup key (C3-6)"`

---

### 任务 7：收尾回归

- [x] 后端目标测试覆盖 6 个 EM preset 注册与 flatten；前端扩展数据页复用现有 ext_data UI。
- [x] `docs/vibe-trading-migration-candidates.md` C3 条目已记录候选与"北向已剔除"。
- [x] Commit：`git commit -am "docs: mark C3 sources landed"`
