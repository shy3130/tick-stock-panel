# fstore/engine DuckDB 数据源覆盖范围

## fstore（`FQUANT_FSTORE_MODE=duckdb`，默认 postgres）

已覆盖（见 2026-07-06 任务 1-4，**下列方法均已实现，但部分需要非零代码改动**）：

- `get_instruments`（`base_infos`）—— **零代码改动**
- `get_daily` 的 fstore 兜底路径（`_get_daily_from_fstore_klines`，`t_1_day_klines`/`t_20_day_klines`/`day_klines`）—— **需要客户端支持**（见下文）
- `get_adj_factors` 的 fstore 兜底路径（`_get_adj_events_from_fstore`，`chuquan_chuxi`）—— **需要 DuckDB 特定 SQL 分支**（见下文）
- `get_financial`（`financial_report_*`；`forecast` 表本身是空表，两个数据源下都返回空 df，不是回归）—— **零代码改动**
- `_get_universe_codes_from_chengfen_gu`（`chengfen_gu`）—— **零代码改动**
- `get_universe_constituents`（`chengfen_gu_items`）—— **零代码改动**
- `_get_fstore_realtime`（`daily_markets`，见任务 4）—— **需要 DuckDB 特定 SQL 分支**（JSON 解析）

### 需要修复的方法详情

#### `_get_daily_from_fstore_klines` 客户端参数适配

DuckDB 的 Python 驱动对 VARCHAR 列的类型绑定较严格：Python `date`/`datetime` 对象无法直接作为查询参数（PostgreSQL 的 psycopg 驱动会自动适配，但 DuckDB 不会）。

**修复方案**（`FStoreDuckDBClient.query()` 透明处理）：在执行前自动将所有 `date`/`datetime` 对象转换为 ISO 格式字符串（见 `fstore_duckdb_client.py` 第 91-104 行 `_convert_params` 方法）。调用方（`fquant_provider.py`）无需改动，直接传递 Python 日期对象如常，客户端侧自动转换。

#### `_get_adj_events_from_fstore` DuckDB TIMESTAMPTZ 兼容性

`chuquan_chuxi.t_date` 在 DuckDB 中存储为 `TIMESTAMPTZ` 类型。DuckDB 的 Python 驱动将 `TIMESTAMPTZ` 物化为 Python 对象时需要 `pytz` 包，但 `pytz` 不是本仓库的核心依赖（仅作为可选依赖间接拉入）。

**修复方案**（`fquant_provider.py` 第 519-564 行）：在 `_get_adj_events_from_fstore` 中增加 DuckDB 分支，对该列做 `CAST(t_date AS DATE)` 转换，绕开 TIMESTAMPTZ 物化。PostgreSQL 分支保持原样（现有 SQL 已正常工作）。这是逻辑分支，不会对调用方或返回数据格式有影响。

**关键注意**：若无此修复，缺少 `pytz` 依赖时，该方法会抛异常但被 `FStoreDuckDBClient.query()` 的全局 `except Exception: return []` 捕获并静默吞掉，导致出现"找不到调整事件"的现象而非明确的错误，这种静默失败模式对后续调试回归时较难追踪根因。

---

## engine（`FQUANT_ENGINE_DATA_SOURCE=duckdb`，默认 http，与 `engine_mode` 完全独立）

已覆盖（2026-07-07 起，见任务 5）：`get_day`（`tdx.duckdb` 的 `market_day_kline`）、`get_wide`（`market_wide_kline`）、`get_minutes`（`tdx-minutes.duckdb` 的 `market_minutes`）、`get_trans`（`tdx-trans.duckdb` 的 `market_transactions`）、`get_xdxr`（`market_xdxr`）。

### 各数据集特性与已知限制

#### `get_day`（`market_day_kline`）

字段对齐 HTTP EngineDataClient，包括 `datetime`/`adjustment_count`。无已知数据问题。

#### `get_wide`（`market_wide_kline`）

- `datetime` 和 `adjustment_count` 列不存在，实现中固定填 `None`/`0`（见 `engine_data_duckdb_client.py` 第 116-160 行）。调用方字段归一函数需能容忍这两个字段缺失。
- **已知限制：表级导入延迟约 2 个交易日**。在实测中，`market_wide_kline.MAX(trade_date)` 稳定比 `market_day_kline.MAX(trade_date)` 晚 2 个交易日（例如同一批股票代码，`market_day_kline` 最新数据为 2026-07-07，`market_wide_kline` 则为 2026-07-03）。这是 engine 侧导入流水线的上游问题，本客户端无法修复。因此 `get_wide` 的结果可能缺少近期交易日数据，即使这些数据在 `get_day` 或 HTTP 路径中已存在。

#### `get_minutes`（`market_minutes`）

- 字段对齐 HTTP EngineDataClient 的 `price`/`volume`。
- `time`/`amount` 两列全表 34 亿+行全是 NULL（已实测确认），实现中不查询这两列。

#### `get_trans`（`market_transactions`）

- 字段对齐 HTTP EngineDataClient。`order_count` 列不存在，实现中固定填 `None`。
- `direction` 直接透传 `side` 列值，无映射转换。
- **已确认 `side` 取值范围**：`{0, 1, 2, 3, 5, 8}`（之前报告为 `{0, 1, 2, 5, 8}`，但实测发现极少量 `3` 值存在，占总 926,909,252 行（约 9.27 亿）中的微小比例）。两个值都直接透传，无特殊处理。
- **已知语义冲突**（不在本计划范围内解决）：`../fm-cli` 的 `internal/cli/stock/engine_data.go` 里 `directionLabel` 把 `direction=1` 标成卖出、`2` 标成买入；如果 tickflow-stock-panel 其它地方（前端展示、`trans_rows_to_df` 下游消费者）理解为相反映射，需找权威定义统一。本次改造只做如实透传。

#### `get_xdxr`（`market_xdxr`）

- 表列名是 `xingquanjiya`（比 HTTP 契约的 `xingquanjia` 多一个 `ya`），实现中用 `AS xingquanjia` 对齐字段名。
- **已知数据缺失**：`xingquanjia` 列当前全表都是 NULL（engine 侧写入用错了列名把真实数据写丢了），所以本客户端返回的该字段恒为 None。等 engine 仓库修好表结构/回填数据后才会有真实值。

### 代码→交易所前缀映射的已知限制

engine 数据表中的 `code` 列都带交易所前缀（如 `sh600519`），而 FQuantProvider 传入的是裸代码（如 `600519`）。`EngineDataDuckDBClient` 用 `_PREFIX_BY_HEAD` 字典（见 `engine_data_duckdb_client.py` 第 27-31 行）做前缀补全。

**已知限制：仅覆盖 A 股代码段**：
- 沪深主板/科创/创业板：60/68/90→sh；00/30/20→sz；43/83/87/92→bj
- **不覆盖**：ETF 代码（上交所 ETF 5xxxxx，深交所 ETF 15xxxxx/16xxxxx）、指数代码等

**影响**：调用 `get_day`/`get_wide`/`get_minutes`/`get_trans`/`get_xdxr` 时，传入 ETF/指数代码会因前缀补全失败而返回 `[]`，即使底层数据表中相应代码的数据存在（例如 `get_day("510300")` 返回 `[]` 尽管 `market_day_kline` 中 `sh510300` 有 3,428 行真实数据）。这是传入代码类型识别的设计限制，非 bug。

**背景**：这个限制与同一代码库 `symbols.py` 中 `exchange_of()` 函数的类似限制相同（都缺少 ETF/指数前缀映射），反映的是整体设计中对 A 股代码的优先级。本次计划经权衡决定按现状文档化，不在此分支中扩展前缀映射。

---

## 明确不覆盖

### fstore 侧

- `get_daily`/`get_adj_factors` 的**主路径**是 engine-data（`wide`/`xdxr`），只有主路径不可用时才降级到 fstore。本计划不改变 engine-data 主路径的行为。
- `get_by_universes`/涉及 `t_N_money_flow_minutes`/`hsgt_money_flow` 之类的其它 fstore 表（参见 `docs/data-query-inventory-local-source.md` §10.2 的其余表列表）不在本次范围内。如果以后要接入 DuckDB 源，应该复用任务 1 的 `FStoreDuckDBClient`，不要新建第三个 fstore 客户端。

### engine 侧

- `get_by_universes` 之外的 engine 相关能力（比如筹码 `chips`）不在 `tdx*.duckdb` 里，也不在本计划范围内。
- TDX 磁盘本地源迁移（`docs/data-query-inventory-local-source.md` 里的 P0-P4 计划）是另一条独立路线，处理的是完全不同的上游（TDX CSV vs engine 侧的 DuckDB 导出），两者不冲突，不要合并成一个开关。
