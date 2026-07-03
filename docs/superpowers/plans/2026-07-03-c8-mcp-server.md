# C8：MCP Server 暴露 panel tools 实现计划

> **面向 AI 代理的工作者：** 复用现有 `agent_tools.TOOLS`，不搬 Vibe agent runtime。先 stdio，只读。

**目标：** 提供一个本地 MCP/stdio 工具服务，让外部 AI 客户端调用 panel 的受控只读工具：capabilities、行情、选股、回测、概览、扩展数据。

**现状证据：**
- 已有 `backend/app/services/agent_tools.py` 和 `backend/app/api/agent.py` 骨架。
- 项目业务能力主要是 FastAPI/UI，不需要 LangGraph/swarm/channel runtime。
- 工具暴露必须避免文件读写、shell 执行和未审 API。

**范围：** 本地 stdio server + tool schema + self-test。默认不开放 TCP。

## 文件

| 文件 | 动作 |
|---|---|
| `backend/app/services/agent_tools.py` | 补 schema、参数校验、只读约束 |
| `backend/app/mcp_server.py` | 创建 stdio 入口 |
| `backend/tests/services/test_agent_tools.py` | 补 schema/执行测试 |
| `backend/tests/test_mcp_server.py` | 可选 self-test |
| `docs/mcp.md` | 使用说明 |

## 任务 1：工具 registry 契约

- [ ] 每个工具结构：
  - `name`
  - `description`
  - `input_schema`
  - `handler`
  - `read_only=True`
- [ ] 首批工具：
  - `get_capabilities`
  - `list_strategies`
  - `run_screener`
  - `get_kline`
  - `get_market_overview`
  - `run_backtest`
  - `list_ext_data`
- [ ] `run_backtest` 允许计算但不得保存/修改策略定义。
- [ ] 所有工具输出做行数和字符数截断。

## 任务 2：失败测试

- [ ] `test_all_tools_have_schema()`。
- [ ] `test_unknown_tool_rejected()`。
- [ ] `test_tool_result_truncated()`。
- [ ] `test_no_write_tools_registered()`：registry 中没有文件写入/shell 工具。
- [ ] `test_get_kline_rejects_bad_symbol()`。

## 任务 3：stdio server

- [ ] `python -m app.mcp_server`
- [ ] 支持 JSON lines：
  - `{"id":1,"method":"tools/list"}`
  - `{"id":2,"method":"tools/call","params":{"name":"get_capabilities","arguments":{}}}`
- [ ] 不加依赖；若后续决定接标准 MCP SDK，另开计划。
- [ ] `--self-test`：列工具并调用 `get_capabilities`，退出码 0。

## 任务 4：安全边界

- [ ] 禁止任意文件路径参数。
- [ ] 禁止 URL 抓取；C6 reader 单独处理。
- [ ] 默认 stdio，不监听端口。
- [ ] 复用 app auth 不适合 stdio；文档注明只在本机用户会话运行。

## 任务 5：文档

`docs/mcp.md` 写：

- 启动命令
- 客户端配置示例
- 工具列表
- 安全边界
- 常见错误

## 验证

```bash
cd backend
uv run --extra dev pytest tests/services/test_agent_tools.py -q
uv run python -m app.mcp_server --self-test
```

## 非目标

- 不搬 LangGraph/swarm/channel runtime。
- 不提供 shell/file edit 工具。
- 不开放 TCP server。
- 不绕过 provider/data access 层直连 DB。

