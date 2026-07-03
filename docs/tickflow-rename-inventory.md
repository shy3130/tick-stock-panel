# TickFlow 命名残留清单（2026-07-03）

- **背景**：TickFlow **数据源/SDK 层已彻底移除**（`app/tickflow/` 目录、`TickFlowProvider`、`tickflow[all]` 依赖均已删）。本文件盘点剩余的 `TickFlow` 字符串命中，**分类**给出处理建议。
- **核心告诫**：**不要对全仓做 `sed 's/TickFlow/.../g'`**。下方 C 类命中改了会破坏老用户数据迁移或指向外部基础设施，必须保留。
- **状态图例**：`[ ]` 待处理 / `[x]` 已处理 / `—` 不处理（留档说明原因）。

---

## A 类 — 死代码 / 过时语义（建议现在就清，与是否改名无关）

这些不是"品牌名"，而是**对已删除的 TickFlow 数据源的过时引用**，属正确性/可读性清理，成本低、独立于改名决策。

### A1 — 前端 `isTickflow` 死分支

- [x] 已清理：删除 `isTickflow` 死分支，数据源卡片固定走中性文案；保留 logo 品牌文字。

**位置**：`frontend/src/components/Layout.tsx:146`（`const isTickflow = provider === 'tickflow'`）、`:203`、`:392`（据此渲染 "TickFlow" 文案）。

**问题**：provider 现在**永远不可能是 `'tickflow'`**（registry 已无该 provider，`_clean_data_provider` 把任何残留值兜成 `fquant_local`）。该分支恒为 false，是死代码，且 `:203/:392` 的 "TickFlow" 文案永不触发。

**建议**：确认后删除 `isTickflow` 分支，状态卡统一走"数据源"展示（与 A4/A5 已做的中性化一致）。跑 `pnpm tsc --noEmit` 兜底。

### A2 — 过时注释：把上游误称 TickFlow

- [x] 已清理：三处注释改为中性"上游/数据源 capability"表述。

**位置**：
- `backend/app/api/intraday.py:101`：`"""返回实时指数行情缓存，不触发 TickFlow 请求。"""`
- `frontend/src/components/StockIntradayChart.tsx:45`：`// source=none 表示本地无数据且 TickFlow 也拉不到 ...`
- `frontend/src/components/Layout.tsx:358`：`// ... 后端同时处理 TickFlow 档位和本地数据源能力。`

**问题**：TickFlow 上游已不存在，注释语义过时，会误导后来者以为还有 TickFlow 拉取路径。

**建议**：改为中性表述（"不触发上游实时请求" / "本地无数据且上游也拉不到" / "后端处理数据源 capability"）。纯注释，零风险。

---

## B 类 — 产品品牌名（改名决策项，需先定目标名）

`TickFlow Stock Panel` / `TickFlow 股票面板` 是**产品身份**。是否改名是一次性 cosmetic 决策，**需先确定目标名**（如 `FQuant Panel` / 自定义），再统一替换。以下按命中面分组，改名时逐组处理；**不改名则全部保留**。

> 前置：**先定目标名** → 再逐组替换 → `pnpm tsc` + 后端冒烟 + 手工过 UI（登录/引导/侧栏/品牌页）。

### B1 — 后端展示名与系统提示

- [ ] 待处理（依赖目标名）

- `backend/app/__init__.py:1`：模块 docstring `"""TickFlow Stock Panel backend."""`
- `backend/app/main.py:33`：启动日志 `"TickFlow Stock Panel v%s starting ..."`
- `backend/app/main.py:190`：FastAPI `title="TickFlow Stock Panel"`
- `backend/app/api/agent.py:30`：system prompt `"You are TickFlow Stock Panel assistant. "`
- `backend/app/services/ai_provider.py:255`：system prompt `"You are TickFlow Stock Panel's local AI provider."`
- `backend/app/desktop.py:27`：`_APP_NAME = "TickFlow 股票面板"`（桌面窗口标题）

### B2 — 用户可见的通知标题（改名时优先，推送里会露出品牌）

- [ ] 待处理（依赖目标名）

- `backend/app/jobs/daily_pipeline.py:785` / `:788`：`"TickFlow · 每日复盘"`（飞书/webhook 推送标题）
- `backend/app/services/quote_service.py:790` / `:831`：`title = f"TickFlow · {source_label}"`
- `backend/app/services/webhook_adapter.py:199`：`quote(title or "TickFlow", safe="")`（默认标题兜底）

### B3 — 前端展示名

- [ ] 待处理（依赖目标名）

- `frontend/index.html:8`：`<title>TickFlow Stock Panel · Quant Terminal</title>`
- `frontend/src/pages/Auth.tsx:92`、`frontend/src/pages/Onboarding.tsx:104`/`:175`、`frontend/src/components/Logo.tsx:25`（aria-label）
- `frontend/src/pages/Branding.tsx:28/33/43/53/63/88`：**这是探索品牌视觉风格的页面**，硬编码 4 份 "TickFlow Stock Panel"；改名时以此页为中心，或按新名重做。

### B4 — 包名 / 打包标识（改动牵连构建，谨慎）

- [ ] 待处理（依赖目标名 + 需回归打包）

- `frontend/package.json:2`：`"name": "tickflow-stock-panel-frontend"`
- `backend/pyproject.toml:2`：`name = "tickflow-stock-panel-backend"`
- `packaging/tickflow.spec`、`packaging/tickflow.iss`：PyInstaller / Inno Setup 脚本文件名 + 内部 AppName。改这里需重跑打包验证；`backend/app/config.py:35` 注释引用了 `packaging/tickflow.iss` 路径，改名一并更。

---

## C 类 — 禁止改动（改了会坏事 / 属外部基础设施）

### C1 — 老桌面版数据迁移路径 `TickFlowStockPanel_Data`

- — 不处理

**位置**：`backend/app/storage/repository.py:78`、`:80`、`:97`（`legacy_dir = self.data_dir.parent / "TickFlowStockPanel_Data"`）。

**原因**：这是**老版本安装实际使用的磁盘目录名**，用于把旧数据迁移到新位置。字符串必须与历史目录名**逐字节一致**，改了老用户升级时数据迁移直接失效。**即使产品改名也保留此字面量**（可加注释说明这是历史目录名、勿改）。

### C2 — GitHub 仓库 URL

- — 不处理（除非同步改仓库名）

**位置**：`frontend/src/pages/Auth.tsx:176`、`System.tsx:255`、`Financials.tsx:82`（`github.com/shy3130/tickflow-stock-panel/...`）。

**原因**：指向真实远端仓库。只有在 GitHub 上真的重命名仓库后才同步改；否则改了变成死链。属独立的仓库迁移决策。

### C3 — 内部临时目录前缀

- — 不处理（可选）

**位置**：`backend/app/services/ai_provider.py:194`：`TemporaryDirectory(prefix="tickflow-codex-run-")`。

**原因**：进程内临时目录前缀，用户不可见、无功能影响。改名时顺手改亦可，非必要。

---

## 建议执行顺序

1. **先做 A 类**（A1 死分支 + A2 过时注释）——低风险、独立于改名、消除误导。可立即安排。
2. **B 类等一次性改名决策**：先由产品定目标名，再按 B1→B4 分组替换，最后回归（tsc + 打包 + UI 手测）。B4 牵连打包，单独验证。
3. **C 类永久保留**，其中 C1 建议加一行注释标注"历史目录名，勿改"，防止将来有人误 sed。

> 待办入口：本清单不阻断任何功能，属清理/品牌事项，可在主线计划全部合入后单开一个 `chore/rename` 分支处理。
