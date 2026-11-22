# tick-stock-panel MCP 服务器

通过 stdio MCP 将工作台的公开接口交给 AI 客户端，用于查行情、读扩展表和策略、核对告警及运行回测。这份指南对应当前 TSP 候选工作区。

```
AI 客户端 ⇄ MCP stdio (本服务器) ⇄ HTTP + Bearer Token ⇄ 面板开放网关
```

权限、限流、契约全部复用面板的开放接口层 — 本服务器只是一层薄桥, 面板侧的 Token scope 是最终权威。

## 准备

1. 启动工作台，按需同步数据。打开「设置 → 开放接口」新建 Token，勾选任务所需 scope。
2. 保存仅显示一次的 Token 到客户端环境。准备 Python 3.11+、uv 和本项目的 `mcp-server` 目录。

## 配置到 MCP 客户端

下面是采用 `mcpServers` 配置的 stdio 客户端示例。将目录替换为本机的实际绝对路径。

```json
{
  "mcpServers": {
    "tick-stock-panel": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/tick-stock-panel/mcp-server", "run", "--frozen", "tsp-mcp-server"],
      "env": {
        "TSP_BASE": "http://127.0.0.1:3018",
        "TSP_TOKEN": "tsp_xxxx",
        "TSP_SCOPES": "read:market,read:ext,read:analysis"
      }
    }
  }
}
```

- `TSP_BASE`: 面板地址 (默认 `http://127.0.0.1:3018`)
- `TSP_TOKEN`: API Token
- `TSP_SCOPES`: 逗号分隔, 决定暴露哪些工具 (默认只读三档)。**真正的权限裁决在面板网关** — 声明了没授权的 scope, 调用会得到 403, AI 能看到错误并停止

### 验证连接

在仓库根目录执行。终端先配置 `TSP_TOKEN`，必要时设置 `TSP_BASE`。使用只读权限验证时，Token 应包含 `read:market` 和 `read:analysis`。

```bash
uv --directory mcp-server run --frozen python smoke_test.py
```

冒烟会进行握手、列出工具，并查询标的、市场总览和市场环境。查询示例使用已同步的本地数据。单独运行 `tsp-mcp-server` 时，进程等待客户端在标准输入上发 JSON-RPC 消息。

## 工具清单 (按 scope)

| scope | 工具 | 说明 |
| :--- | :--- | :--- |
| `read:market` | `search_instrument` | 代码/名称/拼音搜标的 |
| | `get_kline` | 标的日K (前复权) |
| | `get_index_kline` | 指数日K |
| | `get_market_overview` | 涨跌家数/成交额/涨停连板/情绪 |
| `read:analysis` | `get_regime_state` | 最新市场环境阶段 |
| | `list_strategies` / `get_strategy_detail` | 策略清单与定义 |
| | `get_recent_alerts` | 最近告警 |
| `read:ext` | `list_ext_tables` / `query_ext_table` | 自有数据表查询 |
| `write:ext` | `write_ext_rows` | 写入扩展表 (进入策略数据面, 慎授) |
| `run:backtest` | `run_backtest` | 通过 Tivelo 运行普通策略回测，沿用默认前复权 |

先让客户端成功调用 `search_instrument`，再把返回的代码交给 `get_kline`。需要回测时从 `list_strategies` 获取本机策略 ID。MCP 回测使用默认前复权。要使用后复权或动态复权，可在面板的「复权方式」选择，或通过 [Open API](../examples/open-api/README.md) 的 `price_basis` 指定。

## 设计说明

- **薄桥原则**: 所有权限/限流/契约由面板网关裁决, 本服务器不持有任何业务逻辑, 面板升级自动受益
- 工具返回原文 JSON, 错误以可读文本返回 — LLM 可据此自行纠正参数
- 更底层的 REST 契约与示例见 [examples/open-api](../examples/open-api/README.md) 与 [docs/features.md → 开放接口](../docs/features.md)

如果工具可见但返回 403，分别核对 Token scope、`TSP_BASE` 和开放接口网络门禁。部署问题或要让 AI 开发扩展，见 [AI 接入指南](../docs/ai-integration.md)。
