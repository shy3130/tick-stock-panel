# AI 个股五维分析语义验收

- 验收对象：`SPEC-STOCK-AI-FIVE-DIMENSION-001`
- 产品决策：成交额用于新增独立资金状态分析，港股页面使用港股规则。
- 语义边界：成交额反映交易活跃度，不代表净流入；没有方向性成交数据时禁止“主力流入/流出”结论。
- 后端语义验收：`backend/tests/test_stock_analyzer_prompt.py` 验证成交额统计值、港股/港元口径、非净流入红线、市场后缀推断及缺失成交额降级。
- 前端语义验收：`frontend/src/pages/stock-analysis-five-dimension.test.tsx` 验证五维文案及 `market=hk` 从页面传入分析任务。
- 2026-07-20 本地执行结果：后端全量 `487 passed`；前端定向 `1 passed`；`pnpm build` 成功；规格合规检查成功。
