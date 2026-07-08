# tickflow-stock-panel fstore/engine DuckDB 数据源接入实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 `FQuantProvider` 里现在直连 fstore PostgreSQL（`pve.wf:5432/fstore`）的只读查询，可以按环境变量切换为读取本机 `/Volumes/WD1/fstore.duckdb`；同时让 `self._engine`（现在只有 `http`/`disk` 两种 `engine_mode`）多一个 `duckdb` 选项，完整读取本机 `tdx.duckdb`/`tdx-minutes.duckdb`/`tdx-trans.duckdb` 三个文件，覆盖 `get_day`/`get_wide`/`get_minutes`/`get_trans`/`get_xdxr` 全部五个数据集。两个开关都默认关闭（保持现状行为），出问题可以立即改回默认值回退。

**架构：** 新增 `FStoreDuckDBClient`，对外暴露和 `FStoreClient` 完全相同的 `query(sql, params) -> list[dict]` 接口，内部把调用方已经写好的 `%s` 占位符 SQL 翻译成 DuckDB 的 `?` 占位符再执行——**`fquant_provider.py` 里绝大多数直连 fstore 的方法因此不需要改一行代码**，只需要把 `FQuantProvider.__init__` 里 `self._fstore = FStoreClient()` 换成按环境变量选择的工厂函数。唯一必须改代码的例外是 `_get_fstore_realtime`：它查询的 `t_{asset_type}_daily_markets` 分表在 fstore.duckdb 里并不存在（这一族表在迁移时被合并成了统一长表 `daily_markets`，其余表都保留了原表名），需要单独按方言分支重写。

engine 侧新增 `EngineDataDuckDBClient`，和 `EngineDataClient`/`EngineDataDiskClient` 一样实现完整的 `get_day/get_wide/get_minutes/get_trans/get_xdxr` 契约（不再是"只实现一个方法"的降级版）——这是相对 2026-07-06 版本的实质性变化，原因见下面"背景核实"一节：当时 `engine.duckdb` 的 `market_day_kline`/`market_minutes` 还是空表，只有 `market_transactions` 有数据，所以只做了 `get_trans` 一个方法；现在 engine 侧把 `engine.duckdb` 拆分重组成了 `tdx.duckdb`（day/wide K线、xdxr）+ `tdx-minutes.duckdb`（分钟）+ `tdx-trans.duckdb`（逐笔）三个文件，五个数据集全部有真实数据，具备实现完整契约的条件。即便如此，仍然不直接接入 `engine_mode` 或 `registry.py` 的 provider 选择机制——`engine_mode`/`DATA_PROVIDER` 是面向用户的 provider 概念（`fquant`/`fquant_local`），而这里只是同一份 TDX 数据的第三种底层读取方式，用独立的 `FQUANT_ENGINE_DATA_SOURCE` 环境变量控制，不应该让用户在"选 provider"这个心智模型里再多选一个选项。

不属于本计划范围、明确不动的部分：`EngineDataDiskClient`（TDX 磁盘 CSV，`docs/data-query-inventory-local-source.md` 里已经在推进的另一条独立的"本地磁盘源"迁移路线，处理的是完全不同的上游）、`MoneyflowClient`、`registry.py`/`preferences.py` 的 provider 白名单和前端下拉。

**技术栈：** Python 3.13、`duckdb`（已是依赖，`pyproject.toml:18` 锁定 `>=1.0`，当前环境装的是 1.5.3）、`psycopg`/`psycopg2`（现状不变）、`pytest`。

---

## 2026-07-07 修订说明

写完这份计划的第二天，DuckDB 文件分布发生了几处变化：

1. `fdata-store.duckdb` 已改名为 `/Volumes/WD1/fstore.duckdb`——正文路径引用已全部替换。任务 1-4（fstore 侧）的结论和代码本身不受影响，不用重新验证。
2. **`/Volumes/WD1/engine.duckdb` 已经被拆分并改名成三个文件**：`/Volumes/WD1/tdx.duckdb`（`market_day_kline`/`market_wide_kline`/`market_xdxr` 等）、`/Volumes/WD1/tdx-minutes.duckdb`（`market_minutes`）、`/Volumes/WD1/tdx-trans.duckdb`（`market_transactions`）。
3. **`market_day_kline`（dataset='day'，1900 万+行，1990-2026）、`market_wide_kline`（1900 万+行）、`market_minutes`（34 亿+行，2010-2026）、`market_xdxr`（17 万+行）现在都有真实数据了**——两天前这几张表要么是空表要么根本不存在，任务 5 因此把范围限制在"只实现 `get_trans`"；现在数据已经补齐，任务 5 整个重写为完整的 `EngineDataDuckDBClient`，覆盖全部五个数据集。这是本次修订里改动最大的部分。

**终局方向确认：目标不是永久保留双路径，而是最终能停掉旧的 fstore PostgreSQL 服务和 engine-data HTTP 服务。** 本计划里 `FQUANT_FSTORE_MODE`/`FQUANT_ENGINE_DATA_SOURCE` 默认值都还是走旧服务（PostgreSQL/HTTP），这是验证阶段的安全回退设计，不是终态。任务 1-7 跑通、双路径对比稳定之后，后续（不在本计划范围内）还要：(a) 把默认值切成 `duckdb`；(b) 确认没有其它消费者还依赖旧服务后，删掉 `FStoreClient`（psycopg 连接）和 `EngineDataClient`（HTTP 客户端）这两条代码路径本身，旧服务才能真正下线。**这里有个必须先确认的前提：`../fquant` 也是 `engine.data_base_url`（`internal/api/engine_stock_data.go`、`internal/backtestdata/engine_execution_store.go`）和 fstore PostgreSQL（`internal/config/config.go` 的 `FSTORE_DATABASE_*`）的消费者，完全不在本计划范围内**——只做完 tickflow-stock-panel 和 fm-cli 两边，这两个旧服务都还不能下线，fquant 那边需要单独一份类似的迁移计划，这个缺口现在就要记下来。

---

## 2026-07-08 修订说明

对照 engine 仓库新写的 `docs/DUCKDB-TABLE-CATALOG.md`（当前 DuckDB 表结构权威目录）核对了一遍本计划，发现一处必须修的问题，其余核实过的部分（`market_day_kline`/`market_wide_kline`/`market_minutes`/`market_xdxr`、`market_transactions.side` 编码、`xingquanjiya` 列全 NULL 的已知 bug）都和目录一致，不用改：

1. **`fd_daily_market` 这张表已经不存在了，正文所有引用已经全部改成 `daily_markets`。** `../fstore/fdata-store` 那边在这两天内做了一轮"无前缀物理表"改造：`t_{asset_type}_daily_markets` 这一族被合并进的统一长表，从原来的 `fd_daily_market`（一次性迁移落地时的过渡命名）重命名/提升成了无前缀的 `daily_markets`（`DUCKDB-TABLE-CATALOG.md` 确认 `fd_*` 表当前是 0 张，`daily_markets` 是 21,849,221 行的无前缀 BASE TABLE）。列结构不变（`asset_type/code/trade_date/price/change_percent/volume/amount/payload_json/source/updated_at`，新增了 `z50/z52/z53/tags` 四列，本计划用不到），`payload_json` 的驼峰 key 集合也不变，只是表名换了——这次修订是纯粹的字符串替换，不影响本计划任何一处 SQL 逻辑或字段映射结论。

---

## 背景核实

- `backend/app/data_providers/fquant/fstore_client.py` 的 `FStoreClient.query(sql, params)` 用 `%s` 风格占位符（psycopg），返回 `list[dict]`（`_query_psycopg3`/`_query_psycopg2` 内部分别用 `dict_row`/`RealDictCursor`）。
- 已逐条核实 `fquant_provider.py` 里下列方法的 SQL 在 `/Volumes/WD1/fstore.duckdb` 上**原样可跑**（表名、列名跟 PostgreSQL 完全一致，只有 `%s` vs `?` 占位符风格不同），因为 fdata-store 给每张迁移过的表都建了同名无前缀兼容 view：
  - `get_instruments`（`fquant_provider.py:206-247`）→ `base_infos`
  - `_get_daily_from_fstore_klines`（384-433）→ `t_1_day_klines`/`t_20_day_klines`/`day_klines`
  - `_get_adj_events_from_fstore`（498-520）→ `chuquan_chuxi`
  - `get_financial`（1068-1089）→ `financial_report_{income_statement,balance_sheet,cash_flow,annual,quick,forecast}`
  - `_get_universe_codes_from_chengfen_gu`（1003-1059）→ `chengfen_gu`
  - `get_universe_constituents`（1219-1283）→ `chengfen_gu_items`
- 已用 `duckdb -readonly /Volumes/WD1/fstore.duckdb` 现场验证过 `SELECT DISTINCT ON (col) ... ORDER BY col, t_date DESC` 语法在 DuckDB 1.x 上可以直接跑（`get_universe_constituents`/`_get_universe_codes_from_chengfen_gu`/`_get_fstore_realtime` 都用了这个 PostgreSQL 惯用写法），不需要改写成 `QUALIFY ROW_NUMBER() OVER (...)`。
- 已确认 `_get_fstore_realtime`（775-797）查询的 `t_{asset_type}_daily_markets`（例如 `t_1_daily_markets`）**在 fstore.duckdb 里不存在**——这一族表在迁移时被合并进了统一长表 `daily_markets`（列：`asset_type, code, trade_date, price, change_percent, volume, amount, payload_json, source, updated_at`，其余字段打包在 `payload_json` 里），是本计划里唯一必须重写 SQL 的方法。
- `financial_report_forecast` 在 fstore.duckdb 里是 0 行空表——已核实这不是迁移遗漏，PostgreSQL 源表本身也是 0 行（`docs/ai/fstore-duckdb-table-count-audit.md` 同口径），`get_financial(table="forecast")` 在两个数据源下都会返回空 df，属于现状一致，不是本计划引入的回归。
- `market_transactions`（`/Volumes/WD1/tdx-trans.duckdb`）当前有 9 亿多行真实数据（`dataset='trans'`，覆盖 `sh/sz/bj`，2017-01-03 至今），列为 `dataset, market, code, trade_date, time, price, volume, amount, side, raw_json`——**没有 `order_count` 列**。`side` 编码已经实测核实（直连 `http://192.168.5.99:8099` 打了一次线上 HTTP 接口对拍，`nas.wf:8099` 域名当时连不上）：真实 `direction` 取值分布是 `{0,1,2,5,8}`，不是"0=中性/1=买/2=卖"三档，`side` 和 HTTP 的 `direction` 是同一套编码，任务 5 直接透传，不做折叠映射。另外 `time`/`amount` 两列在 `market_minutes`（下面）里全是 NULL，但在 `market_transactions` 这张表里是正常有值的，不要混淆两张表。
- `market_day_kline`（`/Volumes/WD1/tdx.duckdb`，`dataset='day'` 时是个股/指数日K线，1900 万+行，1990-2026 全量）、`market_wide_kline`（同文件，1900 万+行，列为 `code, market, trade_date, open, close, high, low, volume, amount, up_count, down_count, last_close, change_rate, open_volume, open_turnz, open_unmatched, close_volume, close_turnz, close_unmatched, inner_volume, outer_volume, inner_amount, outer_amount`）、`market_xdxr`（同文件，17 万+行，列为 `code, event_date, category, name, fenhong, peigujia, songzhuangu, peigu, suogu, qianliutong, houliutong, qianzongguben, houzongguben, fenshu, xingquanjiya, raw_json`——**这不只是命名差异，是 engine 侧的真实 bug**：`xingquanjiya` 这一列 175,441 行**全是 NULL**，已经用 `raw_json` 核对过，原始 JSON 里的 key 确实是 `xingquanjia`（不带多余的 `ya`），说明 engine 那边写入/CSV 导入时查询的是拼错的列名，把正确数据写丢了。下游查询层做 `AS xingquanjia` 别名只能对齐字段名，**取不到真实数据**，这个字段需要 engine 仓库先修表结构/导入逻辑并回填存量数据，才能真正可用）、`market_minutes`（`/Volumes/WD1/tdx-minutes.duckdb`，34 亿+行，2010-2026 全量，列为 `dataset, market, code, trade_date, minute_index, time, price, volume, amount, raw_json`）**现在都有真实数据了**，逐列核对过和 `EngineDataClient`/`EngineDataDiskClient` 的 `get_day`/`get_wide`/`get_xdxr`/`get_minutes` 返回字段基本一一对应，具备实现完整 `EngineDataDuckDBClient` 的条件，见重写后的任务 5。

---

## 任务 1：新增 `FStoreDuckDBClient`

**文件：**
- 创建：`backend/app/data_providers/fquant/fstore_duckdb_client.py`
- 测试：`backend/tests/data_providers/test_fstore_duckdb_client.py`

- [ ] **步骤 1：写入新文件**

```python
"""fstore DuckDB 直连客户端 —— FStoreClient 的只读替代实现。

背景：fstore 已经把分析结果表迁移到本机
``/Volumes/WD1/fstore.duckdb``，并为每张迁移过的 PostgreSQL 表提供了
同名无前缀兼容 view（物理镜像表带 pg_/fd_ 前缀，兼容 view 不带）。

本客户端对外暴露和 ``FStoreClient`` 完全相同的
``query(sql, params) -> list[dict]`` 接口 —— 调用方（fquant_provider.py）
不需要感知底层是 PostgreSQL 还是 DuckDB。唯一的差异是 SQL 占位符风格：
调用方写的是 psycopg 风格 ``%s``，这里在执行前统一替换成 DuckDB 的
``?``。**因此调用方写的 SQL 字符串里不能出现字面意义的 "%s"**
（比如 LIKE 模式里的 ``%`` 后面刚好跟一个 ``s`` 的情况），当前已核实
fquant_provider.py 里所有直连 fstore 的 SQL 都不满足这个反例。

配置：
- ``FQUANT_FSTORE_DUCKDB_PATH``（默认 ``/Volumes/WD1/fstore.duckdb``）
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

FSTORE_DUCKDB_PATH = os.getenv("FQUANT_FSTORE_DUCKDB_PATH", "/Volumes/WD1/fstore.duckdb")


class FStoreDuckDBClient:
    """fstore DuckDB 只读客户端，接口对齐 FStoreClient。"""

    def __init__(self, path: str | None = None) -> None:
        self._path = path or FSTORE_DUCKDB_PATH
        self._conn: Any = None
        self._available: bool | None = None
        self._lock = threading.Lock()

    def _get_conn(self):
        if self._available is False:
            return None
        if self._conn is not None:
            return self._conn
        try:
            import duckdb
        except ImportError:
            logger.warning("FStoreDuckDBClient: duckdb 未安装")
            self._available = False
            return None
        if not os.path.exists(self._path):
            logger.warning("FStoreDuckDBClient: 文件不存在 %s", self._path)
            self._available = False
            return None
        try:
            conn = duckdb.connect(self._path, read_only=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("FStoreDuckDBClient: 打开失败 %s — %s", self._path, e)
            self._available = False
            return None
        self._conn = conn
        self._available = True
        logger.info("FStoreDuckDBClient: 已打开 %s（只读）", self._path)
        return conn

    @property
    def available(self) -> bool:
        return self._get_conn() is not None

    def query(self, sql: str, params: tuple | list | None = None) -> list[dict]:
        """执行 SELECT，返回 ``[{col: val, ...}, ...]``。失败返回空列表。"""
        conn = self._get_conn()
        if conn is None:
            return []
        duck_sql = sql.replace("%s", "?")
        try:
            with self._lock:
                cursor = conn.execute(duck_sql, list(params) if params else [])
                columns = [d[0] for d in cursor.description]
                rows = cursor.fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("FStoreDuckDBClient: 查询失败 — %s | SQL: %s", e, duck_sql[:200])
            return []
        return [dict(zip(columns, row)) for row in rows]
```

- [ ] **步骤 2：写入测试**

```python
"""FStoreDuckDBClient 测试 —— 需要本机挂载 /Volumes/WD1，否则自动 skip。"""
from __future__ import annotations

import os

import pytest

from app.data_providers.fquant.fstore_duckdb_client import FStoreDuckDBClient

DUCKDB_PATH = "/Volumes/WD1/fstore.duckdb"

pytestmark = pytest.mark.skipif(
    not os.path.exists(DUCKDB_PATH),
    reason=f"本机没有挂载 {DUCKDB_PATH}",
)


def test_query_returns_list_of_dict():
    client = FStoreDuckDBClient()
    rows = client.query(
        "SELECT code, name FROM base_infos WHERE code = %s",
        ("600519",),
    )
    assert len(rows) == 1
    assert rows[0]["code"] == "600519"
    assert isinstance(rows[0], dict)


def test_query_with_in_clause_placeholders():
    client = FStoreDuckDBClient()
    rows = client.query(
        "SELECT code FROM base_infos WHERE asset_type IN (%s,%s) ORDER BY code LIMIT 5",
        (1, 10),
    )
    assert len(rows) > 0


def test_query_unknown_table_returns_empty_not_raise():
    client = FStoreDuckDBClient()
    rows = client.query("SELECT * FROM table_that_does_not_exist WHERE code = %s", ("x",))
    assert rows == []


def test_available_false_when_file_missing():
    client = FStoreDuckDBClient(path="/tmp/does-not-exist.duckdb")
    assert client.available is False
    assert client.query("SELECT 1") == []
```

- [ ] **步骤 3：运行测试**

运行：`cd /Users/wf2311/Projects/wf2311/fm/tickflow-stock-panel/backend && uv run pytest tests/data_providers/test_fstore_duckdb_client.py -v`
预期：本机存在 `/Volumes/WD1/fstore.duckdb` 时 4 个用例全部 PASS。

- [ ] **步骤 4：Commit**

```bash
git add backend/app/data_providers/fquant/fstore_duckdb_client.py backend/tests/data_providers/test_fstore_duckdb_client.py
git commit -m "feat(data-providers): add read-only DuckDB client mirroring FStoreClient interface"
```

---

## 任务 2：把 `FQuantProvider` 接到 `FStoreDuckDBClient`（按环境变量切换）

**文件：**
- 修改：`backend/app/data_providers/fquant_provider.py:176-198`（`__init__`）
- 测试：`backend/tests/data_providers/test_fquant_provider_fstore_mode.py`（新建）

- [ ] **步骤 1：把 `__init__` 里的**

```python
    def __init__(self, engine_mode: str = "http") -> None:
        self._fstore = FStoreClient()
```

改为：

```python
    def __init__(self, engine_mode: str = "http") -> None:
        self._fstore = _build_fstore_client()
```

- [ ] **步骤 2：在 `FQuantProvider` 类定义之前（文件顶部 import 区之后）新增工厂函数**

```python
def _build_fstore_client():
    """按 ``FQUANT_FSTORE_MODE`` 选择 fstore 客户端实现。

    - ``postgres``（默认）：直连 fstore PostgreSQL（现状，安全回退）。
    - ``duckdb``：只读打开 /Volumes/WD1/fstore.duckdb。

    这是一个独立于 ``DATA_PROVIDER``/provider 白名单的内部开关——切换
    fstore 后端不改变 provider 的名字（仍然是 fquant/fquant_local），
    因为对上层调用方而言这只是同一份数据换了个更快的读取路径，不是
    换了一个新的数据 provider。
    """
    mode = os.getenv("FQUANT_FSTORE_MODE", "postgres").strip().lower()
    if mode == "duckdb":
        from app.data_providers.fquant.fstore_duckdb_client import FStoreDuckDBClient
        return FStoreDuckDBClient()
    return FStoreClient()
```

（`fquant_provider.py` 顶部已经 `import os`，不需要新增 import；`FStoreClient` 本来就已 import。）

- [ ] **步骤 3：新建测试，验证两种模式都能正确构造且互不影响**

```python
"""验证 FQuantProvider 按 FQUANT_FSTORE_MODE 选择正确的 fstore 客户端。"""
from __future__ import annotations

import os

import pytest

from app.data_providers.fquant.fstore_client import FStoreClient
from app.data_providers.fquant.fstore_duckdb_client import FStoreDuckDBClient
from app.data_providers.fquant_provider import FQuantProvider


def test_default_mode_is_postgres(monkeypatch):
    monkeypatch.delenv("FQUANT_FSTORE_MODE", raising=False)
    provider = FQuantProvider()
    assert isinstance(provider._fstore, FStoreClient)


def test_duckdb_mode_via_env(monkeypatch):
    monkeypatch.setenv("FQUANT_FSTORE_MODE", "duckdb")
    provider = FQuantProvider()
    assert isinstance(provider._fstore, FStoreDuckDBClient)


def test_unknown_mode_falls_back_to_postgres(monkeypatch):
    monkeypatch.setenv("FQUANT_FSTORE_MODE", "not_a_real_mode")
    provider = FQuantProvider()
    assert isinstance(provider._fstore, FStoreClient)
```

- [ ] **步骤 4：运行测试**

运行：`uv run pytest tests/data_providers/test_fquant_provider_fstore_mode.py -v`
预期：3 个用例全部 PASS（不需要真实 DuckDB 文件，只验证类型选择）。

- [ ] **步骤 5：Commit**

```bash
git add backend/app/data_providers/fquant_provider.py backend/tests/data_providers/test_fquant_provider_fstore_mode.py
git commit -m "feat(fquant-provider): select fstore client (postgres|duckdb) via FQUANT_FSTORE_MODE"
```

---

## 任务 3：验证"零改动"的方法在 DuckDB 模式下确实工作

任务 1-2 完成后，下面 6 个方法**不需要修改任何代码**（因为 `self._fstore.query(...)` 已经指向 `FStoreDuckDBClient`，SQL 字符串本身在 DuckDB 上可以直接跑）。本任务只是补集成测试，把"确实可用"钉死成回归测试，防止以后有人在这些方法里引入 PostgreSQL-only 语法却没发现。

**文件：**
- 测试：`backend/tests/data_providers/test_fquant_provider_duckdb_integration.py`（新建）

- [ ] **步骤 1：写入测试文件**

```python
"""FQuantProvider 在 FQUANT_FSTORE_MODE=duckdb 下的集成回归测试。

覆盖那些"迁移时不需要改代码，只是换了底层客户端"的方法——如果以后
有人往这些方法里加了 PostgreSQL-only 语法（比如新的 ::type cast 或
系统目录查询），这里的测试会先坏，而不是等到生产环境切换才发现。
"""
from __future__ import annotations

import os
from datetime import datetime

import pytest

from app.data_providers.fquant_provider import FQuantProvider

DUCKDB_PATH = "/Volumes/WD1/fstore.duckdb"

pytestmark = pytest.mark.skipif(
    not os.path.exists(DUCKDB_PATH),
    reason=f"本机没有挂载 {DUCKDB_PATH}",
)


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("FQUANT_FSTORE_MODE", "duckdb")
    return FQuantProvider()


def test_get_instruments_stock(provider):
    df = provider.get_instruments("stock")
    assert df.height > 0
    assert "600519" in df["code"].to_list()


def test_get_daily_fstore_fallback(provider):
    rows = provider._get_daily_from_fstore_klines(
        symbol="600519.SH", code="600519",
        start_time=datetime(2025, 1, 1), end_time=datetime(2025, 10, 31),
    )
    assert len(rows) > 0


def test_get_adj_events_from_fstore(provider):
    rows = provider._get_adj_events_from_fstore(
        symbol="600519.SH", code="600519",
        start_time=None, end_time=None,
    )
    assert isinstance(rows, list)


def test_get_financial_income(provider):
    df = provider.get_financial("600519.SH", "income")
    assert df.height > 0


def test_get_financial_forecast_is_empty_not_error(provider):
    """financial_report_forecast 源表在 PG 和 DuckDB 里都是 0 行，
    这里验证的是"不报错、返回空 df"，不是数据覆盖率问题。"""
    df = provider.get_financial("600519.SH", "forecast")
    assert df.height == 0


def test_get_universe_constituents(provider):
    df = provider.get_universe_constituents("000001")
    assert isinstance(df, type(df))  # 只验证不抛异常；具体行数取决于该指数当前是否有成分股快照
```

- [ ] **步骤 2：运行测试**

运行：`uv run pytest tests/data_providers/test_fquant_provider_duckdb_integration.py -v`
预期：本机存在 `/Volumes/WD1/fstore.duckdb` 时全部 PASS。

- [ ] **步骤 3：Commit**

```bash
git add backend/tests/data_providers/test_fquant_provider_duckdb_integration.py
git commit -m "test(fquant-provider): pin down zero-code-change methods against real fstore.duckdb"
```

---

## 任务 4：重写 `_get_fstore_realtime`（唯一需要真正改 SQL 的方法）

**文件：**
- 修改：`backend/app/data_providers/fquant_provider.py:775-797`
- 测试：`backend/tests/data_providers/test_fquant_provider_realtime_duckdb.py`（新建）

**问题：** 原实现按 `table = f"t_{asset_type}_daily_markets"` 动态拼表名，这张表在 fstore.duckdb 里不存在——`t_{asset_type}_daily_markets` 这一族表在迁移时被合并进了统一长表 `daily_markets`（`asset_type` 是过滤列，不是表名的一部分），其余业务字段被压进 `payload_json`。`daily_markets` 的真实列只有 `asset_type, code, trade_date, price, change_percent, volume, amount, payload_json, source, updated_at`；已用 `json_keys(payload_json)` 现场抽样确认过 `Name/Zdfd/Zded/Cjl/Cje/Jrkpj/Zgj/Zdj/Zrspj/Hslv/Zhfu` 这些字段都在 JSON 里（驼峰命名）。

- [ ] **步骤 1：把原实现**

```python
    def _get_fstore_realtime(self, symbols: list[str]) -> list[dict]:
        grouped: dict[int, list[str]] = {}
        for symbol in symbols:
            asset_type = self._asset_type_num_for_symbol(symbol)
            if asset_type is None:
                continue
            grouped.setdefault(asset_type, []).append(symbol_to_code(symbol))

        out: list[dict] = []
        for asset_type, codes in grouped.items():
            table = f"t_{asset_type}_daily_markets"
            placeholders = ",".join(["%s"] * len(codes))
            sql = f"""
                SELECT DISTINCT ON (code)
                    code, name, tdate, price, zdfd, zded, cjl, cje,
                    jrkpj, zgj, zdj, zrspj, hslv, zhfu
                FROM {table}
                WHERE code IN ({placeholders})
                ORDER BY code, tdate DESC
            """
            rows = self._fstore.query(sql, codes)
            out.extend(self._fstore_quote_to_row(r, asset_type) for r in rows)
        return [r for r in out if r]
```

改为：

```python
    def _get_fstore_realtime(self, symbols: list[str]) -> list[dict]:
        grouped: dict[int, list[str]] = {}
        for symbol in symbols:
            asset_type = self._asset_type_num_for_symbol(symbol)
            if asset_type is None:
                continue
            grouped.setdefault(asset_type, []).append(symbol_to_code(symbol))

        out: list[dict] = []
        for asset_type, codes in grouped.items():
            rows = self._query_fstore_realtime_rows(asset_type, codes)
            out.extend(self._fstore_quote_to_row(r, asset_type) for r in rows)
        return [r for r in out if r]

    def _query_fstore_realtime_rows(self, asset_type: int, codes: list[str]) -> list[dict]:
        """按当前 fstore 客户端类型选择表结构。

        PostgreSQL：按 asset_type 物理分表 t_{asset_type}_daily_markets，
        字段是具名列。DuckDB：统一长表 daily_markets，asset_type 是
        过滤列，其余字段打包在 payload_json（驼峰 key）里，用
        ``->>`` 抽取后转型别名，对齐 PostgreSQL 分支的输出列名，
        这样调用方 ``_fstore_quote_to_row`` 不需要区分数据源。
        """
        placeholders = ",".join(["%s"] * len(codes))
        if isinstance(self._fstore, FStoreDuckDBClient):
            sql = f"""
                SELECT DISTINCT ON (code)
                    code,
                    COALESCE(payload_json->>'Name', '') AS name,
                    trade_date AS tdate,
                    price,
                    CAST(NULLIF(payload_json->>'Zdfd', '') AS DOUBLE) AS zdfd,
                    CAST(NULLIF(payload_json->>'Zded', '') AS DOUBLE) AS zded,
                    CAST(NULLIF(payload_json->>'Cjl', '') AS BIGINT) AS cjl,
                    CAST(NULLIF(payload_json->>'Cje', '') AS DOUBLE) AS cje,
                    CAST(NULLIF(payload_json->>'Jrkpj', '') AS DOUBLE) AS jrkpj,
                    CAST(NULLIF(payload_json->>'Zgj', '') AS DOUBLE) AS zgj,
                    CAST(NULLIF(payload_json->>'Zdj', '') AS DOUBLE) AS zdj,
                    CAST(NULLIF(payload_json->>'Zrspj', '') AS DOUBLE) AS zrspj,
                    CAST(NULLIF(payload_json->>'Hslv', '') AS DOUBLE) AS hslv,
                    CAST(NULLIF(payload_json->>'Zhfu', '') AS DOUBLE) AS zhfu
                FROM daily_markets
                WHERE asset_type = %s AND code IN ({placeholders})
                ORDER BY code, trade_date DESC
            """
            return self._fstore.query(sql, (asset_type, *codes))

        table = f"t_{asset_type}_daily_markets"
        sql = f"""
            SELECT DISTINCT ON (code)
                code, name, tdate, price, zdfd, zded, cjl, cje,
                jrkpj, zgj, zdj, zrspj, hslv, zhfu
            FROM {table}
            WHERE code IN ({placeholders})
            ORDER BY code, tdate DESC
        """
        return self._fstore.query(sql, codes)
```

（`fquant_provider.py` 顶部 `import` 加一行 `from app.data_providers.fquant.fstore_duckdb_client import FStoreDuckDBClient`。）

- [ ] **步骤 2：新建测试**

```python
"""验证 _get_fstore_realtime 在 DuckDB 模式下改用 daily_markets 且字段对齐。"""
from __future__ import annotations

import os

import pytest

from app.data_providers.fquant_provider import FQuantProvider

DUCKDB_PATH = "/Volumes/WD1/fstore.duckdb"

pytestmark = pytest.mark.skipif(
    not os.path.exists(DUCKDB_PATH),
    reason=f"本机没有挂载 {DUCKDB_PATH}",
)


def test_fstore_realtime_duckdb_matches_postgres_shape(monkeypatch):
    monkeypatch.setenv("FQUANT_FSTORE_MODE", "duckdb")
    duckdb_provider = FQuantProvider()
    duckdb_rows = duckdb_provider._get_fstore_realtime(["600519.SH"])
    assert len(duckdb_rows) == 1
    row = duckdb_rows[0]
    # _get_fstore_realtime 最终经过 _fstore_quote_to_row -> _quote_row 归一，
    # 输出形状固定是 symbol/name/last_price/prev_close/open/high/low/volume/
    # amount/timestamp/source/ext（不是 SQL 里查出来的原始列名 code/price/
    # zdfd/cjl/cje —— 那些只是 _fstore_quote_to_row 的输入，不是这里的输出）。
    assert row["symbol"] == "600519.SH"
    assert row["last_price"] > 0
    assert row["source"] == "fquant:fstore:daily_markets"
    assert "change_pct" in row["ext"]
```

- [ ] **步骤 3：运行测试**

运行：`uv run pytest tests/data_providers/test_fquant_provider_realtime_duckdb.py -v`
预期：PASS

- [ ] **步骤 4：手动对拍 postgres vs duckdb 输出**

```bash
cd /Users/wf2311/Projects/wf2311/fm/tickflow-stock-panel/backend
uv run python - <<'PY'
import os
from app.data_providers.fquant_provider import FQuantProvider

os.environ.pop("FQUANT_FSTORE_MODE", None)
pg = FQuantProvider()._get_fstore_realtime(["600519.SH"])

os.environ["FQUANT_FSTORE_MODE"] = "duckdb"
duck = FQuantProvider()._get_fstore_realtime(["600519.SH"])

print("postgres:", pg)
print("duckdb:  ", duck)
PY
```

预期：两边 `price`/`zdfd`/`cjl`/`cje` 数值一致（daily_markets 是每日实时同步的活表，不是一次性快照，理论上应该和 PostgreSQL 当前值一致；如果不一致，先去核对 fdata-store 的 dual-write 是否真的在跑，而不是怀疑这条 SQL）。

- [ ] **步骤 5：Commit**

```bash
git add backend/app/data_providers/fquant_provider.py backend/tests/data_providers/test_fquant_provider_realtime_duckdb.py
git commit -m "feat(fquant-provider): rewrite _get_fstore_realtime for DuckDB's unified daily_markets table"
```

---

## 任务 5：完整的 `EngineDataDuckDBClient`（`get_day`/`get_wide`/`get_minutes`/`get_trans`/`get_xdxr` 全覆盖）

**文件：**
- 创建：`backend/app/data_providers/fquant/engine_data_duckdb_client.py`
- 修改：`backend/app/data_providers/fquant_provider.py:176-198`（`__init__`，新增 `FQUANT_ENGINE_DATA_SOURCE` 开关）
- 测试：`backend/tests/data_providers/test_engine_data_duckdb_client.py`（新建）

**范围说明（相对 2026-07-06 版本的重大变化）：** 原计划因为 `engine.duckdb` 只有 `market_transactions` 有数据，只实现了 `get_trans` 一个方法。现在 engine 侧把 `engine.duckdb` 拆分重组成了 `tdx.duckdb`（`market_day_kline`/`market_wide_kline`/`market_xdxr`）、`tdx-minutes.duckdb`（`market_minutes`）、`tdx-trans.duckdb`（`market_transactions`）三个文件，五个数据集都有真实数据，已经逐列核对过和 `EngineDataClient`/`EngineDataDiskClient` 的字段基本对应（见"背景核实"一节）。因此这里直接实现完整契约，`EngineDataDuckDBClient` 可以整体替换 `self._engine`，不再需要像上一版那样单独开一个 `self._engine_trans_duckdb` 旁路属性。

**仍然不接入 `engine_mode`/`registry.py`**：`engine_mode` 是 `FQuantProvider.__init__` 的构造参数，由 provider 工厂（`registry.py` 的 `fquant`/`fquant_local`）决定，改这个会牵扯到 provider 选择这个用户可见的概念。这里用独立的 `FQUANT_ENGINE_DATA_SOURCE` 环境变量在 `__init__` 内部直接替换 `self._engine`，`engine_mode`/`self._engine_mode` 本身保持不变（因此 `self._engine_key(symbol, code)` 依然按原逻辑返回不带前缀的裸 `code`，正好是 `EngineDataDuckDBClient`期望的输入，不需要额外改 `_engine_key`）。

**已知数据差异：**
- `market_transactions` 没有 `order_count` 列，固定填 `None`。
- `market_wide_kline` 没有 `datetime`/`adjustment_count` 两列（`market_day_kline` 有），`get_wide` 返回行里这两个字段固定填 `None`/`0`。
- `market_xdxr` 的列名是 `xingquanjiya`，且这一列**全是 NULL**（不是简单的命名差异）——已经查过 `raw_json`，原始 JSON key 确实是 `xingquanjia`（没有多余的 `ya`），说明 engine 侧写入/CSV 导入时用错了列名，把真实数据写丢了。下游查询做 `AS xingquanjia` 别名只解决字段名对齐，**取不到真实数据**，`get_xdxr` 返回的 `xingquanjia` 字段现阶段恒为 `None`，这是 engine 仓库需要修的问题（改表结构/导入逻辑并回填存量），不是本任务能解决的，任务里只做别名对齐 + 明确注释这个限制。

**side -> direction 编码已经核实过，不需要再跑对拍脚本**（2026-07-07，直接打了线上 engine-data HTTP 接口 `http://192.168.5.99:8099/api/v1/trans/600519?date=20260706` 对拍，走 no-proxy 直连 IP——`nas.wf:8099` 域名当时返回 `Empty reply from server`，直连 IP 才拿到真实数据）：真实 `direction` 分布是 `{0,1,2,5,8}`，不是"买/卖/中性"三档，`market_transactions.side` 和 HTTP 的 `direction` 是同一套编码。正确做法是**直接透传，不做任何折叠映射**，下面 `get_trans` 的实现已经改成 `"direction": r[4]`（`side` 原始值），不再需要映射表。

- [ ] **步骤 1：写入 `engine_data_duckdb_client.py`**

```python
"""engine 侧 TDX 数据只读客户端 —— 完整覆盖 get_day/get_wide/get_minutes/get_trans/get_xdxr。

分别打开三个独立文件（不做跨库 ATTACH，因为没有跨表 join 需求）：
- /Volumes/WD1/tdx.duckdb          -> market_day_kline / market_wide_kline / market_xdxr
- /Volumes/WD1/tdx-minutes.duckdb  -> market_minutes
- /Volumes/WD1/tdx-trans.duckdb    -> market_transactions
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

TDX_PATH = os.getenv("FQUANT_TDX_DUCKDB_PATH", "/Volumes/WD1/tdx.duckdb")
TDX_MINUTES_PATH = os.getenv("FQUANT_TDX_MINUTES_DUCKDB_PATH", "/Volumes/WD1/tdx-minutes.duckdb")
TDX_TRANS_PATH = os.getenv("FQUANT_TDX_TRANS_DUCKDB_PATH", "/Volumes/WD1/tdx-trans.duckdb")

# side 直接就是 HTTP 契约的 direction 编码（已实测核实，取值 {0,1,2,5,8}），
# 不需要映射表，get_trans 里直接透传。

# A 股代码段 -> 交易所前缀。market_day_kline/market_wide_kline/market_xdxr/
# market_minutes/market_transactions 的 code 列都带这个前缀（如 sh600519），
# 而 FQuantProvider 传进来的 code 是裸代码（如 600519，来自 symbol_to_code）。
_PREFIX_BY_HEAD = {
    "60": "sh", "68": "sh", "90": "sh",
    "00": "sz", "30": "sz", "20": "sz",
    "43": "bj", "83": "bj", "87": "bj", "92": "bj",
}


def _prefixed_code(code: str) -> str:
    code = code.strip()
    if len(code) != 6:
        return code
    return _PREFIX_BY_HEAD.get(code[:2], "") + code if code[:2] in _PREFIX_BY_HEAD else code


class _SingleFileConn:
    """单个 DuckDB 文件的懒加载只读连接，四个数据集各自独立复用一份。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: Any = None
        self._available: bool | None = None

    def get(self):
        if self._available is False:
            return None
        if self._conn is not None:
            return self._conn
        try:
            import duckdb
        except ImportError:
            self._available = False
            return None
        if not os.path.exists(self._path):
            logger.warning("EngineDataDuckDBClient: 文件不存在 %s", self._path)
            self._available = False
            return None
        try:
            self._conn = duckdb.connect(self._path, read_only=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("EngineDataDuckDBClient: 打开失败 %s — %s", self._path, e)
            self._available = False
            return None
        self._available = True
        return self._conn


class EngineDataDuckDBClient:
    """只读打开 tdx.duckdb/tdx-minutes.duckdb/tdx-trans.duckdb，完整实现五个数据集。"""

    def __init__(
        self,
        tdx_path: str | None = None,
        minutes_path: str | None = None,
        trans_path: str | None = None,
    ) -> None:
        self._tdx = _SingleFileConn(tdx_path or TDX_PATH)
        self._minutes = _SingleFileConn(minutes_path or TDX_MINUTES_PATH)
        self._trans = _SingleFileConn(trans_path or TDX_TRANS_PATH)

    def get_day(self, code: str, limit: int = 250) -> list[dict]:
        """读 market_day_kline（dataset='day'），字段对齐 EngineDataClient 的 day 数据集。"""
        conn = self._tdx.get()
        if conn is None:
            return []
        try:
            cursor = conn.execute(
                """
                SELECT trade_date, datetime, open, close, high, low, volume, amount,
                       up_count, down_count, adjustment_count
                FROM market_day_kline
                WHERE code = ? AND dataset = 'day'
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                [_prefixed_code(code), limit],
            )
            rows = cursor.fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("EngineDataDuckDBClient: get_day 查询失败 — %s", e)
            return []
        return [
            {
                "date": r[0].strftime("%Y-%m-%d") if r[0] else None,
                "datetime": r[1], "open": r[2], "close": r[3], "high": r[4], "low": r[5],
                "volume": r[6], "amount": r[7], "up": r[8], "down": r[9], "adjustment_count": r[10],
            }
            for r in rows
        ]

    def get_wide(self, code: str, limit: int = 250, asset_type: str | None = None) -> list[dict]:
        """读 market_wide_kline，字段对齐 EngineDataClient 的 wide 数据集。

        market_wide_kline 没有 datetime/adjustment_count 两列（market_day_kline 有），
        这里固定填 None/0——调用方的字段归一函数需要能容忍这两个字段缺失。
        """
        _ = asset_type
        conn = self._tdx.get()
        if conn is None:
            return []
        try:
            cursor = conn.execute(
                """
                SELECT trade_date, open, close, high, low, volume, amount, up_count, down_count,
                       last_close, change_rate, open_volume, open_turnz, open_unmatched,
                       close_volume, close_turnz, close_unmatched, inner_volume, outer_volume,
                       inner_amount, outer_amount
                FROM market_wide_kline
                WHERE code = ?
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                [_prefixed_code(code), limit],
            )
            rows = cursor.fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("EngineDataDuckDBClient: get_wide 查询失败 — %s", e)
            return []
        return [
            {
                "date": r[0].strftime("%Y-%m-%d") if r[0] else None, "datetime": None,
                "open": r[1], "close": r[2], "high": r[3], "low": r[4], "volume": r[5], "amount": r[6],
                "up": r[7], "down": r[8], "adjustment_count": 0,
                "last_close": r[9], "change_rate": r[10],
                "open_volume": r[11], "open_turnz": r[12], "open_unmatched": r[13],
                "close_volume": r[14], "close_turnz": r[15], "close_unmatched": r[16],
                "inner_volume": r[17], "outer_volume": r[18], "inner_amount": r[19], "outer_amount": r[20],
            }
            for r in rows
        ]

    def get_minutes(self, code: str, date_yyyymmdd: str, limit: int = 5000) -> list[dict]:
        """读 market_minutes，字段对齐 EngineDataClient 的 minutes 数据集（price/volume）。

        market_minutes 的 time/amount 两列全表 34 亿+行全是 NULL（已实测确认），
        只有 price/volume/minute_index 有真实数据，这也是为什么只选 price/volume
        两列、靠 minute_index 排序——不要改成查 time 列，查了也是 None。
        """
        conn = self._minutes.get()
        if conn is None:
            return []
        trade_date = f"{date_yyyymmdd[0:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}"
        try:
            cursor = conn.execute(
                """
                SELECT price, volume
                FROM market_minutes
                WHERE code = ? AND trade_date = ? AND dataset = 'minutes'
                ORDER BY minute_index
                LIMIT ?
                """,
                [_prefixed_code(code), trade_date, limit],
            )
            rows = cursor.fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("EngineDataDuckDBClient: get_minutes 查询失败 — %s", e)
            return []
        return [{"price": r[0], "volume": r[1]} for r in rows]

    def get_trans(self, code: str, date_yyyymmdd: str, limit: int = 5000) -> list[dict]:
        """读 market_transactions，字段对齐 EngineDataClient 的 trans 数据集。

        market_transactions 没有 order_count 列，这里固定填 None——
        调用方 trans_rows_to_df 需要能容忍这一列缺失/为空。
        """
        conn = self._trans.get()
        if conn is None:
            return []
        trade_date = f"{date_yyyymmdd[0:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}"
        try:
            cursor = conn.execute(
                """
                SELECT time, price, volume, amount, side
                FROM market_transactions
                WHERE code = ? AND trade_date = ? AND dataset = 'trans'
                ORDER BY time
                LIMIT ?
                """,
                [_prefixed_code(code), trade_date, limit],
            )
            rows = cursor.fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("EngineDataDuckDBClient: get_trans 查询失败 — %s", e)
            return []
        return [
            {
                "time": r[0], "price": r[1], "volume": r[2], "amount": r[3],
                "order_count": None, "direction": r[4],
            }
            for r in rows
        ]

    def get_xdxr(self, code: str, limit: int = 100, asset_type: str | None = None) -> list[dict]:
        """读 market_xdxr，字段对齐 EngineDataClient 的 xdxr 数据集。

        表里的列名是 xingquanjiya（比 HTTP 契约的 xingquanjia 多一个 ya），这里
        用 AS xingquanjia 对齐字段名——但这只是对齐命名，不是修复数据：这一列
        当前全表都是 NULL（已实测确认，engine 侧写入/导入用错了列名把真实数据
        写丢了），所以这个方法返回的 xingquanjia 字段现阶段恒为 None，等 engine
        仓库修好表结构/回填存量数据之后才会有真实值，这里不做任何掩盖或伪造。
        """
        _ = asset_type
        conn = self._tdx.get()
        if conn is None:
            return []
        try:
            cursor = conn.execute(
                """
                SELECT event_date, category, name, fenhong, peigujia, songzhuangu, peigu, suogu,
                       qianliutong, houliutong, qianzongguben, houzongguben, fenshu, xingquanjiya
                FROM market_xdxr
                WHERE code = ?
                ORDER BY event_date DESC
                LIMIT ?
                """,
                [_prefixed_code(code), limit],
            )
            rows = cursor.fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("EngineDataDuckDBClient: get_xdxr 查询失败 — %s", e)
            return []
        return [
            {
                "date": r[0].strftime("%Y-%m-%d") if r[0] else None,
                "category": r[1], "name": r[2], "fenhong": r[3], "peigujia": r[4],
                "songzhuangu": r[5], "peigu": r[6], "suogu": r[7], "qianliutong": r[8],
                "houliutong": r[9], "qianzongguben": r[10], "houzongguben": r[11],
                "fenshu": r[12], "xingquanjia": r[13],
            }
            for r in rows
        ]
```

- [ ] **步骤 2：在 `fquant_provider.py:__init__`（176-198 行）末尾追加**

```python
        if os.getenv("FQUANT_ENGINE_DATA_SOURCE", "").strip().lower() == "duckdb":
            from app.data_providers.fquant.engine_data_duckdb_client import EngineDataDuckDBClient
            self._engine = EngineDataDuckDBClient()
```

（放在原有 `if engine_mode == "disk": ... else: self._engine = EngineDataClient()` 之后，作为一个整体覆盖——不改 `engine_mode`/`self._engine_mode` 本身，所以 `self._engine_key` 依然按原逻辑返回裸 `code`，正好是 `EngineDataDuckDBClient` 期望的输入。）

- [ ] **步骤 3：新建测试**

```python
"""EngineDataDuckDBClient 完整契约测试。"""
from __future__ import annotations

import os

import pytest

from app.data_providers.fquant.engine_data_duckdb_client import EngineDataDuckDBClient, _prefixed_code

TDX_PATH = "/Volumes/WD1/tdx.duckdb"
TDX_MINUTES_PATH = "/Volumes/WD1/tdx-minutes.duckdb"
TDX_TRANS_PATH = "/Volumes/WD1/tdx-trans.duckdb"


def test_prefixed_code():
    assert _prefixed_code("600519") == "sh600519"
    assert _prefixed_code("000001") == "sz000001"
    assert _prefixed_code("300059") == "sz300059"
    assert _prefixed_code("830799") == "bj830799"


@pytest.mark.skipif(not os.path.exists(TDX_PATH), reason=f"本机没有 {TDX_PATH}")
def test_get_day_returns_rows():
    client = EngineDataDuckDBClient()
    rows = client.get_day("600519", limit=5)
    assert len(rows) > 0
    for key in ("date", "open", "close", "high", "low", "volume", "amount"):
        assert key in rows[0]


@pytest.mark.skipif(not os.path.exists(TDX_PATH), reason=f"本机没有 {TDX_PATH}")
def test_get_wide_returns_rows():
    client = EngineDataDuckDBClient()
    rows = client.get_wide("600519", limit=5)
    assert len(rows) > 0
    for key in ("open", "last_close", "change_rate", "inner_volume", "outer_volume"):
        assert key in rows[0]


@pytest.mark.skipif(not os.path.exists(TDX_PATH), reason=f"本机没有 {TDX_PATH}")
def test_get_xdxr_returns_rows_with_aliased_column():
    client = EngineDataDuckDBClient()
    rows = client.get_xdxr("600519", limit=5)
    assert len(rows) > 0
    assert "xingquanjia" in rows[0]  # 键名是 xingquanjia 不是 xingquanjiya
    # 已知限制：market_xdxr.xingquanjiya 全表都是 NULL（engine 侧写入 bug，
    # 见任务背景），这里断言值为 None 是记录现状，不是期望值——等 engine 那边
    # 修好后这一行断言要改成非 None，否则会一直"假装通过"掩盖数据已经修复的事实。
    assert rows[0]["xingquanjia"] is None


@pytest.mark.skipif(not os.path.exists(TDX_MINUTES_PATH), reason=f"本机没有 {TDX_MINUTES_PATH}")
def test_get_minutes_returns_price_volume_shape():
    client = EngineDataDuckDBClient()
    rows = client.get_minutes("600519", "20260706", limit=5)
    assert len(rows) > 0
    assert set(rows[0].keys()) == {"price", "volume"}


@pytest.mark.skipif(not os.path.exists(TDX_TRANS_PATH), reason=f"本机没有 {TDX_TRANS_PATH}")
def test_get_trans_returns_rows_with_expected_shape():
    client = EngineDataDuckDBClient()
    rows = client.get_trans("600519", "20260706", limit=10)
    assert len(rows) > 0
    for key in ("time", "price", "volume", "amount", "order_count", "direction"):
        assert key in rows[0]
```

- [ ] **步骤 4：运行测试**

运行：`uv run pytest backend/tests/data_providers/test_engine_data_duckdb_client.py -v`
预期：本机挂载了对应文件时全部 PASS。

- [ ] **步骤 5：对拍 http vs duckdb 的实际取值**

```bash
cd /Users/wf2311/Projects/wf2311/fm/tickflow-stock-panel/backend
for src in "" duckdb; do
  echo "=== FQUANT_ENGINE_DATA_SOURCE=$src ==="
  FQUANT_ENGINE_DATA_SOURCE=$src uv run python - <<'PY'
from app.data_providers.fquant_provider import FQuantProvider
p = FQuantProvider()
print("day:", p._engine.get_day("600519", limit=2))
print("wide:", p._engine.get_wide("600519", limit=2))
print("xdxr:", p._engine.get_xdxr("600519", limit=2))
PY
done
```

预期：`open`/`close`/`high`/`low` 等核心字段数值一致（同一份 TDX 源数据，只是读取路径不同）；`duckdb` 下 `wide` 的 `datetime`/`adjustment_count` 恒为 `None`/`0`（已知限制，见任务背景）。

- [ ] **步骤 6：Commit**

```bash
git add backend/app/data_providers/fquant/engine_data_duckdb_client.py \
        backend/app/data_providers/fquant_provider.py \
        backend/tests/data_providers/test_engine_data_duckdb_client.py
git commit -m "feat(fquant-provider): full EngineDataDuckDBClient covering day/wide/minutes/trans/xdxr via FQUANT_ENGINE_DATA_SOURCE"
```

---

## 任务 6：明确标注不迁移的部分（文档，不改代码）

**文件：**
- 创建：`docs/duckdb-source-coverage.md`

- [ ] **步骤 1：新建文档**

```markdown
# fstore/engine DuckDB 数据源覆盖范围

## fstore（`FQUANT_FSTORE_MODE=duckdb`，默认 postgres）

已覆盖（零代码改动或已重写，见 2026-07-06-fstore-engine-duckdb-source.md 任务 1-4）：

- `get_instruments`（`base_infos`）
- `get_daily` 的 fstore 兜底路径（`_get_daily_from_fstore_klines`，`t_1_day_klines`/`t_20_day_klines`/`day_klines`）
- `get_adj_factors` 的 fstore 兜底路径（`_get_adj_events_from_fstore`，`chuquan_chuxi`）
- `get_financial`（`financial_report_*`；`forecast` 表本身是空表，两个数据源下都返回空 df，不是回归）
- `_get_universe_codes_from_chengfen_gu`（`chengfen_gu`）
- `get_universe_constituents`（`chengfen_gu_items`）
- `_get_fstore_realtime`（重写为 `daily_markets`，见任务 4）

**明确不覆盖：**

- `get_daily`/`get_adj_factors` 的**主路径**是 engine-data（`wide`/`xdxr`），只有 fallback 到 fstore 时才会用到上面这些方法；本计划不改变 engine-data 主路径的行为。
- `get_by_universes`/涉及 `t_N_money_flow_minutes`/`hsgt_money_flow` 之类的其它 fstore 表（`docs/data-query-inventory-local-source.md` §10.2 列出的其余表）不在本次范围内，如果以后要接，应该复用任务 1 的 `FStoreDuckDBClient`，不要新建第三个 fstore 客户端。

## engine（`FQUANT_ENGINE_DATA_SOURCE=duckdb`，默认 http，与 `engine_mode` 完全独立）

已覆盖（2026-07-07 起，见任务 5）：`get_day`（`tdx.duckdb` 的 `market_day_kline`）、`get_wide`（`market_wide_kline`，`datetime`/`adjustment_count` 恒为 `None`/`0`）、`get_minutes`（`tdx-minutes.duckdb` 的 `market_minutes`，`time`/`amount` 两列全表 NULL，只有 `price`/`volume` 有数据）、`get_trans`（`tdx-trans.duckdb` 的 `market_transactions`，`order_count` 恒为 `None`，`direction` 直接透传 `side`，已实测确认取值范围 `{0,1,2,5,8}`）、`get_xdxr`（`market_xdxr`，`xingquanjiya` 列已用 `AS xingquanjia` 对齐命名，但该列当前全表 NULL，是 engine 侧写入 bug，不是本计划能修的，`xingquanjia` 字段现阶段恒为 `None`）。

**已知未解决的语义冲突**：`../fm-cli` 的 `internal/cli/stock/engine_data.go` 里 `directionLabel` 把 `direction=1` 标成卖出、`2` 标成买入；如果 tickflow-stock-panel 其它地方（前端展示、`trans_rows_to_df` 的下游消费者）把 `1`/`2` 理解成"买/卖"，两边对同一个编码的中文语义是反的，需要找一方权威定义再统一，本次改造只做如实透传，不处理这个问题。

**明确不覆盖：**

- `get_by_universes` 之外的 engine 相关能力（比如筹码 `chips`）不在 `tdx*.duckdb` 里，也不在本计划范围内。
- TDX 磁盘本地源迁移（`docs/data-query-inventory-local-source.md` 里的 P0-P4 计划）是另一条独立路线，处理的是完全不同的上游（TDX CSV vs engine 侧的 DuckDB 导出），两者不冲突，不要合并成一个开关。
```

- [ ] **步骤 2：Commit**

```bash
git add docs/duckdb-source-coverage.md
git commit -m "docs: document fstore/engine DuckDB source coverage and exclusions"
```

---

## 任务 7：手动回归与 CHANGELOG

- [ ] **步骤 1：跑一遍关键接口的 postgres/duckdb 对比**

```bash
cd /Users/wf2311/Projects/wf2311/fm/tickflow-stock-panel/backend
for mode in postgres duckdb; do
  echo "=== FQUANT_FSTORE_MODE=$mode ==="
  FQUANT_FSTORE_MODE=$mode uv run python - <<'PY'
from app.data_providers.fquant_provider import FQuantProvider
p = FQuantProvider()
print("instruments:", p.get_instruments("stock").height)
print("financial income:", p.get_financial("600519.SH", "income").height)
print("realtime:", p._get_fstore_realtime(["600519.SH"]))
PY
done
```

预期：两种模式下 `instruments`/`financial income` 行数一致，`realtime` 里 `price`/`zdfd` 等数值一致。

- [ ] **步骤 2：跑一遍 engine 五个数据集的 http/duckdb 对比**（任务 5 步骤 6 已经做过一次，这里是最终验收，跑全量五个方法）

```bash
cd /Users/wf2311/Projects/wf2311/fm/tickflow-stock-panel/backend
for src in "" duckdb; do
  echo "=== FQUANT_ENGINE_DATA_SOURCE=$src ==="
  FQUANT_ENGINE_DATA_SOURCE=$src uv run python - <<'PY'
from app.data_providers.fquant_provider import FQuantProvider
p = FQuantProvider()
print("day:", p._engine.get_day("600519", limit=2))
print("wide:", p._engine.get_wide("600519", limit=2))
print("minutes:", p._engine.get_minutes("600519", "20260706", limit=2))
print("trans:", p._engine.get_trans("600519", "20260706", limit=2))
print("xdxr:", p._engine.get_xdxr("600519", limit=2))
PY
done
```

预期：两种数据源下 `open`/`close`/`price`/`volume` 等核心字段数值一致；`duckdb` 下 `wide` 的 `datetime`/`adjustment_count` 恒为 `None`/`0`，`trans` 的 `order_count` 恒为 `None`（都是已知限制，不是回归）。

- [ ] **步骤 3：全量单测**

运行：`uv run pytest backend/tests -v -k "duckdb"`
预期：本机挂载了 `/Volumes/WD1` 时全部 PASS，未挂载时全部 SKIP（不是 FAIL）。

- [ ] **步骤 4：跑一遍完整测试套件确认没有破坏现有行为**

运行：`uv run pytest backend/tests -x -q`
预期：无新增 FAIL（相对本计划开始前的基线）。

---

## 任务 8：补齐两处自检发现的遗漏方法

**背景（2026-07-07 自检发现）：** 任务 1-7 覆盖的是最初 inventory 列出的方法，重新过了一遍
`self._fstore.query(` 和 `self._engine.` 的全部调用点后，发现两处漏掉的方法。

### 8.1 `_get_raw_oracle_rows`（`fquant_provider.py:343-382`）

这个方法查两张表，只有一半能在 DuckDB 模式下工作：

```python
day_rows = self._fstore.query(
    "... FROM t_1_day_klines WHERE code = %s AND ktype = 101 AND fq = 0 AND tdate BETWEEN %s AND %s ...",
    (code, dates[0], dates[-1]),
)
market_rows = self._fstore.query(
    "... FROM t_1_daily_markets WHERE code = %s AND tdate BETWEEN %s AND %s ...",
    (code, dates[0], dates[-1]),
)
```

`t_1_day_klines` 那部分已经实测确认能在 DuckDB 下正常工作（`::text`/`::float8` 这类 PG cast
语法 DuckDB 原生支持，直接拿真实数据验证过）。但 `t_1_daily_markets` **在 fstore.duckdb 里根本
不存在这张表**——`t_{n}_daily_markets` 这一族已经被合并进无前缀物理表 `daily_markets` 长表了。`FStoreDuckDBClient.query()`
遇到查询失败会捕获异常返回空列表，不会抛错，但 `market_rows` 会静默变成空列表，`_get_raw_oracle_rows`
的 `by_date.update(...)` 那一步就拿不到 `market_rows` 的数据，只剩 `day_rows` 的字段。

- [ ] **步骤 1：把 `market_rows` 的查询目标从 `t_1_daily_markets` 改成 `daily_markets`**

```python
market_rows_raw = self._fstore.query(
    """
    SELECT
        trade_date::text AS date,
        CAST(NULLIF(payload_json->>'Jrkpj', '') AS DOUBLE) AS oracle_open,
        CAST(NULLIF(payload_json->>'Zgj', '') AS DOUBLE) AS oracle_high,
        CAST(NULLIF(payload_json->>'Zdj', '') AS DOUBLE) AS oracle_low,
        price AS oracle_close,
        CAST(NULLIF(payload_json->>'Cjl', '') AS DOUBLE) * 100 AS oracle_volume,
        CAST(NULLIF(payload_json->>'Cje', '') AS DOUBLE) AS oracle_amount
    FROM daily_markets
    WHERE asset_type = 1 AND code = %s AND trade_date BETWEEN %s AND %s
    """,
    (code, dates[0], dates[-1]),
)
```

这条 SQL 只在 `FQUANT_FSTORE_MODE=duckdb` 时使用；`postgres` 模式下 `daily_markets`/
`payload_json` 都不存在，需要在 `_get_raw_oracle_rows` 里按 `isinstance(self._fstore, FStoreDuckDBClient)`
分支选择原 SQL（`t_1_daily_markets`）还是新 SQL（`daily_markets`），模式和任务 4 里
`_query_fstore_realtime_rows` 的写法一致，直接照抄那个分支模式。

- [ ] **步骤 2：新建测试**

```python
def test_get_raw_oracle_rows_duckdb_uses_daily_markets(monkeypatch):
    monkeypatch.setenv("FQUANT_FSTORE_MODE", "duckdb")
    provider = FQuantProvider()
    rows = provider._get_raw_oracle_rows("600519", [{"date": "2026-07-01"}, {"date": "2026-07-03"}])
    assert len(rows) > 0
    assert "oracle_close" in rows[0]
```

- [ ] **步骤 3：Commit**

```bash
git add backend/app/data_providers/fquant_provider.py backend/tests/data_providers/test_fquant_provider_duckdb_integration.py
git commit -m "fix(fquant-provider): _get_raw_oracle_rows uses daily_markets under duckdb mode (t_1_daily_markets doesn't exist)"
```

### 8.2 `get_moneyflow_range`/`get_moneyflow_daily` 用到的 `get_fund_range`/`get_fund_daily`

这两个方法完全不在 `get_day/get_wide/get_minutes/get_trans/get_xdxr` 五个契约里，
`EngineDataDuckDBClient` 没实现。代码本身有 `hasattr` 防御（`fquant_provider.py:1110,1129`），
切到 `FQUANT_ENGINE_DATA_SOURCE=duckdb` 不会报错，但 `get_moneyflow_range` 会直接静默返回空
df——功能性倒退，不是崩溃，容易被忽略。`tdx.duckdb` 里有张 `market_fund_flow` 表（61 万+行，
列为 `code, market, trade_date, main, main_ratio, super_large, super_large_ratio, large,
large_ratio, medium, medium_ratio, small, small_ratio, source_path`），字段形状和
`get_fund_daily`/`get_fund_range` 想要的"主力/超大单/大单/中单/小单净流入分类"对得上，具备
接入条件，但两份计划都没提到这张表，这里先补上。

- [ ] **步骤 1：在 `EngineDataDuckDBClient` 里新增两个方法**

```python
    def get_fund_daily(self, code: str, date_iso: str) -> dict | None:
        """读 market_fund_flow 单日数据，字段对齐 get_fund_daily 期望的净流入分类。"""
        conn = self._tdx.get()
        if conn is None:
            return None
        try:
            cursor = conn.execute(
                """
                SELECT main, main_ratio, super_large, super_large_ratio, large, large_ratio,
                       medium, medium_ratio, small, small_ratio
                FROM market_fund_flow
                WHERE code = ? AND trade_date = ?
                """,
                [_prefixed_code(code), date_iso],
            )
            row = cursor.fetchone()
        except Exception as e:  # noqa: BLE001
            logger.warning("EngineDataDuckDBClient: get_fund_daily 查询失败 — %s", e)
            return None
        if row is None:
            return None
        keys = ["main", "main_ratio", "super_large", "super_large_ratio", "large", "large_ratio",
                "medium", "medium_ratio", "small", "small_ratio"]
        return dict(zip(keys, row))

    def get_fund_range(self, code: str, start_date: str, end_date: str):
        """读 market_fund_flow 区间数据，返回 polars DataFrame（对齐 get_fund_range 契约）。"""
        import polars as pl

        conn = self._tdx.get()
        if conn is None:
            return pl.DataFrame()
        try:
            cursor = conn.execute(
                """
                SELECT trade_date, main, main_ratio, super_large, super_large_ratio, large,
                       large_ratio, medium, medium_ratio, small, small_ratio
                FROM market_fund_flow
                WHERE code = ? AND trade_date BETWEEN ? AND ?
                ORDER BY trade_date
                """,
                [_prefixed_code(code), start_date, end_date],
            )
            rows = cursor.fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("EngineDataDuckDBClient: get_fund_range 查询失败 — %s", e)
            return pl.DataFrame()
        if not rows:
            return pl.DataFrame()
        cols = ["trade_date", "main", "main_ratio", "super_large", "super_large_ratio", "large",
                "large_ratio", "medium", "medium_ratio", "small", "small_ratio"]
        return pl.DataFrame([dict(zip(cols, r)) for r in rows])
```

**注意：** `get_fund_range` 的真实返回类型（`pl.DataFrame` 还是 `list[dict]`）要先看
`EngineDataDiskClient.get_fund_range`（如果它实现了这个方法）的真实签名和调用方
`get_moneyflow_range` 怎么处理返回值再确定，上面只是按 `get_moneyflow_range` 直接
`return self._engine.get_fund_range(...)` 的用法（隐含调用方期望 DataFrame）写的示意实现，
不要假设这个签名一定对，实现前先读一遍 `get_moneyflow_range` 的完整上下文确认。

- [ ] **步骤 2：新建测试**

```python
@pytest.mark.skipif(not os.path.exists(TDX_PATH), reason=f"本机没有 {TDX_PATH}")
def test_get_fund_daily_returns_dict():
    client = EngineDataDuckDBClient()
    result = client.get_fund_daily("600519", "2026-07-06")
    if result is not None:
        assert "main" in result
```

- [ ] **步骤 3：Commit**

```bash
git add backend/app/data_providers/fquant/engine_data_duckdb_client.py backend/tests/data_providers/test_engine_data_duckdb_client.py
git commit -m "feat(fquant-provider): add get_fund_daily/get_fund_range to EngineDataDuckDBClient via market_fund_flow"
```
