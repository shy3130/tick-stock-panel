# 趋势监控机会 / 异动重点解读列语义验收

状态：实现及语义验收完成；尚未部署

## 验收需求

- `REQ-DOW-MONITOR-KEY-INTERPRETATION-COLUMN-001`
- 权威规格：
  `docs/superpowers/specs/2026-07-30-dow-monitor-key-interpretation-column-design.md`

## 用户批准的原型

- 原型：
  `output/playwright/dow-monitor-opportunity-interpretation-prototype.html`
- 本地审阅地址：
  `http://127.0.0.1:8765/output/playwright/dow-monitor-opportunity-interpretation-prototype.html`
- 截图：
  `output/playwright/dow-monitor-opportunity-interpretation-prototype.png`
- 用户明确批准该版原型后才开始生产代码开发。
- 原型验证：10 个表头顺序正确；7 个代表场景均固定三行；重点解读列
  `320px`；`1100px` 视口保留表格内部横向滚动；控制台无错误或警告。

## 测试先行证据

### RED

- `interpretationMarketContext.test.ts` 首次运行因底层上下文模块不存在而失败；
- `keyInterpretation.test.ts` 首次运行因场景解释器不存在而失败；
- `KeyInterpretationCell.test.tsx` 首次运行因展示组件不存在而失败；
- 列表接入前，`suddenAnomalyHighlights.test.ts` 有 1 项因未导出固定指标集合失败，
  `DowMonitorList.test.tsx` 有 3 项因缺少新表头、单元格和异动解释失败；
- 帮助页接入前，`DowMonitorHelp.test.tsx` 有 2 项因缺少“重点解读”标题和目录入口失败。

### GREEN

- 底层价格上下文与既有列表呈现：21 项通过；
- 底层上下文与场景解释器：12 项通过；
- 单元格、异常状态、列表和既有呈现：49 项通过；
- 帮助页：2 项通过；
- 最终五文件聚焦语义套件：5 个文件、25 项通过；
- 相关规格契约：6 项通过，无跳过。

## 语义验收结果

- `interpretationMarketContext.ts` 独立证明形成中 K 线排除、同日过滤、连续交易分钟、
  12 根已完成 5m 区间、确认区间、前次确认区间、实时报价及延迟降级；
- `keyInterpretation.ts` 使用固定优先级和集中阈值，确定性输出机会、风险、异动、
  观察或数据状态；
- 机会或明确风险至少需要两个唯一证据维度；单项 10 秒异动只能进入待确认；
- 实时跨越与已完成 5m 收盘确认分离，确认价、失效价和下一参考价最多三个且有名称；
- 缺失价格不补零，延迟数据返回“关键数据延迟”；
- 输出禁止“建议买入”“建议卖出”“立即操作”“止盈”“止损”；
- 列表固定 10 列，新列位于“日内走势”和“趋势 / 位置”之间，最小宽度 320px；
- 原始指标、正式信号、北京时间、分页、详情开合和既有六项异动标记均有回归测试；
- 帮助页直接读取 `INTERPRETATION_THRESHOLDS`，文档阈值不另设副本。

## 构建与浏览器证据

- `pnpm --dir frontend build` 成功，2715 个模块完成转换；
- 构建产物包含：
  - `assets/DowMonitor-DTFoyzTp.js`
  - `assets/DowMonitorHelp-1tW5eFMm.js`
  - `assets/interpretationMarketContext-CsUSrBHL.js`
- 静态包检索确认列表包包含“重点解读”“放量突破正在形成”，帮助包包含
  “重点解读”“最近12根已完成5分钟K线”；
- 本地完整 3018 壳加载构建后的帮助页，确认 9 个目录入口、重点解读 12 个子标题、
  无文档级横向溢出，控制台 0 个错误/警告；
- 本地临时 3018 服务没有初始化真实道氏监控数据，因此正式列表的数据行视觉证据使用
  已批准原型；列表语义、交互和回归证据使用 React 可执行测试，不用截图代替。

## 全量基线

- 一次性全量前端：42 个文件中 41 个通过，192 项通过、2 项跳过、1 项失败；
- 唯一失败是既有
  `src/pages/Screener.dow-strategy.test.tsx` 仍断言已不存在的
  “道氏趋势 · 多周期”，与本需求修改文件和趋势监控聚焦套件无关；
- `python scripts/check_spec_compliance.py` 仍只报告既有基线：
  - 过期例外 `EXC-COLLECTION-MONITOR-PREACCEPTANCE-DEPLOY-001`；
  - `REQ-DOW-MONITOR-DETAIL-TOGGLE-LAYOUT-001` 的一个测试路径位于 `frontend/src/`。

## 变更边界

- 未修改后端、19912 道氏引擎、WebSocket 订阅、ClickHouse、通知、监控池、排序或
  自动交易；
- 未生成、清除、翻转或升级正式买卖信号；
- 本轮仅构建和本地验证，没有发布到 10.28，也没有替换本机正式服务。
