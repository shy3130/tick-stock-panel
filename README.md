
<div align="center">

# 📈 A股智能量化工作台

[![声明:个人开源](https://img.shields.io/badge/⚠️_声明-个人开源_非_TickFlow_官方项目-green?style=for-the-badge&labelColor=red)](https://github.com/shy3130/tickflow-stock-panel)



**自托管、零运维的 A 股「选股 + 监控 + 回测」量化工作台**

**面向个人散户与量化爱好者而生**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/Python-≥3.11-blue.svg)](https://www.python.org/)
[![React](https://img.shields.io/badge/React-18-61dafb.svg)](https://react.dev/)
[![Data: Tushare](https://img.shields.io/badge/Data-Tushare_Pro-00b386.svg)](https://tushare.pro/)
[![Deploy: Docker](https://img.shields.io/badge/Deploy-Docker-2496ed.svg)](./Dockerfile)
[![GitHub stars](https://img.shields.io/github/stars/shy3130/tickflow-stock-panel?style=social)](https://github.com/shy3130/tickflow-stock-panel/stargazers)

</div>

<div align="center">
  


**[快速开始](#-快速开始)** · **[核心功能](#-核心功能)** · **[配置](#️-配置)** · **[完整文档](#-完整文档)**

</div>


---



**本分支面向个人自用研究：Tushare Pro 为主数据源，AKShare 仅作显式备用，TickFlow 保留为可选源。任何拉取失败都不会静默换源或伪造成功。**




> ⚠️ 小白请绕路，本开源项目谨作为本地量化提供解决思路Demo，不作为投资软件或者看盘软件。
>
> **明确不做**:不对标同花顺 / 通达信，不做 AI 荐股、收益承诺、目标价或自动下单。

有问题可以邮件415333856@qq.com,交流群二维码在文末。

觉得有用可以点个 Star

---

## ✨ 核心功能

| 模块             | 一句话                                                                 | 详见                              |
| :--------------- | :--------------------------------------------------------------------- | :-------------------------------- |
| 🔍 **选股引擎**   | 18 个内置策略 + 自定义信号 + AI 生成 + 代码迁移,Polars 毫秒级扫全 A 股 | [strategy.md](./docs/strategy.md) |
| 🛡️ **量化顾问**   | 数据可信度门禁 + 多策略共识评分 + GO/WAIT/NO-GO 研究清单              | [a-share-advisor.md](./docs/a-share-advisor.md) |
| 📊 **指标流水线** | MA/EMA/MACD/RSI/KDJ/布林/量比等,一次扫表落盘 enriched Parquet          | [features.md](./docs/features.md) |
| 🧪 **回测引擎**   | 三种模式(个股/策略组合/自由信号),T+1/手续费/滑点/止损,SSE 流式进度     | [features.md](./docs/features.md) |
| 📡 **监控中心**   | 四类监控(策略/个股信号/价格/异动),多条件 AND/OR + 语音播报 + 飞书推送  | [features.md](./docs/features.md) |
| 📈 **个股分析**   | 9 类关键价位 + AI 四维分析(技术/基本面/财务/消息面)                    | [features.md](./docs/features.md) |
| 🏆 **连板梯队**   | 连板层级统计 + 概念涨幅轮动 + 盘后 AI 复盘 + 炸板/翘板预警             | [features.md](./docs/features.md) |
| 🧰 **数据扩展**   | TickFlow 多源 + 第三方接入(接口/推送/CSV/JSON)同台分析                   | [features.md](./docs/features.md) |





<details>
<summary><b>📦 主要页面与功能</b></summary>

**📊 行情总览**
- **看板** Dashboard — 市场情绪评分 + 涨跌/成交额榜单 + 概念领涨领跌 + 大盘异动事件流,一日全貌
- **自选** Watchlist — 自选股池,表格/卡片双视图,换手/量比/RSI 等实时指标
- **指数** Indices — 沪深指数浏览与同步

**🔍 选股与回测**
- **策略** Screener — Polars 毫秒级扫描全 A 股,18 个内置策略卡片 + 自定义条件
- **量化顾问** Advisor — 读取已落盘的策略结果和数据回执，确定性生成 GO / WAIT / NO-GO 研究清单
- **回测** Backtest — 两种模式:
  - **因子回测** — IC/IR、分层收益、多空组合,先筛掉无效指标
  - **策略回测** — 净值曲线、回撤、夏普、胜率,支持 T+1/手续费/滑点/止损,SSE 流式进度

**📈 个股与板块分析**
- **个股分析** Stock Analysis (Beta) — 日K + 9 类关键价位 + AI 四维分析(技术/基本面/财务/消息面)
- **财务分析** Financials — 利润表/资负表/现金流/关键指标 + AI 解读
- **概念分析** Concept Analysis — ths 概念涨幅轮动矩阵 + 领涨/领跌主线 + 个股穿透
- **行业分析** Industry Analysis — 行业分层涨幅轮动 + 领涨/领跌主线 + 成分股
- **连板梯队** Limit Up Ladder — 连板层级统计 + 概念/行业分布 + 封单监控(可切换连跌梯队)

**🔔 监控与复盘**
- **监控中心** Monitor — 策略/个股信号/价格/异动四类规则,盘中实时弹窗 + 语音播报(播报个股名称与信号) + 触发记录持久化
- **复盘** Review (Beta) — 盘后 AI 自动生成市场复盘,可定时执行、推送飞书、下载 Markdown

**🗄️ 数据与扩展**
- **数据** Data — 本地数据画像与同步状态(维表/日K/除权/Enriched/指数/ETF/分钟K/财务),盘后管道与历史扩展
- **扩展分析** (动态菜单) — 把任意第三方/扩展数据字段配成一级菜单,与内置数据同台分析
- **设置** Settings — Tushare/AKShare/TickFlow 数据源、可信度回执、AI 接口、实时监控、菜单与系统设置

</details>


---

## 📸 界面预览

<table>
  <tr>
    <td width="50%" align="center"><b>看板 Dashboard</b></td>
    <td width="50%" align="center"><b>策略 Screener</b></td>
  </tr>
  <tr>
    <td width="50%"><img src="./screenshots/dashboard.png" alt="看板页面"></td>
    <td width="50%"><img src="./screenshots/screener.png" alt="策略页"></td>
  </tr>
  <tr>
    <td width="50%" align="center"><b>回测 Backtest</b></td>
    <td width="50%" align="center"><b>监控中心 Monitor</b></td>
  </tr>
  <tr>
    <td width="50%"><img src="./screenshots/backtest.png" alt="回测页"></td>
    <td width="50%"><img src="./screenshots/monitor.png" alt="监控中心"></td>
  </tr>
  <tr>
    <td width="50%" align="center"><b>连板梯队 Limit Ladder</b></td>
    <td width="50%" align="center"><b>概念分析 Concept</b></td>
  </tr>
  <tr>
    <td width="50%"><img src="./screenshots/limit-ladder.png" alt="连板梯队页"></td>
    <td width="50%"><img src="./screenshots/concept-analysis.png" alt="概念分析"></td>
  </tr>
</table>

<div align="center">

### 📸 [查看更多界面截图 »](./screenshots/README.md)

</div>


---




## 🚀 快速开始

> 前置依赖:Python ≥ 3.11 · Node ≥ 20 · [`uv`](https://docs.astral.sh/uv/) · `pnpm`(`npm i -g pnpm`)

### 方式 A:Dev 模式(二次开发推荐)

```bash
cp .env.example .env       # 填 TUSHARE_TOKEN；Windows 可用 Copy-Item
./dev.sh                   # Windows: .\dev.ps1
```

自动检查 / 下载依赖、释放端口、同时起前后端。后端 → <http://localhost:3018> · 前端 → <http://localhost:3011>。

### 方式 B:Docker(部署最省心)

```bash
cp .env.example .env
docker compose up --build
# 打开 http://localhost:3018
```

默认 Compose 只将服务映射到 `127.0.0.1:3018`，不暴露到局域网或公网，也不会挂载主机 Codex 凭据。`market-data` 依赖组会安装 Tushare 与 AKShare；stock-sdk 默认不打包。

> 📖 Docker 进阶、GitHub Actions 自构建、老 CPU 兼容、访问密码设置等见 [docs/deployment.md](./docs/deployment.md)。

### 跑起来后的第一次使用

1. 在 `.env` 填入 `TUSHARE_TOKEN`，启动后到 **设置 → 数据源** 确认 Tushare 为“使用中”
2. 同步证券主表、日 K、复权因子和财务/股本数据，检查“数据可信度回执”
3. 到 **策略** 页运行至少两个策略；只有同一数据截止日的结果才进入共识
4. 到 **量化顾问** 查看 GO / WAIT / NO-GO；GO 只表示可进入人工研究清单
5. 回测时核对 T+1、手续费、滑点、涨跌停和样本期，不把历史结果当成收益承诺

---

## ⚙️ 配置

所有配置从根目录 `.env` 读取(复制 `.env.example` 开始),也可在面板 **设置** 页修改。最常用的三项:

```ini
TUSHARE_TOKEN=                 # 主数据源凭据，不要提交到 Git
TUSHARE_SHARE_HISTORY_YEARS=3  # 股本历史回溯年数
BACKEND_EXTRAS=market-data     # 安装 Tushare + AKShare
AI_API_KEY=                    # 留空 = 关闭 AI;填 Key 启用策略生成
PORT=3018                      # 服务端口
```

> 📖 完整配置项(数据源档位、AI、服务、密码、老 CPU 兼容)见 [docs/configuration.md](./docs/configuration.md)。

---

## 🏗️ 技术栈

| 层           | 选型                                                                                              |
| :----------- | :------------------------------------------------------------------------------------------------ |
| **后端**     | FastAPI · Pydantic v2 · APScheduler · sse-starlette                                               |
| **数据**     | Polars(计算)· DuckDB(查询)· Parquet(存储)                                                         |
| **回测**     | vectorbt(全项目唯一 pandas 边界)                                                                  |
| **数据源**   | Tushare Pro 主源 · AKShare 显式备用 · TickFlow 可选                                      |
| **AI**(可选) | OpenAI 兼容接口(DeepSeek / 通义 / Ollama 等)                                                      |
| **前端**     | React 18 · Vite · TypeScript · Tailwind · Tanstack Query · Lightweight Charts · ECharts · dnd-kit |
| **部署**     | Docker 两阶段构建,前端 dist 拷进后端镜像,**单容器**                                               |

---

## 🗺️ 路线图

| Phase  | 内容                                                               | 状态 |
| :----- | :----------------------------------------------------------------- | :--- |
| 0-1    | 仓库骨架 · FastAPI 壳 · 能力探测 · K 线同步与分析页                | ✅    |
| 2-3    | Polars enriched 流水线 · Screener · vectorbt 回测(T+1/手续费/止损) | ✅    |
| 4-5    | 监控引擎 · 四类监控规则 · 实时 SSE 推送 · 持久化记录               | ✅    |
| 6      | 个股分析(专用日 K + 9 类关键价位 + AI 四维分析)                    | ✅    |
| **v2** | Webhook 推送· 板块异动 · 早晚报 · 更多扩展           | 🚧    |

---

## 📚 完整文档

| 文档                                                                                               | 内容                                                                 |
| :------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------- |
| [docs/deployment.md](./docs/deployment.md)                                                         | 部署方式(Dev / Docker / GH Actions)、老 CPU 兼容、更新代码、访问密码 |
| [docs/private-tailscale-deployment.md](./docs/private-tailscale-deployment.md)                     | Tailscale 私网 HTTPS、手机/平板接入与安全边界                        |
| [docs/verified-backup-and-restore.md](./docs/verified-backup-and-restore.md)                       | 无凭据快照、自动轮换、manifest 校验与隔离恢复演练                    |
| [docs/configuration.md](./docs/configuration.md)                                                   | 所有 `.env` 配置项详解(数据源、AI、服务、密码、数据目录)             |
| [docs/a-share-advisor.md](./docs/a-share-advisor.md)                                               | 数据源边界、可信度回执、推荐门禁、首跑流程与已知限制                 |
| [docs/features.md](./docs/features.md)                                                             | 各功能模块详细说明(选股/指标/回测/监控/个股分析/数据扩展)            |
| [docs/custom-data-source.md](./docs/custom-data-source.md)                                         | 自定义数据源接入、YAML 配置与 mock 联调示例                         |
| [docs/strategy.md](./docs/strategy.md)                                                             | 策略体系(18 内置策略 + 三种扩展方式 + 文件结构)                      |
| [backend/app/strategy/prompts/strategy-guide.md](./backend/app/strategy/prompts/strategy-guide.md) | 策略开发完整规范(AI 生成与手写)                                      |

fork同时请点个star哦,欢迎 Issue 和 PR。

---

## 💬 交流群

欢迎加入交流群,讨论交流。

<img src="./community-qr-code.jpg" alt="交流群二维码" width="240" />

---

## ⚠️ 免责声明

本项目仅供**个人学习与量化研究**，**不构成任何投资建议**。GO 不是买入指令；回测结果不代表未来收益。A 股有风险，入市需谨慎。数据准确性应以所选数据源的实际回执和官方口径为准。

## 📄 License

[MIT](./LICENSE) © tickflow-stock-panel contributors 

使用 Tushare、AKShare 或 TickFlow 前，请分别遵守其许可证、服务条款和数据使用边界。

数据源插件 [stock-sdk](https://stock-sdk.linkdiary.cn) 遵循其各自的 ISC 协议。

## 社区

本开源项目已链接并认可 [LINUX DO 社区](https://linux.do)。
