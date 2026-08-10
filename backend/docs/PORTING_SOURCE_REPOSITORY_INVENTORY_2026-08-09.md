# 跨仓库移植来源证据清单

> 日期：2026-08-09
> 状态：**Phase 1、Phase 2 均已完成。** 这是来源发现与逐仓审计账本，不是“已把所有上游功能全量移植”的声明；本轮明确排除的候选仍须按产品路线另行立项。
> 审计对象：`/Users/wf2311/Projects/wf2311/fm/tickflow-stock-panel` 当前工作树（`feature/fstore-engine-duckdb-source`，审计基线 `ce0d79f`）及其可复核的 Git、文档、源码、配置、脚本和同级工作区证据。
> 本文第 2～8 节保留发现阶段的历史证据；各来源的最终处置、已实施的最小修复与验证见第 9 节，若有表述冲突以第 9 节为准。

## 1. 范围、方法与证据等级

### 1.1 已执行的发现步骤

1. **Git 拓扑**：读取 `git remote -v`、本地/远程分支、worktree、submodule 与可达 `git log --all`；重点检索 `port`、`upstream`、`migrate`、`PA_Agent`、`YMOS`、`fquant`、`Vibe`、`daily` 等提交。
2. **当前树静态引用**：扫描根 `README.md`、`CONTEXT.md`、`docs/`、`backend/docs/`、脚本、配置、后端/前端源码和测试中的仓库名、相对路径、Git URL 与移植标记。
3. **相邻工作区**：枚举父目录 `/Users/wf2311/Projects/wf2311/fm/`；只有被步骤 1 或 2 命名的目录才纳入候选。对每个候选核对目录存在性、可读性和 Git remote（如有）。
4. **历史工作树**：核对两条 `port/upstream-f8fca96-duckdb*` ref。部分较早的评估文档只存在于 `658f40e` 历史 ref，不在当前 `HEAD`；本清单明确标记该事实，避免将历史文件误作当前依据。

### 1.2 证据等级

| 等级 | 含义 |
|---|---|
| **A — 直接代码/运行时** | 当前代码直接标记移植来源，或在运行时调用该仓库的 CLI / 读取其生产的数据契约。 |
| **B — Git / 设计账本** | 当前或可达历史提交、计划文档明确命名来源与引入范围；必须在 Phase 2 用源码复核，不能仅信文档。 |
| **C — 历史/反面参考** | 只在设计、注释或历史评估中出现；没有当前代码移植或运行时调用证据。 |
| **X — 非代码来源** | 仅 remote 镜像、SaaS 或无引用的相邻目录；记录以避免重复发现，但不进入代码对照。 |

“可访问”仅表示本轮可读取本地工作区；它不意味着上游逻辑、版本或许可已被接受。除注明外，角色均为已观察事实；推断会显式标为 **[推断]**。

## 2. Git 与历史工作树事实

| 对象 | 证据 | 结论 |
|---|---|---|
| 当前仓库 `origin` | `git remote -v`：`https://github.com/shy3130/tickflow-stock-panel.git` | 当前仓库的 fork/upstream remote；它是“上游 30 提交批次”等自仓历史的审计对象，不自动等同于外部功能来源。 |
| `workbench` remote | `git remote -v`：`https://github.com/wf2311/fm-workbench.git`；`workbench/port/*` ref 与本地 port ref 指向相同提交 | 镜像/推送目标，**不是**独立移植来源；无需源码对照。 |
| 历史 port ref | `port/upstream-f8fca96-duckdb` → `4275e0b`（worktree 已 prunable）；`port/upstream-f8fca96-duckdb-resume` → `658f40e` | 可复核历史移植提交。`658f40e` 提交正文明确列出 daily_stock_analysis、Vibe-Trading、外部 fallback 及批次 A–C；需要在 Phase 2 逐项核实。 |
| 历史评估文档 | `git show 658f40e:backend/docs/{UPSTREAM_FEATURE_PORTING,DAILY_STOCK_ANALYSIS_PORTING_ASSESSMENT,VIBE_TRADING_PORTING_ASSESSMENT}.md` 可读；当前 `HEAD` 中这三份文件不存在 | 它们只能作为 **B 级历史证据**，不能替代当前源码对照。 |
| Git submodule | `git submodule status` 无输出 | 未发现隐藏的 Git submodule 来源。 |

## 3. 需进入 Phase 2 的功能/机制来源

| ID | 候选仓库（绝对路径） | 发现证据 | 已观察角色 | 本轮可访问性 | Phase 2 处置与优先级 |
|---|---|---|---|---|---|
| S1 | `origin/main`（remote：`https://github.com/shy3130/tickflow-stock-panel.git`；无独立本地路径） | `fc63588`：`feat: port upstream features to DuckDB architecture`；`658f40e` 正文列出“上游 30 提交批次 A–C”；当前历史 ref 可读 | 同仓上游提交批次，覆盖 regime、指数监控、回测/UI、安全修复等；具体哪些部分被采纳尚未验证 | Git object / remote 可读 | **必审，高**：以 `5a9fa9b..f8fca96` 及批次 A/B/C 的实际 diff 对当前相应模块做 lineage 对照；不把提交描述当完成证据。 |
| S2 | `/Users/wf2311/Projects/wf2311/fm/PA_Agent` | `53dd632` 明确“移植 PA_Agent 结构化输出运行时”；`backend/docs/PA_AGENT_PORTING_PLAN.md:5`；`backend/app/services/ai_structured/models.py:3` 明确 P0/P1 契约 | AI 结构化结果、校验/重试、不可变字段、K 线分析上下文、两阶段计划检查的机制来源；PyQt、自动交易与二元荐股语义应排除 | 存在、可读；origin=`https://github.com/rosemarycox5334-debug/PA_Agent.git` | **必审，高**：逐模块对照 `ai/`、`data/`、`orchestrator/`、`records/`、`notify/` 与 `ai_structured/`、`analysis_context.py`、`trading/plan_check.py`。 |
| S3 | `/Users/wf2311/Projects/wf2311/fm/YMOS` | `53ea5ea` 明确“移植 YMOS 交易纪律域”；`backend/docs/YMOS_PORTING_PLAN.md:1-3`；根 `README.md:258` 指向该计划 | 生命周期、纪律门禁、审计事实流、红旗、复盘、策略治理的机制来源 | 存在、可读；origin=`https://github.com/Evan-XYZ/YMOS` | **必审，最高交易风险**：对照 YMOS 的规则原文与 `services/trading/{gates,lifecycle,red_flags,plans,proposals}.py`；验证不会产生执行/荐股入口。 |
| S4 | `/Users/wf2311/Projects/wf2311/fm/ymos-diagnosis` | `YMOS_PORTING_PLAN.md:3` 明确列为 YMOS 联合来源；计划文末引用其 `skills/ymos-diagnosis/references/` | 策略结构诊断、坐标卡与 playbook 的设计依据 | 存在、可读；origin=`https://github.com/Evan-XYZ/ymos-diagnosis.git` | **必审，中**：随 S3 对照策略 profile / validator 的诊断条目；不单独引入第二套领域语言。 |
| S5 | `/Users/wf2311/Projects/wf2311/fm/fquant` | `fecf830` 明确将 `../fquant` 复盘展示分区移入；`frontend/src/lib/threeLocks.ts:1` 标注移植自 `../fquant/web/src/components/threeLocks.ts`；`dev.sh:24` 使用 `../fquant/.env` | 复盘语义、threeLocks 前端算法、provider/数据口径参考；也与本地 DuckDB 数据链共用环境 | 存在、可读；origin=`git@github.com:wf2311/fquant.git` | **必审，高**：逐行对 threeLocks；对 review 分区和 provider 契约做行为对照；单列审计 `dev.sh` 共享 `.env` 的密钥边界。 |
| S6 | `/Users/wf2311/Projects/wf2311/fm/go-stock` | `backend/app/strategy/gostock_presets.py` 文件注释标明移植自 `choice_stock_by_indicators_tool.go` | 选股策略语句库的局部来源；不是其东财在线 API 的接入 | 存在、可读；origin=`https://github.com/ArvinLovegood/go-stock.git` | **必审，中**：只对照预设语句、字段含义与测试；不得引入其在线行情 API。 |
| S7 | `/Users/wf2311/Projects/wf2311/fm/Vibe-Trading` | `658f40e` 列出 Vibe P0 Trade Journal 导入健壮性；当前 `docs/vibe-trading-migration-candidates.md`、`docs/vibe-agent-page-gap-assessment.md` 明确绝对源路径 | Trade Journal 解析健壮性与 Agent/mandate 安全模式的候选来源；LangGraph、券商连接器和 Shadow Account 必须排除 | 存在、可读；origin=`https://github.com/HKUDS/Vibe-Trading.git` | **必审，中**：对照导入解析器、Agent 安全模式；逐项确认当前未迁移候选是否具有用户入口和安全契约。 |
| S8 | `/Users/wf2311/Projects/wf2311/fm/daily_stock_analysis` | `658f40e` 明确 daily P0/P1/P2 来源；`CONTROLLED_EXTERNAL_FALLBACK_DESIGN.md:5,119-120` 保留对历史评估和 commit 的引用 | 选股诊断、超时、webhook 安全与受控 fallback 的模式来源；其多公网 fetcher 已在设计上排除 | 存在、可读；origin=`https://github.com/ZhuLinsen/daily_stock_analysis.git` | **必审，中**：把已称移植的 P0/P1 机制与源代码比对；明确保留其 fetcher 排除结论，禁止扩展为主数据源。 |

## 4. 运行时数据、格式或 CLI 契约来源

这些候选可能没有可复制的业务功能，但其输出直接影响 tickflow 正确性；Phase 2 应做**契约审计**而非复制实现。

| ID | 候选仓库（绝对路径） | 发现证据 | 已观察角色 | 本轮可访问性 | Phase 2 处置 |
|---|---|---|---|---|---|
| D1 | `/Users/wf2311/Projects/wf2311/fm/fstore` | `backend/app/data_providers/fquant/fstore_duckdb_client.py:1-4,27-34` 写明 FStoreClient 的只读替代与默认 DuckDB ATTACH；`FQUANT_INTEGRATION_PROGRESS.md` 数据矩阵 | fstore DuckDB 表/快照的生产方和 schema 口径来源；tickflow 只读消费 | 存在、可读；origin=`git@github.com:wf2311/fstore.git` | **必审，高**：对照消费表、字段、复权/时区/符号口径与 generation 约束；不迁移数据库服务。 |
| D2 | `/Users/wf2311/Projects/wf2311/fm/engine` | `FQUANT_INTEGRATION_PROGRESS.md`、`FQUANT_PROVIDER_DESIGN.md`、`FQUANT_SNAPSHOT_ROOT_ENGINE_A` 配置；`catalog_resolver.py` 的 engine-a staged 路由 | TDX/minutes/trans/moneyflow DuckDB 快照生产方 | 存在、可读；origin=`git@gitee.com:wf2311/engine.git`，另有 upstream `https://github.com/quant1x/engine.git` | **必审，高**：审计发布物与 catalog/generation pinning 的格式契约；不将 engine 服务接入业务层。 |
| D3 | `/Users/wf2311/Projects/wf2311/fm/duckdbsnap` | `snapshot_resolver.py` 文档字符串明确“on-disk layout written by duckdbsnap（参见 engine/duckdbsnap/manifest.go）”；目录含 `generation.go`、`publish.go`、`manifest.go` | DuckDB immutable generation、`current.json`、manifest 与 pinning 格式来源 | 存在、可读；独立目录无 `.git` remote | **必审，高**：审计格式兼容性与 fail-closed 行为；不可把它误记为普通 Python 代码移植。 |
| D4 | `/Users/wf2311/Projects/wf2311/fm/fhold` | `backend/app/services/trading/fhold_client.py:1,25,34`；项目身份卡 §Trading；对应 mock 契约测试 `backend/tests/services/trading/test_fhold_client.py` | `fhold-cli --format json` 的只读持仓/账户集成；不是行情 provider，也不是数据库直连 | 存在、可读；origin=`git@github.com:wf2311/fhold.git` | **必审，高交易边界**：核对 CLI JSON schema、fail-soft、超时与只读保证；禁止绕过 CLI 读取 `~/.fhold/fhold.db`。 |
| D5 | `/Users/wf2311/Projects/wf2311/fm/tdx-api` | `FQUANT_INTEGRATION_PROGRESS.md:172,182` 记录历史阶段；项目身份卡和 `README.md:438` 明确“**不再调用 tdx-api**” | 历史 TDX 行情 HTTP 候选，当前本地 DuckDB 链的已弃用旁路 | 存在、可读；origin=`https://github.com/oficcejo/tdx-api.git` | **处置审计，非对照**：确认没有 HTTP/subprocess 残留调用；不得恢复为 fallback。 |

## 5. 参考、排除和不可访问候选

| ID | 候选（绝对路径 / URL） | 发现证据 | 角色与处置 | 可访问性 |
|---|---|---|---|---|
| X1 | `/Users/wf2311/Projects/wf2311/fm/wz-api`（相关 `waizao` / `wz` 工具链） | `ADR-0003`、`CONTEXT.md` 说明它只在 fquant Manager 的最后兜底出现；项目数据源红线禁止业务层直连 | **C，明确排除**：仅保留为“不得恢复外部主行情链”的反例；不进行功能迁移。 | 存在、可读 |
| X2 | `/Users/wf2311/Projects/wf2311/fm/fm-cli` | `tdx_duckdb_client.py:379` 的设计注释、`test_mapping.py:9` 的知识溯源；无运行时 import/子进程调用 | **C，知识参考**：不属于代码移植或 runtime 依赖；只核对注释不构成隐藏耦合。 | 存在、可读；origin=`git@github.com:wf2311/fm-cli.git` |
| X3 | `https://github.com/wf2311/fm-workbench.git` | `workbench` Git remote 与镜像 refs；在 `/Users/wf2311/Projects/wf2311/fm/` 未发现本地克隆 | **X，镜像 remote**：无移植提交命中，排除源码对照。 | 远程 ref 可读；本地目录不存在 |
| X4 | TickFlow SaaS / SDK（无本地仓库路径） | 项目身份卡的“明确不重新引入 TickFlow SDK”红线及历史 Git 提交 | **X，产品/SDK 而非可审计本地源**：永久排除；不因历史依赖重新接入。 | 无本地仓库 |
| X5 | `/Users/wf2311/Projects/wf2311/fm/gotdx`、`/Users/wf2311/Projects/wf2311/fm/eltdx` 及其他同级金融目录 | 相邻目录枚举后，对 tickflow 的 README/docs/scripts/config/code 搜索未发现来源或运行时引用 | **X，邻近但无证据**：不列为移植源。未来出现直接引用时再重新入册。 | 目录可读 |

## 6. Phase 2 执行规则

1. **一行一处置**：S1–S8 和 D1–D5 必须分别记录“已对照/已有覆盖/安全移植/明确不迁移/无法验证”之一；不得由另一份计划文档替代源码证据。
2. **数据底座与功能机制分开**：D1–D3 是 schema/快照契约审计，D4 是 CLI 边界审计；它们不是将上游服务并入 tickflow 的授权。
3. **外部网络与交易安全不降级**：不恢复 `tdx-api` / waizao / TickFlow SDK；不让外部 fallback 写 canonical/enriched，也不让 AI 生成订单或写交易事实。
4. **变更门**：只有源码对照发现有明确、可测试、用户可见且符合上述边界的缺口，才允许创建最小实现和回归测试；默认结论是“不改动”。
5. **工作树保护**：本次现有未提交安全修复属于独立变更；后续审计/实现不得 checkout、reset、覆盖或回退它。

## 7. Phase 1 未知项（保留历史）

- `origin/main` 的“上游 30 提交批次”已在 Phase 2 按 commit / 文件对照，最终范围与处置见 §9 S1。
- PA_Agent 的 remote 可能是镜像/fork；本轮只以本地可读源码完成机制对照，不追溯更早原始作者。
- `engine` 的 `quant1x/engine` upstream 属更深层生产者血缘；D2 未发现 snapshot/schema 缺口，故不扩展到第三层源码审计。
- Vibe-Trading 与 daily_stock_analysis 的历史评估文档虽不在当前 `HEAD`，但 Phase 2 已直接审阅本地源仓，结论见 §9 S7 / S8。

## 8. Phase 1 验收记录

- 候选均有绝对路径（或明确说明“无本地路径”）、可读状态、发现证据、观察角色和 Phase 2 处置方向。
- 普通 PyPI/npm 依赖（FastAPI、Polars、React、vectorbt 等）未被误列为仓库来源。
- 相邻但未被 tickflow 任何 Git/文档/脚本/配置/代码引用的目录被显式排除。
- 本节只记录 Phase 1 的发现边界；S/D 项是否源码对照、已覆盖或明确排除，以 §9 的逐项结论为准。


## 9. Phase 2 最终处置与验证（2026-08-09）

审计只采纳有明确用户路径、可测试契约且不触碰本地 DuckDB 主链路或交易红线的最小修复。其余候选没有因为“上游已有代码”而自动进入当前工作树。

| ID | 最终处置 | 源码对照结论与本轮动作 |
|---|---|---|
| S1 | **部分覆盖；安全候选已修复** | 当前 HEAD 与历史 port 批次在共同祖先后分叉；regime、完整指数监控路由、composite/OCR/vectorbt 锁等未迁移，明确不作为本轮隐式范围。三处扩展列动态 SQL 已由 `db_safe.quote_ident` 与配置 ID 白名单保护；补入 port 先例的 sealed 写入防御，拒绝 `tencent_quote` 等非 `fquant*` provenance 写入实时 daily/enriched 入口。 |
| S2 | **covered** | PA_Agent 的 P0-P4 与 P5 M15-M19 已按 `PA_AGENT_PORTING_PLAN.md` 的边界落地：结构化 AI、K 线 context、显式 profile fallback、只读两阶段计划检查、artifact/通知；Qt GUI、自动交易与公网多源链保持排除。 |
| S3 / S4 | **covered** | YMOS 五条结构红线已由服务端强制；事实流 append-only，复盘/策略治理均无自动下单路径。ymos-diagnosis 的坐标卡、失效信号和策略体检已以同一领域词汇重写。 |
| S5 | **covered；边界清理已完成** | `threeLocks` 与上游逐项同构并有对拍测试；review 为读取本地 enriched 数据的语义重写，未引入 PG/HTTP。已删除 `dev.sh` 对 `../fquant/.env` 的 `FSTORE_DATABASE_PASSWORD` 读取，杜绝跨仓密钥耦合。 |
| S6 | **covered** | go-stock 仅贡献本地可执行的选股预设语义；其东财在线 smart-tag API 未被引入。 |
| S7 / S8 | **明确排除或既有能力覆盖** | Vibe-Trading 的券商、LangGraph/Shadow Account 与 daily_stock_analysis 的多公网 fetcher 不进入 tickflow。已验证的 Trade Journal 解析、日志脱敏、webhook 清洗和超时/重试机制保留在本项目实现中。 |
| D1 | **安全候选已修复** | `fstore-extended.duckdb` 的 `extended` logical 已改为独立 `snapshots/fstore-extended` generation；不再静默回退至通用 fstore snapshot。 |
| D2 / D3 | **covered；安全候选已修复** | engine catalog/pinning 与 duckdbsnap manifest/current.json 契约已对齐。`tdx-moneyflow-minute.duckdb` 已改用独立 `snapshots/engine-a-moneyflow-minute` generation；日期分片 minutes/trans 仍经 catalog fail-closed，不允许 raw fallback。 |
| D4 | **covered** | `fhold-cli --format json` 只读账户/持仓、超时与 fail-soft 契约完备；没有直读 `~/.fhold/fhold.db` 或券商写入路径。 |
| D5 / X1–X5 | **明确排除** | tdx-api、waizao、TickFlow SDK、fm-cli、workbench mirror 及仅相邻无引用目录均无 runtime 迁移路径；不得恢复为外部行情主链或 fallback。 |

### 9.1 本轮验证记录

- `cd backend && uv run pytest -q tests/data_providers/test_snapshot_resolver.py tests/data_providers/test_generation.py tests/data_providers/test_fstore_duckdb_client.py tests/storage/test_repository_sealed_write.py tests/test_ext_sql_safety.py tests/external_fallback/test_external_fallback.py`：**86 passed**。
- `cd backend && uv run python -c "import app.main; print('app.main import ok')"`：成功；`bash -n dev.sh`：成功。
- 上述测试覆盖独立 generation root、扩展 SQL 标识符转义、外部 fallback 默认关闭/口径/熔断，以及 sealed 写入拒绝；未访问真实外部行情、未写入 `data/`。