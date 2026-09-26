# tick-stock-panel MCP 服务器

把面板的开放接口包装成 **MCP (Model Context Protocol)** 工具, 让 Claude / ZCode / Cursor 等 AI 客户端直接查询行情、市场环境、策略与告警, 触发回测。

```
AI 客户端 ⇄ MCP stdio (本服务器) ⇄ HTTP + Bearer Token ⇄ 面板开放网关
```

权限、限流、契约全部复用面板的开放接口层 — 本服务器只是一层薄桥, 面板侧的 Token scope 是最终权威。

## 准备

1. 面板 → **设置 → 开放接口** → 新建 Token, 勾选所需 scope
2. 记下 `tsp_` 开头的明文 (只显示一次)

## 配置到 MCP 客户端

以 Claude Desktop (`claude_desktop_config.json`) / 任何支持 stdio MCP 的客户端为例:

```json
{
  "mcpServers": {
    "tick-stock-panel": {
      "command": "uv",
      "args": ["--directory", "E:/tick-stock-panel/mcp-server", "run", "tsp-mcp-server"],
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

手动验证:

```bash
cd mcp-server
TSP_TOKEN=tsp_xxx uv run tsp-mcp-server   # stdio JSON-RPC, 供客户端拉起
```

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
| `run:backtest` | `run_backtest` | 策略回测 (纯 Polars 引擎) |

未包含: 模拟盘交易类端点 — 不建议把下单能力交给 AI, 需要时可自行在 `server.py` 里按同样模式补。

## 设计说明

- **薄桥原则**: 所有权限/限流/契约由面板网关裁决, 本服务器不持有任何业务逻辑, 面板升级自动受益
- 工具返回原文 JSON, 错误以可读文本返回 — LLM 可据此自行纠正参数
- 更底层的 REST 契约与示例见 [examples/open-api](../examples/open-api/README.md) 与 [docs/features.md → 开放接口](../docs/features.md)
