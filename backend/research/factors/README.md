# Factors

因子公式、语义检验、多重检验和引擎级 walk-forward OOS。

当前入口：

- `run_factor_search.py`：候选搜索。
- `run_factor_semantic.py`：语义因子检验。
- `run_factor_oos.py` / `run_factor_walkforward.py`：因子级 OOS。
- `run_factor_engine_wf.py`：统一 universe 与真实引擎 walk-forward 口径源。

因子筛选、参数和 early stopping 只能读取训练折。历史产物不能替代当前封存结论。
