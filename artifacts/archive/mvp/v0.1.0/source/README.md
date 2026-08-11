# TickFlow A 股量化研究与回测后端

一个面向 A 股因子研究、策略验证、数据处理和真实回测的 Python 项目。项目当前采用纯后端结构，不包含 React/Vite 等 Web 前端；API、研究脚本和离线 HTML 报告构成主要使用入口。

> 本项目仅供学习与量化研究，不构成任何投资建议。回测结果不代表未来收益。

## 当前状态

项目已完成因子 DSL、语义因子、多重检验、多区间 walk-forward、真实引擎 OOS、
regime 条件化归因，以及 AlphaGPT Research v1.0 完整研究基线。AlphaGPT v1.0
不是生产 alpha：其生成、评估和审计闭环完整，但泛化 gate 不支持实盘或 PPO。

接手或继续研究时按以下顺序阅读：

1. [HANDOFF.md](./HANDOFF.md)：项目现状、诚实结论、关键坑和推荐路线。
2. [AGENTS.md](./AGENTS.md)：自动化代理和研究任务的执行约束。
3. [BackendArchitecture.md](./BackendArchitecture.md)：目录、模块边界和新增文件规则。
4. [artifacts/current/regime_ensemble_report.html](./artifacts/current/regime_ensemble_report.html)：当前权威综合报告。
5. [AlphaGPT Research v1.0 发布说明](./artifacts/archive/factors/alphagpt_research_v1_release.md)：完整研究版本边界和产物哈希。

当前机器可读事实源：

- `artifacts/current/strategy_regime_ensemble.json`
- `artifacts/current/diag_f4_regime.json`

## 目录结构

```text
.
├─ backend/
│  ├─ app/                 # FastAPI 生产后端
│  ├─ research/            # 因子、regime、优化、验证和报告脚本
│  ├─ scripts/             # 后端运维脚本
│  ├─ tests/               # 自动化测试
│  ├─ pyproject.toml
│  └─ uv.lock
├─ artifacts/
│  ├─ current/             # 当前权威产物
│  ├─ archive/             # 按主题归档的历史结果
│  └─ logs/                # 本地运行日志，不提交
├─ data/                   # Parquet 数据与缓存，不提交
├─ docs/                   # 使用和部署文档
├─ scripts/                # 仓库级数据接入工具
├─ packaging/              # 安装包构建
├─ AGENTS.md
├─ BackendArchitecture.md
└─ HANDOFF.md
```

完整边界与放置规则见 [BackendArchitecture.md](./BackendArchitecture.md)。

## 环境

- Python ≥ 3.11
- 推荐使用项目自带的 `backend/.venv`
- 数据处理：Polars、DuckDB、Parquet
- API：FastAPI、Pydantic
- 回测：项目 Matrix 引擎；vectorbt 为可选依赖

Windows 下不要使用全局 Python。研究回测必须启用 in-process 模式。

## 最小 MVP：一条命令完成回测

第一版 MVP 不包含前端，只开放固定的 `trend_breakout`（趋势突破）策略。它会先检查
canonical enriched 日线，再用固定 seed 抽取股票池、调用现有 Matrix 引擎回测，并输出
可审计 JSON 和离线 HTML。该策略目前仍是历史回放未晋级状态，运行结果不能解释为实盘
有效性证明。

```powershell
Set-Location backend
$env:TICKFLOW_BACKTEST_MODE = "inprocess"
.\.venv\Scripts\python.exe -m scripts.run_mvp `
  --strategy trend_breakout `
  --start 2024-09-24 `
  --end latest
```

只检查数据、不运行回测：

```powershell
.\.venv\Scripts\python.exe -m scripts.run_mvp --validate-only
```

固定产物：

- `artifacts/current/mvp_backtest.json`
- `artifacts/current/mvp_backtest.html`

相同数据、区间、策略、股票池大小和 seed 会产生相同的协议哈希与核心结果；运行耗时、
进程 ID 和缓存命中信息不会写入 MVP 事实源。

冻结或验证 MVP v0.1.0：

```powershell
.\.venv\Scripts\python.exe -m scripts.freeze_mvp
.\.venv\Scripts\python.exe -m scripts.freeze_mvp --verify-only
```

冻结归档位于 `artifacts/archive/mvp/v0.1.0/`。同版本允许相同内容重复验证，但拒绝覆盖
任何已经冻结且哈希不同的文件。

## 启动后端 API

PowerShell：

```powershell
Copy-Item .env.example .env
.\dev.ps1
```

Linux/macOS：

```bash
cp .env.example .env
./dev.sh
```

默认只监听本机 `127.0.0.1`。API 地址为 `http://localhost:3018`，OpenAPI 文档为
`http://localhost:3018/docs`。需要局域网或公网访问时，必须先配置访问密码，再显式改为
`0.0.0.0`。

也可使用 Docker：

```bash
cp .env.example .env
docker compose up --build
```

Docker 中的 `stock-sdk` 插件为可选能力，Node.js 依赖它而存在，与前端无关。

## 导入本地数据

行情数据统一写入项目根目录 `data/`。示例：

```powershell
backend\.venv\Scripts\python.exe scripts\ingest.py --sample --symbols 20 --days 500
backend\.venv\Scripts\python.exe scripts\ingest.py --parquet .\my_data.parquet
```

因子和 regime 研究读取：

```text
data/kline_daily_enriched/**/*.parquet
```

## 运行当前权威研究

从 `backend/` 目录使用模块方式运行：

```powershell
Set-Location backend
$env:TICKFLOW_BACKTEST_MODE = "inprocess"

.\.venv\Scripts\python.exe -m research.regime.run_regime_ensemble
.\.venv\Scripts\python.exe -m research.regime.diag_f4_regime
.\.venv\Scripts\python.exe -m research.reporting.make_regime_ensemble_report
```

输出会写入 `artifacts/current/`，不会污染仓库根目录。其他实验入口见 [backend/research/README.md](./backend/research/README.md)。

验证 AlphaGPT Research v1.0：

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m research.alphagpt.run_release_v1 --verify-only
```

## 测试

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest
```

只检查研究模块是否可导入：

```powershell
.\.venv\Scripts\python.exe -m compileall -q research
```

## 文档

- [配置说明](./docs/configuration.md)
- [部署说明](./docs/deployment.md)
- [策略体系](./docs/strategy.md)
- [功能说明](./docs/features.md)
- [自定义数据源](./docs/custom-data-source.md)
- [插件开发](./docs/plugin-development.md)

已下线 UI 的历史截图仅保存在 `docs/archive/ui-screenshots/`，不属于当前产品结构。

## License

[MIT](./LICENSE)

本项目基于 TickFlow 数据能力进行适配，使用前请遵守相应服务条款。数据源插件 `stock-sdk` 遵循其自身许可证。
