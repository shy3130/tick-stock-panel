# MCP stdio tools

启动：

```bash
cd backend
uv run python -m app.mcp_server
```

自检：

```bash
cd backend
uv run python -m app.mcp_server --self-test
```

协议为 JSON lines：

```json
{"id":1,"method":"tools/list"}
{"id":2,"method":"tools/call","params":{"name":"get_capabilities","arguments":{}}}
```

工具：`get_capabilities`、`list_strategies`、`get_kline`、`run_screener`、`run_backtest`、`get_market_overview`、`list_ext_data`。

边界：只走本地 stdio，不监听 TCP；不提供 shell、文件写入或任意 URL 抓取；数据访问复用 panel 现有 repository/service。
