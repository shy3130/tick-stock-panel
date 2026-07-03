# 计划方案审查发现（2026-07-03）

- **审查对象**：codex 编写的 10 份实现计划（B3、C4、C6、C7、C8、C9、C11、C12、C13、P4）。
- **审查人**：Claude（交叉核对计划对代码库的事实假设）。
- **处理约定**：**codex 当前开发任务全部结束后**再按本文档逐条修改；不打断在途开发。
- **状态图例**：`[ ]` 待改 / `[x]` 已改。

---

## 必修（开发前须澄清，否则会引入正确性/安全缺陷）

### F1 — B3：`get_daily(..., asset_type="hk")` 语义错误，可能污染港股价格

- [x] 已改：B3 计划已要求先修港股 asset_type 口径，禁止港股走 `asset_type=="stock"` 的 xdxr 重建分支。

**位置**：`docs/superpowers/plans/2026-07-03-b3-hk-pricepos-parquet.md` 任务 4（`provider: FQuantProvider(engine_mode="disk").get_daily(... asset_type="hk")`）。

**证据**：
- `AssetType = Literal["stock", "index", "etf"]`（`backend/app/data_providers/base.py:10`）——**不含 `"hk"`**，契约违规。
- `FQuantProvider.get_daily` 的 `asset_type` 参数被忽略（`backend/app/data_providers/fquant_provider.py:270`，`# noqa: ARG002`）；港股分桶路由实际靠 symbol 里的 `.HK` 经 `_tdx_name` 完成（`backend/app/data_providers/fquant/engine_data_disk.py:15-22`，market="hk" → 4 字符分组）。
- 但 asset_type **在 `_get_daily_from_engine_wide` 里又确实被消费**：`if asset_type == "stock": reconstruct_raw_rows(...)`（`fquant_provider.py:315` 附近）——用于 A 股前复权（xdxr）重建。
- 现状：P6 读港股走 `kline.py:126` 的 `provider.get_daily([symbol], ..., "stock")`，对港股也传 `"stock"`，靠港股无 xdxr 文件侥幸 no-op。

**风险**：
1. 若实现者为消掉类型报错把 B3 改回合法的 `"stock"`，港股会被 A 股 xdxr 前复权重建**污染价格**。
2. B3 传 `"hk"`（绕开重建门）与 P6 传 `"stock"` 会让**同一份港股走不同 asset_type 口径**，不一致。

**建议修法（择一，B3 计划须显式写明）**：
- 首选：把 `AssetType` 扩为 `Literal["stock","index","etf","hk"]`，并让 `get_daily` 把 asset_type 透传给 `_get_daily_from_engine_wide`（当前它内部默认 `"stock"`，未接收外部值需一并修）；同时 P6 的 `kline.py:126/349` 对港股 symbol 也改传 `"hk"`，统一口径。
- 次选：在 `_get_daily_from_engine_wide` 的重建门改为显式 `is_hk_symbol(symbol)` 判断，不再依赖 asset_type 字符串。
- **底线**：无论如何，港股绝不能走 `asset_type=="stock"` 的 xdxr 重建分支。

---

### F2 — C6：网页 reader 的 SSRF 重定向校验有洞

- [x] 已改：C6 计划与实现改为 `follow_redirects=False` 手动逐跳校验，新增内网重定向拒绝测试。

**位置**：`docs/superpowers/plans/2026-07-03-c6-document-web-reader.md` 任务 4。

**问题**：计划用 `httpx.Client(trust_env=False, follow_redirects=True)` 并声称“redirect 后再次校验最终 URL host”。自动 follow 时，**中间跳转到内网 IP 的那一跳已经被实际请求**，调用方只能观测到最终 URL——正好违背本计划“不抓内网/本机 URL”的目标。此外 hostname→IP 先校验、httpx 连接时重新解析存在 DNS-rebinding TOCTOU。

**建议修法**：
- 改 `follow_redirects=False`，手动循环跟随重定向，**逐跳**校验每一跳解析出的 IP（拒绝 private/loopback/link-local/multicast），限制最大跳数。
- 尽量对已解析的 IP 直连并带 `Host` 头，或在连接层固定 IP，缓解 DNS-rebinding（若成本高，至少在文档注明该残留风险 + 仅本机可信环境使用）。

---

### F3 — C8：stdio MCP server 缺 app_state 引导

- [x] 已改：C8 计划与实现补 headless `build_state()`，构造 DataStore/KlineRepository/cache/capabilities/StrategyEngine。

**位置**：`docs/superpowers/plans/2026-07-03-c8-mcp-server.md` 任务 1/3。

**问题**：计划把工具扩到 `run_screener`/`get_kline`/`run_backtest`/`get_market_overview`，这些都依赖 `repo`/`strategy_engine`/`quote_service`——它们在 FastAPI `lifespan` 里构建（`backend/app/main.py`：`DataStore()` + `KlineRepository` + `repo.refresh_cache()` + strategy engine + `detect_capabilities`）。但 `python -m app.mcp_server` 运行在 lifespan **之外**。现有 `agent_tools.call_tool(name, app_state, args)` 只有 2 个读 state 属性的平凡工具（`get_capabilities`/`list_strategies`，`backend/app/services/agent_tools.py:20-37`）。计划未说明 stdio 进程如何构造已初始化的 app_state，扩展工具一调即 `AttributeError`。

**建议修法**：C8 增补一个任务——提供 headless 引导函数（构建最小 DataStore + KlineRepository（含 cache 预热）+ strategy engine + capabilities，组装成一个轻量 state 对象喂给 `call_tool`），或让重工具直接调用 service 层函数而非依赖 app.state。首批工具应先只上线不依赖重数据层的只读工具，重工具在引导落地后再开。

---

## 次要（不阻断，改动时顺手处理）

### F4 — C12 依赖 C3 未声明 + suggest host 需单独进白名单

- [x] 已改：C12 计划注明依赖 C3 allowlist，并要求 suggest host 单独进白名单；实现复用 `eastmoney_client`。

**位置**：`docs/superpowers/plans/2026-07-03-c12-symbol-search.md` 现状证据 + 任务 3。

**问题**：C12 称“复用 eastmoney_client 的 host allowlist、trust_env=False、节流”，但 host allowlist 与 `get_datacenter_paged` 是 **C3 任务 0** 才加入的；当前 `backend/app/services/eastmoney_client.py` 仅 34 行、无 allowlist。且 suggest 端点 host（如 `searchadapter.eastmoney.com` / `searchapi.eastmoney.com`，以实测为准）与 C3 datacenter host 不同，需**单独加入白名单**。

**建议修法**：C12 现状证据处标注“依赖 C3 任务 0 先落地”；任务 3 显式加“把 suggest host 追加进 `_ALLOWED_HOSTS`”一步。

### F5 — 跨层 asset_type 处理不一致（F1 根因，记录备查）

- [x] 已改（计划层）：B3 已显式要求 provider 层和磁盘层 asset_type 对齐；代码修复随 B3 实施。

磁盘层 `EngineDataDiskClient.get_wide(code, limit, asset_type)` 接受并使用 `asset_type`（`engine_data_disk.py:34-45`），而 provider 层 `get_daily` 丢弃它（F1）。P4 任务 3 用磁盘层调用（正确），B3 用 provider 层（踩坑）。修 F1 时一并让两层对 asset_type 的处理对齐。

---

## 需求补充（用户明确要求，须并入 C12）

### F6 — C12：搜索需支持拼音 / 拼音首字母，但计划未落地

- [ ] 待改

**位置**：`docs/superpowers/plans/2026-07-03-c12-symbol-search.md` 目标 + 任务 1。

**问题**：C12 目标写了“代码/名称/**拼音**模糊搜索”，但任务 1 的匹配字段只有 `symbol/code/name`——**拼音完全没实现**。用户明确要求：搜索标的时支持拼音（全拼，如 `guizhoumaotai`）和拼音首字母（简拼，如 `gzmt`）。

**证据**：
- `data/instruments/instruments.parquet` 列为 `symbol/name/code/exchange/asset_type/source/as_of`（5535 行）——**无任何拼音列**。
- `pyproject.toml` / `uv.lock` **无拼音库依赖**（无 `pypinyin`）。
- 名称含需归一的字符：如 `万 科Ａ` 带**全角空格 + 全角字母 Ａ**（实测样本）；转拼音前必须先归一。

**建议落地设计（C12 补两个任务）**：

1. **预计算拼音列（首选，避免每次查询现算）**：
   - 在 `instrument_sync` 写 `instruments.parquet` 时，新增两列：
     - `name_pinyin`：全拼小写连写（`平安银行` → `pinganyinhang`）。
     - `name_initials`：拼音首字母（`平安银行` → `payh`）。
   - 用 `pypinyin`（`lazy_pinyin` + `Style.FIRST_LETTER`）。依赖加到 sync/backtest extra 或核心（体量小、纯 Python），**不放进每次查询的热路径**。
   - 转拼音前先归一 name：全角→半角（`unicodedata.normalize("NFKC", name)`）、去空格、去 `ST/*ST/退` 等噪声可选保留。
   - 多音字：`pypinyin` 尽力而为即可，搜索容错，不追求 100% 正确。
   - **替代方案（若不想动 sync/schema）**：首次搜索时懒构建拼音 sidecar 索引（内存 + 落一份缓存文件），随 `instruments.parquet` mtime 变化重建；权衡是首次搜索有一次性构建延迟。二选一，实现时定。

2. **匹配与排序扩展**（任务 1 的排序表补拼音档位）：
   - 精确 code > symbol 前缀 > name 子串 > **全拼前缀 > 全拼子串 > 首字母前缀** > 其他。
   - 首字母匹配要求 query 为纯 ASCII 字母（`gzmt`），避免和 code 数字混淆。
   - 测试补：`test_search_by_pinyin_full`（`guizhoumaotai` 命中贵州茅台）、`test_search_by_initials`（`gzmt` 命中）、`test_fullwidth_name_normalized`（`万 科Ａ` 能被 `wanke`/`wk` 命中）。

3. **Eastmoney suggest fallback**：suggest 接口本身接受拼音/简拼输入，本地拼音命中不足时仍可走 F4 的 suggest 补全，无需为拼音单独加外部调用。

---

## 审查通过（无需改动，记录以示已核）

| 计划 | 结论 |
|---|---|
| **C4** Alpha Zoo | 黄金对拍纪律好；registry 保留旧 `compute_factor` 兼容；候选 10 因子不依赖缺失列，且自带“缺列则跳过”守卫。 |
| **C7** Finance Skills | 纯 Markdown + loader，含 path-escape 守卫（`test_reject_path_escape`）与总预算截断，低风险。 |
| **C9** 定时研究 | 对 C2（`app/api/research.py` + `data/research/`）依赖正确；executor 失败不冒泡到 scheduler，不阻断盘后 pipeline。 |
| **C11** 策略导出 | 限定无状态日线 DSL 子集；未知字段/有状态策略返回 unsupported，自带守卫；router 路径用“或现有策略 router”兜底（实际为 `app/api/strategy.py`）。 |
| **C13** 形态识别 | 自洽可测，固定阈值常量，纯 OHLCV 后处理，无新依赖。 |
| **P4** 数据质量 | 离线 fixture / 真盘 smoke 分层合理；任务 3 用磁盘层 `get_wide(asset_type="hk")`（正确）；引用的 `docs/data-query-inventory-local-source.md` 已存在。 |

---

## 附：跨计划依赖顺序（供排期核对）

- **C3 任务 0（eastmoney 白名单 + 翻页）** → 先于 **C12**。
- **C2（research registry / run_card）** → 先于 **C5 任务 3（run_card 并入）**、**C9**。
- **A2（`app.capabilities` 中性模块）** → 先于 **A6**。
- **A1-A5 全绿 + 产品决策** → 先于 **A6**（A6 前置门已在其计划中写明）。
