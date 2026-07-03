# C11：策略导出（TDX / 同花顺公式）实现计划

> **面向 AI 代理的工作者：** 只导出无状态日线信号。不能导出的策略要明确拒绝，别生成错误公式。

**目标：** 把 panel 策略构建器中的简单日线条件导出为通达信/同花顺公式文本，方便用户在外部软件观察信号。

**现状证据：**
- panel 已有策略构建器、自定义信号和回测页。
- Vibe 提到 Pine/TDX/MT5/vnpy，但本产品定位 A 股，先只做 TDX/同花顺。
- 复杂有状态策略、组合优化、Trade Journal 规则不适合公式导出。

**范围：** 导出文本，不连接券商，不生成下单脚本。

## 文件

| 文件 | 动作 |
|---|---|
| `backend/app/services/strategy_export.py` | 创建 AST/DSL 到公式转换 |
| `backend/app/api/strategies.py` 或现有策略 router | 增加 export 端点 |
| `backend/tests/services/test_strategy_export.py` | 创建 |
| `backend/tests/api/test_strategy_export.py` | 创建 |
| `frontend/src/pages/backtest/StrategyBacktest.tsx` | 后续加按钮，不是首要 |

## 任务 1：限定 DSL 子集

- [ ] 支持字段：
  - `close/open/high/low/volume/amount`
  - `ma5/ma10/ma20/ma60`
  - `change_pct/turnover_rate/vol_ratio_5d`
- [ ] 支持运算：`> >= < <= == AND OR NOT`
- [ ] 支持常量数字。
- [ ] 支持简单交叉：`cross_up(a,b)` / `cross_down(a,b)`，若现有 DSL 已有对应节点才做。
- [ ] 不支持：
  - 持仓状态
  - position sizing
  - stop loss/take profit 状态机
  - 多标的组合条件
  - Python 表达式

## 任务 2：失败测试

- [ ] `ma5 > ma20 AND close > ma60` 可导出。
- [ ] `turnover_rate > 3` 可导出。
- [ ] 有状态 stop_loss 策略返回 unsupported。
- [ ] 未知字段返回 unsupported，包含字段名。
- [ ] TDX 和同花顺输出快照测试。

## 任务 3：TDX 方言

- [ ] 字段映射：
  - `close -> C`
  - `open -> O`
  - `high -> H`
  - `low -> L`
  - `volume -> V`
  - `maN -> MA(C,N)`
- [ ] 逻辑映射：
  - `AND -> AND`
  - `OR -> OR`
  - `NOT -> NOT`
  - `cross_up(a,b) -> CROSS(a,b)`
- [ ] 输出带注释头：
  - strategy id/name
  - generated_at
  - unsupported warnings

## 任务 4：同花顺方言

- [ ] 优先复用 TDX 映射；差异集中在小表。
- [ ] 若某表达式两边都一样，不复制代码。
- [ ] 输出 `.txt` 公式文本。

## 任务 5：API

- [ ] `POST /api/strategies/{id}/export`
- [ ] body：`{"target":"tdx"|"ths"}`
- [ ] response：`{"ok":true,"target":"tdx","formula":"...","warnings":[]}`
- [ ] unsupported：`ok=false`，HTTP 200 或 422 二选一并在测试固定。

## 验证

```bash
cd backend
uv run --extra dev pytest tests/services/test_strategy_export.py tests/api/test_strategy_export.py -q
```

## 非目标

- 不做 Pine/MT5/vnpy。
- 不直连 QMT/掘金/Ptrade。
- 不导出有状态交易系统。
- 不保证外部软件指标库与 panel 完全一致；只导出明确定义子集。

