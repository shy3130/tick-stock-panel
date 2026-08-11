# 后端策略开发指南

当前项目不包含自定义前端。策略通过 Python 模块、回测 API、MVP CLI 和研究脚本使用。

## 生命周期

生命周期事实源是 `backend/app/strategy/catalog.py`：

- `core`：默认展示的产品入口，目前仅均线多头、趋势突破、回踩支撑。
- `tool`：自定义因子载体，不代表独立 alpha。
- `experimental`：能力可运行但没有通过 fresh OOS，默认隐藏。
- `legacy`：为兼容保留的旧模板，默认隐藏。
- `user`：用户或 AI 生成策略，证据状态从 `unverified` 开始。

当前共有 22 个 builtin；数量不等于有效策略数量。显式 ID 可以运行隐藏策略，但不能
绕过其证据状态。

## 新增 builtin

1. 文件放入 `backend/app/strategy/builtin/<descriptive_name>.py`，ID 与文件名一致。
2. 实现 `META`、`EXECUTION_BACKEND="matrix_native"` 和 `MATRIX_STRATEGY`。
3. 只使用安全白名单依赖；builtin 之间禁止互相 import。
4. `compute_signals` 输出 finite `float32` score，并显式声明所需字段和暖机 bars。
5. 在 `catalog.py` 登记生命周期。新策略默认必须是 experimental 或 legacy，不能直接 core。
6. 测试放 `backend/tests/strategy/` 或 `backend/tests/backtest/`。
7. 收益研究放 `backend/research/<domain>/`；测试折不得参与选参。

## 因子组合

`custom_factor` 可作为独立研究工具，也可通过 `StrategyBacktestConfig.composition` 与现有
matrix-native 策略组合。组合支持 AND/OR 入场、截面百分位评分和任一组件退出。是否组合
是可选配置；机制可用不代表组合已经通过 OOS。

## 可解释选股

`quality_momentum_v1` 是 experimental 策略。执行评分事实源位于
`backend/app/strategy/builtin/quality_momentum_v1.py`，逐股审计位于
`backend/research/selection/`。当前行业分类不是 point-in-time，消息历史库尚未提供，
两者不得倒灌历史评分。

## 最低验证

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m scripts.check_structure
.\.venv\Scripts\python.exe -m pytest tests\strategy tests\backtest -q
```

完整目录与依赖规范见 [`ARCHITECTURE.md`](../ARCHITECTURE.md)。旧页面操作说明已归档到
`docs/archive/legacy-panel/`。
