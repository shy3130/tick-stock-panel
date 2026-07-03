# C7：Finance Skills 方法论库实现计划

> **面向 AI 代理的工作者：** Markdown 资产先行。筛掉非 A 股内容，别把方法论库做成 RAG 平台。

**目标：** 建一个小型金融方法论库，AI 分析按场景加载对应 Markdown：大盘复盘、个股分析、交易复盘、因子研究、风险诊断。

**现状证据：**
- 项目已有 `docs/strategy-guide.md` 等静态指南，但 AI prompt 没有统一按场景加载的方法论资产。
- Vibe skills 多，混有美股/crypto/期权/实盘连接内容，不能整包搬。
- Trade Journal 和 Shadow Account 术语已在 `CONTEXT.md` 固化，相关 skill 需与该口径一致。

**范围：** `docs/skills/` + 简单索引 + 后端 loader。无 UI、无用户上传技能、无外部 RAG。

## 文件

| 文件 | 动作 |
|---|---|
| `docs/skills/index.json` | 创建场景索引 |
| `docs/skills/*.md` | 创建 8-12 篇筛选后的方法论 |
| `backend/app/services/skill_context.py` | 创建 loader |
| `backend/tests/services/test_skill_context.py` | 创建 |

## 任务 0：失败测试清单

- `test_load_by_scenario`：当前 loader 不存在，应失败；实现后 trade_journal 场景能加载本地方法论文本。
- `test_reject_path_escape`：索引 path 写成 `../x` 必须抛错，防止 prompt loader 变成任意文件读取。
- `test_total_truncation`：超长技能文本按总预算截断，避免 AI prompt 失控。
- `test_unknown_scenario_empty`：未知场景返回空字符串，不阻断主分析。
- `test_a_share_filtering_terms`：抽样检查保留下来的文档不含 crypto/options/DeFi/券商实盘连接等 Vibe 原项目无关语境。

## 任务 1：筛选和改写 Markdown

- [ ] 保留主题：
  - `market-recap`
  - `sector-rotation`
  - `trade-journal`
  - `shadow-account`
  - `alpha-zoo`
  - `factor-research`
  - `risk-analysis`
  - `backtest-diagnose`
  - `technical-basic`
  - `candlestick`
  - `multi-factor`
  - `market-microstructure`
- [ ] 剔除：美股、crypto、期权、DeFi、券商实盘连接、shell/file edit、swarm。
- [ ] 每篇改成 panel 口径：A 股、本地数据源、Trade Journal 不送原始流水给 LLM。
- [ ] 每篇结构固定：
  - `# title`
  - `适用场景`
  - `检查清单`
  - `常见误判`
  - `输出约束`

## 任务 2：索引契约

`docs/skills/index.json`：

```json
[
  {
    "id": "trade-journal",
    "title": "交易流水复盘",
    "path": "trade-journal.md",
    "scenarios": ["trade_journal"],
    "tags": ["journal", "behavior"],
    "max_chars": 6000
  }
]
```

- [ ] JSON 可被 stdlib 读取。
- [ ] path 必须限制在 `docs/skills` 下，拒绝 `../`。

## 任务 3：loader

- [ ] `load_skill_context(scenario: str, max_chars: int = 12000) -> str`
- [ ] 按 index 中 scenarios 匹配。
- [ ] 单篇按 `max_chars` 截断，总体再按参数截断。
- [ ] 输出前加注释头：`以下为本地方法论，不是实时数据`。

## 任务 4：测试

- [ ] `test_load_by_scenario()`：trade_journal 能加载对应文档。
- [ ] `test_reject_path_escape()`：index 里 `../x` 抛 ValueError。
- [ ] `test_total_truncation()`：超长文档被截断。
- [ ] `test_unknown_scenario_empty()`：未知场景返回空字符串。

## 任务 5：AI 接入

- [ ] 个股分析：`technical-basic` + `risk-analysis`
- [ ] 大盘复盘：`market-recap` + `sector-rotation`
- [ ] Trade Journal：`trade-journal`
- [ ] 回测解释：`backtest-diagnose` + `factor-research`
- [ ] 接入失败不得阻断主分析，只记录 warning。

## 验证

```bash
cd backend
uv run --extra dev pytest tests/services/test_skill_context.py -q
```

## 非目标

- 不做在线技能市场。
- 不做向量库。
- 不允许用户上传任意 prompt skill。
- 不搬 Vibe runtime 指令。
