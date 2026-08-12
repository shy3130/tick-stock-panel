# 配置详解

所有配置从根目录 `.env` 读取(复制 `.env.example` 开始),也可在面板 **设置** 页面可视化修改。本文件解释每个配置项的作用。

部署相关配置(端口/密码/老 CPU 兼容)的实操见 [deployment.md](./deployment.md)。

---

## 数据源：Tushare 主源

```ini
TUSHARE_TOKEN=
TUSHARE_REQUEST_INTERVAL_SECONDS=0.35
TUSHARE_SHARE_HISTORY_YEARS=3
BACKEND_EXTRAS=market-data
```

Tushare Pro 是个人研究配置的默认主数据源，提供证券主表、未复权日 K、复权因子、财务报表与股本数据。Token 只放在本机 `.env`。不同接口所需积分和权限可能变化，请以 [Tushare 官方接口文档](https://tushare.pro/document/2) 为准。

- `TUSHARE_REQUEST_INTERVAL_SECONDS`：真实请求的最小间隔；默认约 171 次/分钟。
- `TUSHARE_SHARE_HISTORY_YEARS`：首次同步股本变更历史的回溯年数，越大越慢。
- `BACKEND_EXTRAS=market-data`：安装锁定版本的 Tushare 和 AKShare。
- AKShare 只会在设置页被明确选中后使用；不会因 Tushare 失败自动切换。

### 可选：TickFlow

```ini
TICKFLOW_API_KEY=
```

TickFlow 仍可在 **设置 → 数据源** 明确选择，用于其套餐支持的数据集。选择某个来源只修改该来源明确声明支持的数据集。

---

## AI(可选)

用于自然语言生成策略。**所有配置留空即跳过**,不影响核心功能。支持任意 OpenAI 兼容接口。

```ini
AI_PROVIDER=openai_compat              # openai_compat | ollama
AI_BASE_URL=https://api.deepseek.com/v1
AI_API_KEY=                            # 留空 = 关闭 AI
AI_MODEL=deepseek-chat
AI_DAILY_TOKEN_BUDGET=500000           # 每日 token 预算上限
```

| 配置项 | 说明 |
| :--- | :--- |
| `AI_PROVIDER` | `openai_compat`(OpenAI 兼容,支持 DeepSeek / 通义 / OpenAI 等)或 `ollama`(本地模型) |
| `AI_BASE_URL` | 接口地址,如 DeepSeek `https://api.deepseek.com/v1` |
| `AI_API_KEY` | 留空则关闭 AI 功能 |
| `AI_MODEL` | 模型名,如 `deepseek-chat` |
| `AI_DAILY_TOKEN_BUDGET` | 每日 token 预算,超限后当日不再调用 |

接入示例见 [strategy.md](./strategy.md) 的「AI 生成策略」章节。

---

## 服务

```ini
HOST=127.0.0.1        # 个人自用默认只监听本机
PORT=3018             # 服务端口
LOG_LEVEL=INFO        # DEBUG | INFO | WARNING | ERROR
```

- `HOST`：默认 `127.0.0.1`，仅本机可访问。只有明确需要局域网部署并配置认证后才改为 `0.0.0.0`。
- `PORT`:默认 `3018`,改端口后 Docker 映射、SSH 转发命令里的端口也要同步改
- `LOG_LEVEL`:排查问题时改 `DEBUG`

---

## 数据

```ini
DATA_DIR=./data       # Parquet / DuckDB 数据存储目录
```

整个 `data/` 目录都不纳入 git —— 行情 K线、财务、自选、回测、监控记录,乃至概念/行业扩展数据,全部是程序运行时生成/拉取的用户数据。

如需迁移数据,直接拷贝整个 `data/` 目录即可。详见 [deployment.md → 更新代码](./deployment.md#更新代码已部署用户必读)。

---

## 访问密码(公网部署)

```ini
AUTH_PASSWORD=你的密码    # 至少 6 位;仅首次生效,已设过则不覆盖
```

面板首次设置访问密码时,出于安全考虑**仅允许本机或内网访问**(防公网陌生人抢先设置锁死面板)。公网服务器部署可通过此环境变量预置首个密码。

详细步骤、SSH 转发方案、重置密码方法见 [deployment.md → 访问密码设置](./deployment.md#访问密码设置公网部署必读)。

---

## 后端依赖 Extras(可选)

```ini
BACKEND_EXTRAS=market-data  # Tushare + AKShare；可追加 legacy-cpu/backtest
```

多个 extra 用空格分隔，例如 `market-data legacy-cpu backtest`。Docker 构建和启动脚本都会读取此值。详见 [deployment.md → 老 CPU 兼容](./deployment.md#老-cpu-兼容avx2fma-缺失)。

---

## 配置优先级

1. **面板设置页**(`设置 → ...`):UI 修改后立即生效,持久化到 `data/`
2. **`.env` 文件**:启动时读取
3. **环境变量**:Docker / 系统环境变量,优先级最高

> 多数配置可在面板设置页修改,无需手动编辑 `.env`。仅 AI Key、API Key 等敏感项建议放 `.env`(不提交到 git)。
