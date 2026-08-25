# 策略寻优（多轴搜索）设计

> 状态：已落地（V1 有界搜索）  
> 日期：2026-08-19  
> 入口：`/backtest` →「策略寻优」；后端 `app/backtest/optimizer.py` + `app/api/backtest_optimizer.py`

## 1. 问题

用户希望对「最近 8 年、分阶段、不同板块 / 个股 / 行业 / 持仓周期 / 策略」做自动回测，找出“最优策略”。  
业界共识是：**全样本收益排序几乎必然过拟合**，不能直接当准入证据。

## 2. 调研结论（引入什么、不引入什么）

| 实践 | 出处 | 本仓库怎么用 |
|---|---|---|
| 训练 / 留出分离，禁止用全样本选参 | Walk-Forward / IBKR / QuantInsti | 默认 8 年切 75% 训练 + 25% 留出；**只按训练期打分** |
| 滚动 Walk-Forward | Wikipedia / QuantInsti | 已有 `robustness.walk_forward_*`（局部邻域）；本模块不重复做折内选参 |
| Deflated Sharpe Ratio | Bailey & López de Prado, 2014 ([SSRN 2460551](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)) | 对全部训练期试验的夏普做选择偏差修正，输出 `dsr` |
| Probability of Backtest Overfitting (CSCV) | Bailey, Borwein, López de Prado, Zhu, 2014 | 训练期收益切 8 块，C(8,4)=70 组对称交叉验证，输出 `pbo` |
| 单目标 + 约束 | [QuantConnect Optimization](https://www.quantconnect.com/docs/v2/cloud-platform/optimization/objectives) | 目标 = sharpe / calmar / total_return / risk_adjusted；约束 = 最少成交、最大回撤 |
| 试验次数告警 | [QuantConnect Research Guide](https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/research-guide) | `n_trials > 70` 显式 `high_trial_count` 告警 |
| 假设驱动、少参数 | 同上 | **默认只用策略基线参数**，不把参数网格再乘一遍；细调仍走既有参数网格 |
| TPE / Optuna | 常见超参框架 | **不引入新依赖**；超预算时用确定性种子抽样 |

明确不宣称「找到了最优策略」。推荐栏只表示：**训练期排名靠前且留出期通过机械门禁**。

## 3. 搜索轴

| 轴 | 默认 | 上限 / 说明 |
|---|---|---|
| 策略 | 用户勾选的 builtin/custom/composite | 至少 1 个；默认不含 AI 生成草稿 |
| 策略组合 | 叶子策略两两 **union 并集** | 最多 8 组；临时注册 `combo:*`，**不写策略池**；禁止嵌套叠加 |
| 股票池 | 全 A + 沪深主板 / 创业板 / 科创板 / 北交所 | 行业取成员数最多的前 N（默认 8）；自定义标的；`per_symbol` 仅当自定义标的 ≤ 8 |
| 持仓周期 | 5 / 10 / 20 日 | 即「不同周期」——日频撮合下的持仓天数 |
| 成交口径 | `open_t+1` | 可选再加 `close_t` |
| 参数 | 策略默认值 | 不在本模块展开网格 |
| 分阶段 | 按自然年切片报告 | **只报告、不参与排序**，避免把阶段结果当选参依据 |

笛卡尔积超过 `max_scenarios`（默认 120，硬顶 240）时按 `scenario_id` 稳定哈希抽样，并标记 `truncated`。

## 4. 协议

```
冻结窗口 [end-8y, end]（end = 可信 enriched 上限，可被数据起点夹逼）
        │
        ├─ 训练窗 [start, split]  ──► 全部场景只在此窗回测、打分、算 DSR/PBO
        │
        └─ 留出窗 (split, end]    ──► 仅对训练期 Top-K 且成交数/未平仓达标的场景重跑
                                      （不要求训练收益为正）；留出收益>0 ∧ 成交数≥min_trades ∧ 无 pending
                                      才进入 recommended
```

- 等权组合：只对 **留出期已通过门禁** 的候选，把归一净值等权合成；这是策略组合近似，不是资金约束账户。
- 模式固定 `position`。`full` / 候选执行曲线禁止进入本搜索（与稳健性口径一致）。
- 全 A / 行业 / 板块池无法证明历史时点成分，结果必须带 `survivorship_bias`。

## 5. 非目标

- 不自动把任何策略写入策略池。
- 不模拟成交量冲击 / 部分成交。
- 不做全局参数优化，也不把 Walk-Forward 局部邻域伪装成全局搜索。
- 不引入 Optuna / 付费云优化。
- 不构成荐股或下单。
