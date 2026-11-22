# 开放接口示例 (Open API Examples)

外部程序接入 tick-stock-panel 核心能力的可运行最小示例 — 纯 Python 标准库, 零第三方依赖。

## 准备

1. 启动面板 (或桌面客户端), 进入 **设置 → 开放接口**, 新建 Token 并勾选所需 scope:

   | 脚本 | 所需 scope |
   | :--- | :--- |
   | `01_pull_market.py` | `read:market` |
   | `02_write_ext_data.py` | `write:ext` + `read:ext` |
   | `03_run_backtest.py` | `read:analysis` + `run:backtest` |
   | `04_events_stream.py` | 任意 (票据继承 scope) |

2. 在终端环境中保存 `TSP_TOKEN`，使用目标后端实际地址。下面从仓库根目录执行。

   ```bash
   export TSP_BASE=http://127.0.0.1:3018   # 面板实际地址
   curl -fsS "${TSP_BASE}/api/openapi.json?tier=a" -o tsp-openapi.json
   python3 examples/open-api/01_pull_market.py
   ```

## 脚本说明

| 脚本 | 演示能力 |
   | :--- | :--- |
   | `01_pull_market.py` | 标的搜索 (拼音可用) / 日K / 市场总览 |
   | `02_write_ext_data.py` | 程序化写入扩展表行数据 + 读回验证 (数据同台分析闭环) |
   | `03_run_backtest.py` | 读取策略清单 / 使用 Tivelo 提交普通策略回测并读取结果 |
   | `04_events_stream.py` | SSE 短期票据 + 事件流 (告警推送) |

脚本 01 使用本地行情。脚本 02 需要事先配置的扩展表，运行前核对表和必填字段。脚本 03 需要本机至少有一个有效策略及对应行情。旧信号组合对应 `POST /api/backtest/run`。

普通策略回测默认 `qfq`，可在请求体中传 `"price_basis": "hfq"` 或 `"price_basis": "dynamic"`。动态复权要求服务端安装并授权兼容的 Tivelo Pro，同时使用 A 股日线策略和日线成交方式。[动态复权条件](../../docs/engine-platform/workbench-research-jobs.md#动态复权扩展)

## 通用约定

- 认证: `Authorization: Bearer tsp_...` 头; 与面板密码会话并行, 互不影响
- 限流: 每 Token 默认 120 次/分钟, 响应头 `X-RateLimit-Remaining` 实时可见, 超限 429 + `Retry-After`
- 错误语义: 401 Token 无效或已吊销 / 403 scope 不足或端点未开放 / 429 超限
- 机器可读契约: `GET /api/openapi.json?tier=a` — 可直接导入 Postman / 生成客户端
- 完整文档: [docs/features.md → 开放接口](../../docs/features.md#-开放接口open-api--tier-a)

## 注意

- `write:ext` 只能向**已配置好的**扩展表写行数据; 表结构 (字段/模式/拉取) 属管理面, Token 不可触及
- 写入的数据会进入策略信号 / 因子 / 回测的数据面, 谨慎授予该 scope
- SSE 票据一次性且 60 秒过期, 断线重连需重新 `POST /api/events/ticket`

默认端口 3018 对应后端。管理和插件安装通过面板会话完成。遇到 403 时优先对照导出的契约、Token scope 和网络门禁。[AI 接入与只读验证](../../docs/ai-integration.md#连接已有工作台)
