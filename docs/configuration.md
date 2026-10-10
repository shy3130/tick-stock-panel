# 配置详解

所有配置从根目录 `.env` 读取(复制 `.env.example` 开始),也可在面板 **设置** 页面可视化修改。本文件解释每个配置项的作用。

部署相关配置(端口/密码/老 CPU 兼容)的实操见 [deployment.md](./deployment.md)。

---

## 数据源:TickFlow

```ini
TICKFLOW_API_KEY=              # 留空 = None 模式(历史日K免费);填 Key = 按订阅档位解锁
```

TickFlow 是内置默认数据源;同时支持插件化接入第三方数据源(YAML 声明自有接口见 [custom-data-source.md](./custom-data-source.md),插件开发见 [plugin-development.md](./plugin-development.md)),在面板 **设置 → 数据源** 切换。

- **留空(None 模式)**:通过 free-api 使用历史日 K(当日数据盘后 1-2 小时可用),**无需付费**即可体验核心策略/回测功能
- **填入 API Key**:按你的订阅档位解锁更多能力

### 实时行情按档位

| 档位     | 实时能力                                 |
| :------- | :--------------------------------------- |
| Free     | 自选页前 5 个标的实时监控(最低 6 秒刷新) |
| Starter+ | 全市场实时行情                           |
| Pro      | 分钟 K + 盘口                            |
| Expert   | WebSocket + 财务数据 + 全量分钟          |

> 完整能力矩阵见 [tickflow.org/pricing](https://tickflow.org/pricing/),高等档位含较低档全部权益。
> 在面板 **设置 → 凭据与能力** 点「重新检测」可查看当前档位标签。
>
> **档位仅适用于 TickFlow 数据源**。功能门槛的统一标准是"能力"(`kline.minute.batch`、`depth5.batch`、`financial` 等能力键):其他第三方/自定义数据源以声明的数据集能力为准,系统会按当前数据源配置自动合并判定,UI 提示一律以能力名表达,不再依赖 TickFlow 档位名。

### 全量分钟 (full_minute)

「全量分钟」是一项**独立能力**(能力键 `full_minute`,探测名 `intraday.universe`),与其他能力同样**可路由**:盘中把全市场当日 1 分钟 K 持续增量落盘到本地 `data/kline_minute/` 当日分区,分钟策略(`minute_filter`)与分时视图即可读到新鲜数据。接入方式二选一:

- **TickFlow Expert**:配置 Expert 档 Key,零配置即用(修复轮 `intraday.batch` + 稳态 `intraday.universe` 单请求增量)
- **插件/自定义源**:声明 `full_minute` 数据集并在 **设置 → 数据源 → 全量分钟** 路由到该源 — Python 插件实现 `get_intraday_batch`(必需)/`get_intraday_latest`(可选,未实现自动降级仅修复轮、节奏下限 60s);YAML 声明式源数据集配置与 `minute` 同形(仅修复轮语义)。契约细节见 [plugin-development.md](./plugin-development.md) 与 [custom-data-source.md](./custom-data-source.md)

接入步骤:

1. **设置 → 凭据与能力**(TickFlow 路径)配置 API Key(Expert 档),或在 **设置 → 数据源** 声明/安装提供 `full_minute` 的源并路由;点「重新检测」后能力列表出现「全量分钟」
2. **开启实时行情**后落盘服务自动启动;仅连续竞价时段(9:30–11:30 / 13:00–15:00)运行,午休/收盘自动暂停与恢复
3. 冷启动(如 10 点才开服务)自动触发**全天修复轮**,一次批量补齐 9:30 起的全部缺口;稳态走**增量轮**(默认 6 秒一轮,可配 3–120 秒),幂等合并滚出全天
4. 与盘后分钟同步写同一分区(`unique(symbol, datetime)` 幂等合并),互不冲突

说明:标的池为 A 股股票(CN_Equity_A),ETF 不在内(分时走批量补拉路径);覆盖滞后超阈值或连续空轮会自动再跑修复轮自愈。

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
HOST=0.0.0.0          # 开发服务监听地址 / Docker 主机绑定地址
PORT=3018             # 开发后端端口 / Docker 主机映射端口
LOG_LEVEL=INFO        # DEBUG | INFO | WARNING | ERROR
```

- `HOST`:`0.0.0.0` 监听所有网卡(容器/公网部署需要);仅本机用可设 `127.0.0.1`
- `PORT`:默认 `3018`;开发模式兼容显式的 `BACKEND_PORT` 覆盖,改端口后 SSH 转发命令也要同步改
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
AUTH_PASSWORD='你的密码'  # 至少 6 位;仅首次生效,已设过则不覆盖
```

面板首次设置访问密码时,出于安全考虑**仅允许本机或内网访问**(防公网陌生人抢先设置锁死面板)。公网服务器部署可通过此环境变量预置首个密码。
密码建议使用单引号包裹，Docker 启动时会把整个原始 `.env` 只读挂载到容器内 `/app/.env`，兼容已有的未加引号配置。容器可以读取其中的密钥但不能修改该文件，请保持主机文件权限为 `600` 并仅运行可信镜像。

详细步骤、SSH 转发方案、重置密码方法见 [deployment.md → 访问密码设置](./deployment.md#访问密码设置公网部署必读)。

---

## 开放接口网络门禁(默认仅本机/内网)

```ini
API_TOKEN_LOCAL_ONLY=1   # 默认;设为 0 显式放开公网 Token 调用
```

API Token 通道(开放接口 / MCP / SSE 票据签发)默认只接受**本机与内网**(`127.0.0.1`、`::1`、`10.x`、`172.16-31.x`、`192.168.x`)请求,公网来源一律 403。本地二次开发的页面、脚本与 MCP 客户端不受影响;门禁在验签之前拦截,公网探测者无法借 401/403 差异探测 Token 有效性,被拦请求也不消耗限流额度。

远程调用请任选其一:

- **SSH 隧道**(推荐,与[访问密码设置](./deploy-password.md#方式二ssh-端口转发)的方式二相同):`ssh -L 3018:127.0.0.1:3018 用户名@服务器IP`,客户端即以本机身份访问;
- **显式放开**:设 `API_TOKEN_LOCAL_ONLY=0` 并重启。

开放接口的设计用途是**本部署的二次开发与个人工具集成,不作为数据对外分发通道**。显式放开公网调用后,行情数据对第三方的再分发需符合数据源服务条款与交易所授权要求,合规责任由部署者承担。反向代理(Nginx)场景请正确配置 `X-Forwarded-For`,后端据此取真实客户端 IP(仅当直连 peer 是本机/内网时才采信该头,防伪造绕过)。

---

## 后端依赖 Extras(可选)

```ini
BACKEND_EXTRAS=             # 留空默认;legacy-cpu 兼容老 CPU
```

老 CPU 无 AVX2/FMA 支持时设为 `legacy-cpu`,会给 Polars 切到 `rtcompat` 运行时;需回测则 `legacy-cpu backtest`。Docker 构建和 `./dev.sh` / `.\dev.ps1` 都会读取此值并同步依赖。详见 [deployment.md → 老 CPU 兼容](./deployment.md#老-cpu-兼容avx2fma-缺失)。

---

## 配置优先级

1. **面板设置页**(`设置 → ...`):UI 修改后立即生效,持久化到 `data/`
2. **`.env` 文件**:启动时读取
3. **环境变量**:Docker / 系统环境变量,优先级最高

> 多数配置可在面板设置页修改,无需手动编辑 `.env`。仅 AI Key、API Key 等敏感项建议放 `.env`(不提交到 git)。
