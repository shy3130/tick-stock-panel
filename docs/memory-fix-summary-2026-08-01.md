# 后端内存异常修复过程（2026-08-01）

## 背景

`tickflow-stock-panel` 后端的 `python3.13` 进程在持续运行约 10 小时后，占用大量压缩内存。

初始现场快照：

| 项目 | 观测值 |
| --- | ---: |
| 物理内存 | 36 GiB |
| 已使用 | 约 35 GiB |
| 空闲 | 约 187 MiB |
| 内存压缩器 | 约 11 GiB |
| 高内存 Python | PID 77674，`top` 显示约 28 GiB |
| Python 进程历史峰值 | `vmmap` 显示 physical footprint peak 106.5 GiB |

该进程属于本项目后端，而不是未知程序：

```text
bash
└─ uv run uvicorn app.main:app
   └─ python3.13
```

工作目录为 `backend/`，运行模式为 `DATA_PROVIDER=fquant_local`。

## 根因与证据

### 1. 异常 enriched 分区存在指数级重复行

对 `data/kline_daily_enriched/date=2026-07-24/part.parquet` 做只读统计后，确认：

| 指标 | 修复前 |
| --- | ---: |
| 总行数 | 33,559,956 |
| 不重复 `(symbol, date)` 键数 | 5,526 |
| `000001.SZ` 重复行数 | 16,777,216 |
| `000680.SZ` 重复行数 | 16,777,216 |

两只股票的重复记录逐列一致。异常分区使原本应为数千行的单日数据被扩大到数千万行。

### 2. 全历史 enriched 被常驻引用

`KlineRepository` 在启动期会加载并长期保留 enriched 历史帧。异常分区进入 Polars 历史缓存后，内存无法随请求结束释放；因此进程空闲时 CPU 接近 0%，但仍长期保留大量内存。

`vmmap` 将大部分区域标为 `CoreMedia Capture Data`。这只是 macOS 对匿名分配区域的归类，不能据此推断代码使用了屏幕、摄像头或视频采集；抽样未发现后端直接调用这些媒体 API。经修复前后对照，重复 parquet 与全历史强引用才是可复现且可消除的主因。

## 修复措施

### 1. 写入与计算路径强制自然键唯一

在 `backend/app/indicators/pipeline.py` 和 `backend/app/storage/repository.py` 中，所有 enriched 计算、staging、全量/增量写入、merge-upsert 及 live flush 路径统一执行：

```python
df.unique(subset=["symbol", "date"], keep="last", maintain_order=True)
```

约束为：

- 数据为空或没有键列时保持原样；
- 只在实际移除重复行时记录前后行数；
- `keep="last"` 与现有 merge-upsert 语义一致；
- 新建分区同样受保护，不能依赖“已有目标文件”才去重。

同时抽取窄范围 `change_pct` 计算路径，避免为 RPS 等只需要价格变动字段的请求计算完整技术指标。

### 2. 移除无界历史常驻，改为有界按需读取

`KlineRepository` 不再持有 A 股/HK 的全历史 enriched DataFrame。启动后仅保留：

- 最新交易日 enriched 看板缓存；
- live aggregate；
- instruments 缓存；
- 计算 latest/live 所需的临时局部帧。

历史请求统一经 `get_enriched_range()` 使用 DuckDB/Parquet 扫描：

- 先应用日期、symbol 和列投影；
- 仅存储列直接读取目标日期范围；
- 价格变动字段仅读取暖机窗口并计算所需列；
- 其他派生指标才走完整计算路径；
- 历史遗留重复数据读取时仍按 `(symbol, date)` 去重。

`ScreenerService` 的历史窗口缓存改为带锁的 LRU，最多保留 2 项、总估算大小不超过 256 MiB；单项超过上限时正常返回结果但不缓存。`BacktestEngine.PanelCache` 也保留 2 项/180 秒 TTL，并增加 512 MiB 总字节上限与超大结果绕过规则。

### 3. DuckDB 统一资源预算与关闭链

新增 `backend/app/storage/duckdb_runtime.py`，集中创建 DuckDB 连接，默认配置为：

```text
DUCKDB_MEMORY_LIMIT=2GB
DUCKDB_THREADS=4
```

`DataStore`、FStore DuckDB 客户端、TDX 客户端、catalog 解析器和筛选 SQL 连接均复用该配置。应用 lifespan 和 MCP 退出路径会依次停止 worker，再关闭 provider、筛选 SQL 连接与 `DataStore`，避免数据库实例跨服务生命周期遗留。

### 4. 原子修复已确认的异常分区

仅处理 `2026-07-24` 这一个已验证异常分区，避免无证据的全历史重写：

1. 以 `shutil.copy2` 创建同文件系统备份：
   `part.parquet.pre-dedup-20260801.bak`；
2. 在临时 DuckDB 连接中以 `memory_limit=512MB`、`threads=2` 执行全行 `DISTINCT`；
3. 将结果写入同目录临时 parquet；
4. 验证临时文件有 5,526 行，且行数等于 distinct `(symbol, date)` 键数；
5. 使用 `os.replace()` 原子替换原文件。

修复后该分区为 5,526 行、5,526 个自然键；全量 enriched 442 个 parquet 文件合计从 35,608,867 行降为 2,054,437 行。备份文件保留且后缀不是 `.parquet`，不会被 `**/*.parquet` 读取路径匹配。

### 5. 随回归暴露并修正的边界问题

完整回归首次执行时，修复了以下与现行行为不一致或拆分库迁移后失效的测试/实现：

- `extend_minute_history` 已不再有 capability 门控，测试改为验证当前“由 provider fail-closed”的契约；
- 缺失分钟 `datetime` 时按行序生成时间戳，避免多个空时间行都落到 `09:31`；
- `chuquan_chuxi` 已位于 `fstore-extended.duckdb`，FStore 连接补充该临时别名；
- `join_asof` 输入在历史换手率修复前按 `symbol/date` 排序，保证点时股本匹配确定；
- 指数日线测试显式关闭本地直连模式，以验证其实际目标的 live fallback 分支；
- 分钟 provider 测试的假引擎分别模拟 `RouteNotFoundError` 与 `StaleCatalogError`。

## 验证过程与结果

### 聚焦测试与真实数据烟测

修复后针对重复写入、按需历史读取、RPS、缓存字节上限、DuckDB 连接关闭与 provider 生命周期运行聚焦测试；随后用真实数据启动 `DataStore`：

```text
latest_rows: 5528
latest_date: 2026-07-31
live_rows: 5528
latest_mb: 3.9
has_history_attr: False
```

说明启动后的公开 latest/live 数据仍可用，且已删除全历史缓存属性。

### 受控服务验证

在 `127.0.0.1:3019` 启动受控服务并等待稳定后：

- `/health` 返回 `status=ok`、`mode=fquant_local`；
- 连续两次请求 `/api/rps/rotation?days=12` 均返回 12 个日期、12 个列、392 个概念；
- 第二次请求耗时约 0.04 秒，证明结果/窗口缓存命中；
- 服务正常关闭，端口释放，关闭日志没有数据库关闭异常。

### 全量回归

从 `backend/` 运行：

```bash
uv run pytest -p no:cacheprovider -q
```

结果：

```text
580 passed
```

### 修复版服务内存复测

旧实例停止后，修复版服务重新由受管进程 `tickflow-stock-panel` 运行在 `127.0.0.1:3018`。

| 快照 | Python 内存 | 压缩内存 | 说明 |
| --- | ---: | ---: | --- |
| 修复前旧实例 | 28–39 GiB | 28–39 GiB | 长时间运行后出现异常膨胀 |
| 修复后启动稳定 | 约 4.2 GiB | 0 B | `vmmap` physical footprint 约 4.1 GiB |
| RPS 历史请求后 | 约 4.3 GiB | 0 B | 请求后没有重回数十 GiB |

同一时段系统空闲内存从不足 1 GiB 恢复到约 6.5 GiB，内存压缩器从约 11–15 GiB 降至约 2.7 GiB。

> 说明：4.1 GiB 比初始“受控启动低于 4 GiB”的目标高约 0.1 GiB，但相对旧实例的 28–39 GiB 已消除异常数量级增长；后续应继续以长期运行曲线而非单次启动快照评估稳定性。

## 回滚与后续观察

### 回滚

如需回滚异常分区，先停止写入该分区的后端进程，再将备份原子替换回去：

```text
data/kline_daily_enriched/date=2026-07-24/part.parquet.pre-dedup-20260801.bak
```

源码侧去重与有界缓存防线不应回滚；否则新的异常输入仍可能再次造成无限历史常驻。

### 观察项

建议在正常盘后 pipeline、重复 RPS/筛选/回测请求以及至少一个交易日的持续运行中记录：

- Python physical footprint 和 `CMPRS`；
- `KlineRepository` latest/live 缓存大小；
- Screener 与 Backtest 缓存的项数、估算字节数及 LRU 驱逐；
- enriched 新分区的行数与 distinct `(symbol, date)` 键数；
- DuckDB 内存预算拒绝或连接关闭异常。

若 Python 再次持续增长，应先关联具体接口和缓存键，再进行定点 heap/VM 采样；不要仅根据 macOS 的 `CoreMedia Capture Data` 标签判断根因。
