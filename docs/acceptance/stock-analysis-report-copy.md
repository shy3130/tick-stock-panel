# 个股分析报告复制语义验收

- 验收对象：`SPEC-STOCK-ANALYSIS-REPORT-COPY-001`
- 当前状态：本地语义验收及生产部署验收通过。
- 验收边界：仅变更报告复制交互，不改变报告生成、保存、历史接口或正文内容。

## 必须证明的行为

- 完成态及历史报告存在正文时显示“复制报告”，生成态隐藏。
- 复制值是原始 Markdown 全文。
- Clipboard API 成功及失败后的文本域回退均可执行。
- 成功显示“已复制”，失败显示“复制失败”，随后恢复默认文案。
- 标题栏按钮在手机宽度下保持可见且具备可访问名称。

## 验收证据

## 2026-07-21 本地验收证据

- `copyText.test.ts` 3 项通过：Clipboard API 成功、API 拒绝后的只读文本域回退、双重失败返回及节点清理。
- `StockAnalysisDialog.test.tsx` 4 项通过：完成态 Markdown 全文复制及成功反馈、历史报告复制、生成态隐藏、失败反馈。
- 既有 `stock-analysis-five-dimension.test.tsx` 1 项通过，证明原分析入口行为未回归。
- `pnpm build` 通过，共转换 2696 个模块；生成资产包含“复制报告”“已复制”“复制失败”文案。
- 完整前端测试套件 21 个文件、55 项测试全部通过。
- 标题栏按钮使用 `shrink-0`、32px 高度和手机端较窄标题内边距，文字操作不使用响应式隐藏类。

## 生产验收证据

- 生产镜像：`tickflow-stock-panel-app:report-copy-20260721-runtime`。
- 生产容器 `TickFlow_Stock_Panel` 状态为 `running`、`Restarting=false`。
- `http://127.0.0.1:3018/stock-analysis?market=cn` 返回 HTTP 200。
- 容器内实际服务资产 `/app/static/assets/index-Bo3UG04n.js` 包含“复制报告”文案。
- `/app/data` 等原有数据及配置挂载保持不变。
