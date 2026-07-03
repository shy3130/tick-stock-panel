# 设计方案：本地磁盘数据源模式（engine-data 直读磁盘 + sina/tencent 实时）

- **日期**：2026-07-02
- **性质**：设计方案（后续实现的依据）
- **取代关系**：搁置《2026-07-02-strategy-dsl-and-fquant-datasource-design.md》的"完全迁移"路线；本方案是其数据源部分的**近期务实版**——在现有 Python 项目内新增可切换的"本地数据源"模式，原有方式（tickflow / fquant-http）全部保留。

## 0. Codex 迁移进度盘点（commit `12d1c98`，实测核对）

| 已完成 | 证据 |
|--------|------|
| Provider 架构（fquant v2，8 capability） | `fquant_provider.py` 593→1000+ 行，16 项测试 |
| 7 个 service 解耦 | `FQUANT_INTEGRATION_PROGRESS.md` 阶段 2 ✅ |
| **切换基建**：`DATA_PROVIDER` env > preferences 持久化；且已有 **per-capability 偏好**（`daily/minute/realtime_data_provider`） | `registry.get_active_provider_name()`、`preferences.py:102-126` |
| realtime：tdx-api **批量**（逗号拼 code）→ fstore `daily_markets` 兜底 | `fquant_provider.get_realtime` |
| 设置页切换 UI | `settings/System.tsx`、`api/settings.py` |

**未做（即本方案范围）**：① engine-data 仍是 HTTP `:8099` 桥接；② 无 sina/tencent 实时源；③ 本地模式下仍走"拉取→落 `data/kline_daily` parquet"的抓取环节。

## 1. 磁盘数据实测（`/Volumes/vol3/tdx/`，今晚迁至外接磁盘）

```
day/{sh600,sz300,bj899,sh000,sh880,...}/sh600519.csv   ← 日K，按前缀分桶
xdxr/{同分桶}/sh600519.csv                              ← 除权事件
minutes/{年份}/... · 5min/{年份}/...                    ← 分钟数据
```

- **day CSV 列**：`date,open,close,high,low,volume,amount,up,down,datetime,adjustment_count`
- ⚠️ **关键语义**：day CSV 是 **TDX 减法前复权序列**（茅台 2001 年价格为 -313、`adjustment_count=30`；最新行 `adjustment_count=0` 为原始价）。**panel 流水线需要原始价**（涨停/炸板判定依赖 `raw_close`）+ 自算前复权。
- **xdxr CSV 列**：`Date,Category,Name,FenHong,PeiGuJia,SongZhuanGu,PeiGu,...,FenShu,XingQuanJia` —— 与现有 `mapping.py`/`adj_factor.py` 消费的 engine-data xdxr 字段**同源同义**，映射代码可直接复用。
- ⚠️ **性能约束**：SMB 挂载上目录扫描极慢（`find` 2 分钟超时）。适配器必须**由 symbol 直接构造文件路径**（`600519.SH → day/sh600/sh600519.csv`），全程零目录扫描。迁到外接磁盘后此约束依然是好习惯。

## 2. 设计决策

### D-L1 模式形态：fquant provider 的"源模式"，不是新 provider 类

在 registry 注册第三个名字 **`fquant_local`**，实例化的仍是 `FQuantProvider`，但注入 `engine_data_mode="disk"`：

```
_PROVIDERS = { "tickflow": ..., "fquant": ..., "fquant_local": FQuantProvider(disk) }
```

理由：fquant_provider 的映射/降级/符号归一全部复用，唯一换掉的是 engine-data 客户端；以 provider 名字区分可复用切换基建的**框架**。

⚠️ **codex review 修正（2026-07-02）**："+1 行注册零改动"不成立，完整切换入口清单如下，全部要同步：
- `registry.py:39` `get_provider()` 把注册值当**无参 callable** 调——需引入工厂/参数注入约定（如 `lambda: FQuantProvider(engine_mode="disk")`）；
- `preferences.py:99` `_clean_data_provider` 白名单只认 `tickflow|fquant`；
- `api/settings.py:395` 设置 API 经同一白名单校验；
- 前端 `System.tsx:52` 下拉、`api.ts:777` union 类型均写死两项；
- **per-capability 偏好尚未接线**：`daily/minute/realtime_data_provider` 目前只是设置读写字段，服务层实际仍走全局 `get_active_provider_name()`（`kline_sync.py:31`）——D-L5 依赖它判定 daily 源之前，必须先把这条偏好接到现有 registry 入口：`get_active_provider_name(capability)`；raw 禁写只看 `get_active_provider_name("daily")`，realtime 链只看 `get_active_provider_name("realtime")`。

### D-L2 磁盘客户端：与 `EngineDataClient` 同接口的 `EngineDataDiskClient`

- 同名方法面：`get_day / get_wide / get_minutes / get_xdxr`。`get_wide` **优先读** `{TDX_DATA_DIR}/wide/...`，保留 `last_close/change_rate/inner_*/outer_*`；仅当 `wide/` 缺文件时降级读 `day/`，此时 `pre_close/change_pct` 等增强字段为空，必须记录为降级而不是主路径。
- 路径规则：`{TDX_DATA_DIR}/{wide|day|xdxr}/{prefix}/{prefix_code}.csv`，prefix = 市场缩写+代码前 2-3 位（`sh600/sz300/bj899/sh000...`），由 `symbols.py` 现有归一函数派生。
- 配置：`TDX_DATA_DIR` 环境变量（默认 `/Volumes/vol3/tdx`；**今晚磁盘迁移后只改这一个 env**）。
- 读取：polars `read_csv` 单文件（本地毫秒级）；不做缓存优先，慢再加。

### D-L3 原始价重建（**mapping 层共享修复**，grill 后升级）

⚠️ **实测升级（2026-07-02 grill）**：磁盘 CSV 与 panel `data/kline_daily` 重叠区间逐位相等（除权日前的行同带 `.075769` 前复权尾巴、`adjustment_count=1`）——证明 **fquant-HTTP 模式下 panel 的 raw 已被前复权污染，这是已上线的共性 bug**，不是磁盘模式新风险。engine-data 上游（HTTP 与磁盘同源）产出的都是 TDX 减法前复权序列。

**决策（用户已确认「统一」）**：raw 重建放在 **mapping 层**，HTTP 与磁盘两条 engine-data 路径**共用同一修复**：

**污染量级（codex 独立复核追加）**：不止 `.075769` 尾巴——600519 在 2025-10-31 磁盘/panel close=1378.03（`adjustment_count=2`），fstore fq=0 原始价=1430.01，**偏差 52 元**；除权日前整段历史都被平移，越早偏差越大。

- **主方案（V1+V2 混合）**：fstore `day_klines fq=0`（实测存到 2025-10-31）做历史 raw + 对拍 oracle；2025-11 之后的缺口用 xdxr 事件做**减法逆运算**补齐（每只股该窗口内除权事件极少，逆运算范围小）。`adjustment_count` 列作为"该行被复权过几次"的自检位。
- **修复位置约束（codex review）**：raw 重建必须发生在 **engine daily source 内部、`normalize_daily` 之前**——normalizer 只保留 8 个 canonical 列（`normalizer.py:54`），`adjustment_count`/辅助列过后即丢；且 **adj_factor 链同病**：`get_adj_factors` 用 daily close 构造 `pre_close`（`fquant_provider.py:298` → `adj_factor.py:63`），若不换成重建后的 raw close，**ex_factor 本身也是基于污染价算的**。修复面 = daily + adj_factor 两条链。
- **验收**：600519 + 一只高送转股 + 一只 ST，三只全区间 raw 对拍误差 < 0.01 元，**且 ex_factor 用重建 raw 重算后与 oracle 一致**，才允许接入涨停判定链路。
- **存量数据修复**：接入后需对 `data/kline_daily` 已污染分区（自 fquant-HTTP 切换以来同步的日期）做一次重刷 + enriched 重算。该脚本只用于迁移前/HTTP 模式历史污染修复；`fquant_local` 日常路径仍禁止写 stock raw mirror。

### D-L4 realtime 源链（新增 sina/tencent；范围含全市场，grill Q4 用户已确认 a）

**按范围分工**（保留全市场盘中能力）：

```
watchlist    : tdx-api(批量, 已有) → tencent 分片 → fstore 快照兜底
full_market  : sina 大批量(主源, ~100-800 只/请求) → tencent 分片兜底；轮询 15-30s
```

- tdx-api **不承担全市场**（自有单点，避免打爆）；连板梯队/盘中选股依赖 full_market 链。
- 新增 `data_providers/fquant/sina_tencent_client.py`：sina `hq.sinajs.cn`（需 Referer 头）+ tencent `qt.gtimg.cn`，批量分片并发。
- **realtime 契约显式化（codex review）**：所谓"normalizer realtime 契约"目前**不存在**——实际契约是 `fquant_provider._quote_row()` 的隐式字段集（`symbol/name/last_price/prev_close/open/high/low/volume/amount/timestamp/source/ext{change_pct,amplitude,turnover_rate}`），消费方 `quote_service.py:441/599` 直接按此读取并把 `last_price` 落成日 K close。实现 sina/tencent client 前，先把该字段集**提升为 `normalizer.py` 的显式 realtime 契约**，并逐字段钉死：价格缩放、volume 单位（股/手）、amount 单位、prev_close 来源、涨跌幅是小数还是百分数、指数/ETF 后缀、timestamp 时区。随后 `FQuantProvider.get_realtime` 及新增 client 输出必须统一走 `normalize_realtime()`；否则"页面能显示"不代表写盘/监控/涨停计算正确。
- **外部 realtime 稳定性约束**：sina/tencent 只做 provider 内受控适配器。必须有连续失败退避、批次部分失败保留成功行、真实响应样本 fixture 固化；连续失败时降级到下一源/空结果，不能持续打满 warning 或阻塞 QuoteService。
- **capability 展示语义**：`fquant_local.capabilities.realtime=True` 表示 provider 能提供 realtime 链，不表示 realtime 来自本地磁盘。设置页/进度文档需展示 `daily=TDX disk; realtime=tdx-api/sina/tencent/fstore snapshot; depth=false`。
- **红线 #2 修订**（用户已明确授权）：AGENTS.md「不要直接连接外部行情接口（Tencent/新浪）」需加注：**经 `data_providers` 抽象层内受控适配器接入 sina/tencent 实时报价是允许的**；红线的本意（业务层绕过抽象层直连）保持不变。实现时同步改 AGENTS.md。
- sina/tencent 支持批量 → watchlist 乃至全市场准实时都可行，顺带缓解原设计 ADR-0004 的单标的瓶颈。

### D-L5 本地模式跳过"抓取落盘"环节

现状链路：`kline_sync` 从 provider 拉数 → 写 `data/kline_daily` parquet → pipeline 读 parquet 算 enriched。

本地模式（`fquant_local` 为 daily 源时）：
- **取消 `data/kline_daily` parquet 镜像**——数据已在本地磁盘，不再复制一份。
- `daily_pipeline` 直接以磁盘 CSV（经 DiskClient + mapping + D-L3 raw 重建）为输入计算 **enriched**，enriched parquet 照旧落盘（它是**计算缓存**而非抓取产物，选股/回测性能依赖它，保留）。
- 单股日 K 查询（K 线页）：按需直读该 symbol 的 CSV（单文件毫秒级），不依赖 kline_daily 镜像。
- **写入点全量清单（codex review 补齐，短路必须覆盖全部）**：
  1. `kline_sync.py:177` `sync_and_persist_daily_batch` → `repo.append_daily()`
  2. `kline_sync.py:229` `sync_daily_by_quotes` → `flush_live_daily()`
  3. `daily_pipeline.py:152/174` 实时覆写与 batch 写入两分支
  4. `extend_history.py:180` 写入 + raw view 刷新
  5. `api/kline.py:403` 手动 `/api/kline/sync`、`/sync_batch`
  6. **`quote_service.py:481`** 全市场 `flush_live_daily()`（常驻，最易漏）
  7. **`quote_service.py:582`** 自选 `merge_live_daily_asset()`
  统一方案（codex double-check 后升级）：**门控收口在 repository 层**——stock raw 写入口在 repository 层单一收口：`append_daily`、`append_daily_asset("stock")`、`merge_live_daily_asset("stock")`、`flush_live_daily`、`flush_live_daily_asset("stock")` 在本地模式禁写；index/ETF raw 暂留给现有页面、统计和 fallback 路径使用。上面的写入点清单降级为验证用例列表。
- **pipeline 需要新输入路径**：`run_pipeline()` 在无 `kline_daily` parquet 时直接返回 0（`pipeline.py:800`）——D-L5 不是"短路抓取"就完，必须给 pipeline 新增"磁盘 CSV → enriched"输入分支；数据统计页对 raw 目录的假设（`api/data.py:130`）同步适配。
- **盘中当日 bar 去向**（无镜像模式）：full_market 快照只进内存缓存 + `compute_enriched_today` 增量路径，**不落 `kline_daily`**；当日正式 bar 以夜间外部脚本更新磁盘 CSV 后的盘后管道为准。
- ⚠️ **QUOTE_POOL 联动（codex review）**：`policy.py:271` 对非 tickflow provider 只要 `capabilities.realtime=True` 就自动开 QUOTE_POOL → QuoteService 全市场路径会轮询**并写盘**。sina/tencent 接通后此路径自动激活，与"不落 raw daily"冲突——写盘门控必须先于 D-L4 上线。
- tickflow / fquant-http 模式下抓取链路**原样保留**。

### D-L7 `fquant`（HTTP 桥）模式迁移后废弃标记（grill Q3，用户已确认 a）

数据迁外接磁盘 + 更新脚本改写外接磁盘后，NAS 上的 engine-data `:8099` 将**永久停更**——HTTP 模式不报错但静默腐烂。处置：
- registry 保留 `fquant`（回滚路径 + 切换基建不动），设置页标注"已停更/仅存档"。
- **D-L6 保鲜探测同样作用于 HTTP 模式**（探 engine-data 返回的最后日期），腐烂可见。
- 迁移后推荐链：默认 `fquant_local`，`tickflow` 为付费兜底，`fquant` 仅存档。

### D-L6 磁盘数据保鲜契约（grill Q2，用户已确认）

- **更新归属**：外部下载脚本负责每日更新磁盘数据（随磁盘所挂机器跑 cron）；**panel 对 `TDX_DATA_DIR` 严格只读**，永不写入。
- **保鲜探测**：provider 启动/盘后管道运行前抽查基准股（600519）CSV 最后日期；落后最近交易日超阈值 → warning + 设置页展示数据新鲜度，管道跳过当日计算。
- **调度时序**：盘后管道先探新鲜度，数据未更新则延迟重试（不与下载脚本硬绑时间）。
- **增量成本预估**：CSV 按 symbol 存全历史（无日期分区），增量 enriched 每日需读全部 ~5500 个 CSV 的尾部（单文件几千行、本地毫秒级，全量预估 10-30s，盘后可接受）；若实测慢，用文件 mtime 预筛跳过未更新文件。

## 3. 模块改动清单

| 模块 | 动作 |
|------|------|
| `data_providers/fquant/engine_data_disk.py` | **新增** DiskClient（D-L2） |
| `data_providers/fquant/raw_reconstruct.py` | **新增** 原始价重建/拼接（D-L3，含 spike 验证脚本） |
| `data_providers/fquant/sina_tencent_client.py` | **新增** sina/tencent 批量实时（D-L4） |
| `data_providers/fquant_provider.py` | engine-data 客户端可注入（http/disk）；realtime 链插入 sina/tencent |
| `data_providers/registry.py` | +1 行注册 `fquant_local` |
| `services/kline_sync.py` / `extend_history.py` / `jobs/daily_pipeline.py` | daily 源=local 时跳过抓取、直算 enriched（D-L5） |
| `api/kline.py` | 单股日 K 在 local 模式直读 CSV |
| `AGENTS.md` | 红线 #2 加注 sina/tencent 授权（D-L4）；数据源矩阵加 `fquant_local` 行 |
| `scripts/test_fquant_provider.py` | 增补 disk 模式用例（路径构造/raw 对拍/实时链降级） |

## 4. 实施顺序（codex review 后重排，依赖关系显式化）

0. **覆盖闸门**：用 fstore `base_infos` + `day_klines max(date)` 分类 `day/` 868 只缺文件；若存在 `true_gap_active_after_2025_11`，必须先降级声明或补明确 backup，不能宣称 daily 全市场可用。
1. **Spike：D-L3 raw 重建验证**（对拍 fstore fq=0，含 **ex_factor 用重建 raw 重算**的验证）→ 决定 V1 拼接 or V2 逆运算。**此步不过，后续不动工。**
2. **raw 重建落地 mapping 层**（HTTP + 磁盘共享，`normalize_daily` 之前；覆盖 daily + adj_factor 两链）+ fquant-HTTP 存量污染分区一次性重刷。此步独立于本地模式，**先修已上线 bug**。
3. **`fquant_local` 完整切换入口**：registry 工厂约定 + preferences 白名单 + settings API + 前端 union/下拉 + **`get_active_provider_name(capability)` 真正接线 per-capability 偏好** + DiskClient → `test_fquant_provider.py` disk 用例绿。
4. **写盘门控 + pipeline 磁盘输入分支**（D-L5 七写入点全覆盖 + `run_pipeline` 新输入路径 + `api/data.py` 统计适配）→ 本地模式端到端：切 `fquant_local` → 盘后管道（无抓取）→ 选股/K线/监控可用。
5. **realtime 契约显式化并接入 provider + sina/tencent client**（D-L4；**依赖第 4 步门控就绪**，否则 QUOTE_POOL 自动激活会写盘）→ watchlist / full_market 实测。
6. 文档沉淀：AGENTS.md（红线注记 + 矩阵 + `fquant_local` 来源说明）、FQUANT_INTEGRATION_PROGRESS.md 加"阶段 6：本地磁盘模式"。

## 5. 风险与开放项

- **磁盘日 K 覆盖缺口（codex 统计，已升为第 0 步硬闸门）**：以 instruments.parquet(5857) 对路径规则统计，`day/` 仅覆盖 4989 只（85.2%），868 只无文件；但基准表无退市状态字段、BJ 仅 6 条（自身可能不全），缺口构成（退市 vs 在市）不可判。必须用 fstore `base_infos` 交叉核对缺口构成；若含在市股，本地模式日 K 须声明缺口或补明确 backup。
- **fund/ 资金流可替（机会项）**：磁盘 `fund/` 有日级资金分类净额（`Main=SuperLarge+Large` 口径已实测），覆盖 4994 只；因 moneyflow 当前**零业务消费者**，可将其契约缩窄为"净额型"直接适配，替掉 502 缠身的 moneyflow HTTP。列为 P3 可选项。

- **磁盘今晚迁移**：所有路径走 `TDX_DATA_DIR`，迁移后改 env 即可；外接磁盘挂载点缺失时 provider 启动降级（capability 探测失败 → warning + 该能力为空，不崩）。
- **day CSV 负价格行**：重建公式若 V2 不收敛（TDX 加法复权的分红口径差异），退回 V1 拼接（fstore 历史 + 磁盘尾部）。
- **分钟数据**：`minutes/`/`5min/` 按年分桶的文件格式本次未实测（SMB 超时），DiskClient 的 minutes 支持列为第 2 步的附加验证项，格式不符则 minute capability 暂走 fstore 备份路径。
- **指数/板块**：`sh000/sh880/sh881/sz399` 目录已确认存在，index 日 K 同一套路径规则覆盖。
