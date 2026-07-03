# C11：策略导出（TDX / 同花顺公式）实现计划

> **面向 AI 代理的工作者：** 只导出无状态日线信号。不能导出的策略要明确拒绝，别生成错误公式。

**目标：** 把 panel 策略构建器中的简单日线条件导出为通达信/同花顺公式文本，方便用户在外部软件观察信号。

**现状证据：**
- panel 已有策略构建器、自定义信号和回测页。
- Vibe 提到 Pine/TDX/MT5/vnpy，但本产品定位 A 股，先只做 TDX/同花顺。
- 复杂有状态策略、组合优化、Trade Journal 规则不适合公式导出。
- 2026-07-03 已实现：只导出显式 DSL（API body 的 `conditions/expression` 或策略 `META["export"]`），不反解析 Python `filter()`；无 DSL 返回 `ok=false`。

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

- [x] 支持字段：
  - `close/open/high/low/volume/amount`
  - `ma5/ma10/ma20/ma60`
  - `change_pct/turnover_rate/vol_ratio_5d`
- [x] 支持运算：`> >= < <= == AND OR NOT`
- [x] 支持常量数字。
- [x] 支持简单交叉：`cross_up(a,b)` / `cross_down(a,b)`。
- [x] 不支持：
  - 持仓状态
  - position sizing
  - stop loss/take profit 状态机
  - 多标的组合条件
  - Python 表达式

## 任务 2：失败测试

- [x] `ma5 > ma20 AND close > ma60` 可导出。
- [x] `turnover_rate > 3` 可导出。
- [x] 有状态/普通 Python 策略未声明 `META.export` 返回 unsupported。
- [x] 未知字段返回 unsupported，包含字段名。
- [x] TDX 和同花顺输出快照测试。

## 任务 3：TDX 方言

- [x] 字段映射：
  - `close -> C`
  - `open -> O`
  - `high -> H`
  - `low -> L`
  - `volume -> V`
  - `maN -> MA(C,N)`
- [x] 逻辑映射：
  - `AND -> AND`
  - `OR -> OR`
  - `NOT -> NOT`
  - `cross_up(a,b) -> CROSS(a,b)`
- [x] 输出带注释头：
  - strategy id/name
  - generated_at
  - unsupported warnings

## 任务 4：同花顺方言

- [x] 优先复用 TDX 映射；差异集中在小表。
- [x] 若某表达式两边都一样，不复制代码。
- [x] 输出公式文本。

## 任务 5：API

- [x] `POST /api/strategies/{id}/export`
- [x] body：`{"target":"tdx"|"ths"}`，并支持可选 `conditions/expression` 给策略构建器即时导出。
- [x] response：`{"ok":true,"target":"tdx","formula":"...","warnings":[]}`
- [x] unsupported：`ok=false`，HTTP 200；未知 target 返回 HTTP 400。

## 验证

```bash
cd backend
uv run --extra dev pytest tests/services/test_strategy_export.py tests/api/test_strategy_export.py -q
```

已执行：

```bash
cd backend
uv run --extra dev pytest tests/services/test_strategy_export.py tests/api/test_strategy_export_api.py -q
```

## 非目标

- 不做 Pine/MT5/vnpy。
- 不直连 QMT/掘金/Ptrade。
- 不导出有状态交易系统。
- 不保证外部软件指标库与 panel 完全一致；只导出明确定义子集。
