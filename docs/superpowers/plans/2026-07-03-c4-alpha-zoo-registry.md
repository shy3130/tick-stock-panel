# C4：Alpha Zoo registry / manifest / compare 实现计划

> **面向 AI 代理的工作者：** 先做小型可验证 zoo。不要搬 456 个因子；不要把 pandas 带进业务路径。

**目标：** 在现有 `alpha101_001` 基础上建立 Alpha Zoo 骨架：metadata registry、manifest、批量 compare、strict random-control bench，并扩到 10 个低依赖 Alpha101。每个 Polars 实现必须有 pandas 黄金对拍。

**现状证据：**
- `backend/app/backtest/factor_zoo.py` 当前只有 `compute_factor()` + `alpha101_001()`。
- `backend/tests/backtest/test_factor_zoo.py` 已有 pandas reference 对拍 `alpha101_001`。
- `backend/pyproject.toml` 中 pandas 注释限定在 backtest 边界；本计划只在测试 reference 中使用 pandas。

## 文件

| 文件 | 动作 |
|---|---|
| `backend/app/backtest/factor_zoo.py` | registry、metadata、10 个因子 |
| `backend/app/backtest/factor.py` | 兼容现有 factor_name 调用 |
| `backend/app/api/backtest.py` | manifest/compare 端点 |
| `backend/tests/backtest/test_factor_zoo.py` | 黄金对拍扩充 |
| `backend/tests/api/test_backtest_factors.py` | API 测试 |

## 任务 0：失败测试清单

- `test_export_manifest_contains_alpha101_001_metadata`：当前无 registry/manifest，应失败；实现后证明旧 alpha 已注册且 metadata 可导出。
- `test_compute_factor_unknown_factor_keeps_legacy_behavior`：先锁住未知 factor 兼容行为，避免 registry 改造破坏现有调用方。
- `test_each_registered_alpha_has_pandas_reference`：先用 pytest 参数化声明 10 个候选，未实现时失败；每补一个 alpha 就有黄金对拍。
- `test_manifest_endpoint_does_not_compute`：API 只返回 metadata，不触发 price panel 读取或计算。
- `test_compare_endpoint_rejects_unknown_factor`：compare 输入必须经过 registry 校验，不能静默吞掉拼写错误。

## 任务 1：registry 先行，保持旧接口兼容

- [ ] 定义 dataclass：

```python
@dataclass(frozen=True)
class AlphaMeta:
    id: str
    name: str
    theme: str
    formula: str
    columns_required: tuple[str, ...]
    warmup: int
    notes: str = ""
```

- [ ] 新增：
  - `ALPHAS: dict[str, tuple[AlphaMeta, Callable[[pl.DataFrame], pl.DataFrame]]]`
  - `list_alphas() -> list[AlphaMeta]`
  - `get_alpha(alpha_id) -> AlphaMeta`
  - `export_manifest() -> list[dict]`
- [ ] 保留 `compute_factor(panel, factor_name)`，内部改为查 registry；未知 factor 仍按现状返回原 panel，避免破坏调用方。
- [ ] `alpha101_001()` 输出列名保持 `alpha101_001`，旧测试不改。

## 任务 2：选择 10 个低风险 Alpha101

选择标准：只依赖 `open/high/low/close/volume/vwap/amount` 中已有列；公式能用 rolling/rank/corr/delta 表达；不需要行业/市值中性化。

- [ ] 候选：
  - `alpha101_001`（已存在）
  - `alpha101_002`（rank delta log volume vs return corr）
  - `alpha101_003`（rank open vs rank volume corr）
  - `alpha101_004`（rank low）
  - `alpha101_006`（open vs volume corr）
  - `alpha101_007`（adv20 与 volume 条件）
  - `alpha101_008`（open return rank）
  - `alpha101_009`（delta close 条件）
  - `alpha101_010`（signed delta close rank）
  - `alpha101_012`（sign(volume delta) * close delta）
- [ ] 若某个候选需要缺失列，先跳过并用下一个低依赖 alpha 替代，不临时造列。

## 任务 3：黄金对拍规则

- [ ] 每个 alpha 添加 pandas reference，放在测试文件，业务代码不 import pandas。
- [ ] fixture：3 个 symbol、60 个日期，构造 open/high/low/close/volume/amount/vwap，含少量 null。
- [ ] 对拍逻辑：
  - join `symbol/date`
  - drop 两边都 null 的 warmup 行
  - `np.allclose(..., atol=1e-9, equal_nan=True)`
- [ ] 对 rank tie 明确 `method="average"`，Polars/pandas 保持一致。

## 任务 4：manifest API

- [ ] `GET /api/backtest/factors/manifest`
- [ ] 返回：

```json
{
  "factors": [
    {"id":"alpha101_001","name":"Alpha101 #001","columns_required":["close"],"warmup":20}
  ]
}
```

- [ ] 不触发任何计算。

## 任务 5：compare API

- [ ] `POST /api/backtest/factors/compare`
- [ ] 输入：`factor_ids/start_date/end_date/universe/symbols`。
- [ ] 输出每个 factor：
  - `coverage`
  - `null_rate`
  - `ic_mean`
  - `ic_ir`
  - `rank_ic_mean`
- [ ] 复用现有 factor backtest 的收益对齐逻辑；不要复制一套收益计算。
- [ ] compare 不写盘；需要研究资产时交给 C2 run_card。

## 任务 6：strict random-control bench

- [ ] 可选参数 `strict=true`。
- [ ] 对每个交易日打乱 factor value，计算随机 IC 分布。
- [ ] 输出：
  - `random_control_ic_mean`
  - `random_control_ic_std`
  - `delta_vs_random`
- [ ] 默认关闭，避免 UI 默认慢。

## 任务 7：验证

```bash
cd backend
uv run --extra dev pytest tests/backtest/test_factor_zoo.py -q
uv run --extra dev pytest tests/api/test_backtest_factors.py -q
```

## 验收标准

- `compute_factor(panel, "alpha101_001")` 旧行为不变。
- `export_manifest()` 至少 10 个 alpha。
- 每个 alpha 有 pandas golden test。
- strict bench 默认不运行。

## 非目标

- 不搬 Vibe 456 因子。
- 不做 AST 安全门禁；Vibe AST 只是 metadata 提取，不证明 compute 无副作用。
- 不新增前端因子商店。
