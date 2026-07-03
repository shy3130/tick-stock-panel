# P4：TDX 磁盘数据质量核对实现计划

> **面向 AI 代理的工作者：** 使用 `superpowers:executing-plans`。这是数据质量护栏，不是清洗工程；先写断言，发现异常先记录。

**目标：** 给 `fquant_local` 磁盘直读链路补最小质量回归：日线量纲、raw 重建污染、港股 amount=0、分钟/逐笔基础完整性。

**现状证据：**
- `EngineDataDiskClient` 读取 `wide/day/xdxr/minutes/trans/fund`：`backend/app/data_providers/fquant/engine_data_disk.py`
- `get_wide()` 优先 `wide`，缺失降级 `day`：`engine_data_disk.py:61-68`
- `get_minutes()` / `get_trans()` 已有磁盘方法：`engine_data_disk.py:111-143`
- provider raw 重建样本此前已验证 `600519.SH 2012-10-26 close=241.0`
- 已提交 `f4bb9a4 test(data): add tdx disk quality guards`；复核执行 `tests/data_providers/test_engine_data_disk.py tests/data_providers/test_engine_data_disk_quality.py tests/data_providers/test_provider_raw_chain.py -q`，结果 `21 passed, 1 skipped`。

**范围：** 只加测试/抽样脚本/文档记录。不改 provider 算法，不写 `data/`，不重建 parquet。

## 文件

| 文件 | 动作 |
|---|---|
| `backend/tests/data_providers/test_engine_data_disk_quality.py` | 创建，放可离线小 fixture + 真盘 smoke 可跳过 |
| `backend/scripts/spike_tdx_quality.py` | 创建，人工抽样输出 JSON/表格 |
| `docs/data-query-inventory-local-source.md` | 记录核对结论和剩余缺口 |

## 任务 0：失败测试清单

先写这些测试再实现 fixture/脚本，确保计划不是只做人工观察：

- `test_disk_daily_units_and_wide_fallback`：已实现，证明 wide 缺失会降级 day 且基本量纲合法。
- `test_hk_zero_amount_does_not_crash`：已实现，锁住“不补假成交额、不产生 NaN/inf”。
- `test_missing_minutes_and_trans_return_empty`：已实现，保证缺文件返回空 list。
- `test_raw_reconstruct_maotai_20121026`：已实现，有真盘时验证 `600519.SH 2012-10-26 close≈241.0`，无挂载时 skip。

## 任务 1：离线 fixture 锁住路径和量纲

- [x] 创建临时 TDX 目录结构：
  - `wide/sh600/sh600519.csv`
  - `day/sz000/sz000001.csv`
  - `wide/hk0257/hk02577.csv`
  - `minutes/2026/20260701/sh600519.csv`
  - `trans/2026/20260701/sh600519.csv`
- [x] 用 `monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))` 初始化 `EngineDataDiskClient()`。
- [x] 测试 `_tdx_name()` 港股 5 位补零路径：`02577.HK -> hk02577`。
- [x] 测试 `get_wide("600519")` 缺 wide 时会降级 day，返回 date 为字符串。
- [x] 测试日线数值：
  - `close > 0`
  - `volume >= 0`
  - `amount >= 0`
  - `amount / max(volume, 1)` 在股票样本上落在合理数量级，不要求港股。

**建议测试骨架：**

```python
def test_disk_daily_units_and_wide_fallback(tmp_path, monkeypatch):
    # 写 day/sz000/sz000001.csv，不写 wide，对 get_wide 触发 fallback
    monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))
    rows = EngineDataDiskClient().get_wide("000001", limit=10)
    assert rows and rows[0]["date"] == "2026-07-01"
    assert rows[0]["close"] > 0
    assert rows[0]["volume"] >= 0
```

## 任务 2：raw 重建污染真盘 smoke

- [x] 新增 pytest 环境跳过：`TDX_DATA_DIR` 不存在时 `pytest.skip`。
- [x] 用 `FQuantProvider(engine_mode="disk").get_daily(["600519.SH"], start=2012-10-26, end=2012-10-26, asset_type="stock")`。
- [x] 断言 close 接近 `241.0`，且不是 `*.075769` 这类前复权尾巴。
- [x] 再抽样固定 A 股做“close 有限、>0、非异常小数尾巴”检查，避免随机 flaky。

**不要做：** 不扫描全市场；这会慢且容易被数据挂载状态污染。

## 任务 3：港股 amount=0 边界

- [x] 用离线 fixture 写 `wide/hk0257/hk02577.csv`，`amount=0`。
- [x] `get_wide("02577", asset_type="hk")` 不抛异常。
- [x] 若走 provider `get_daily(["02577.HK"], asset_type="hk")`，断言返回空或合法 DataFrame，不出现 NaN/inf。
- [x] 文档记录：港股 amount=0 不补假成交额，依赖 amount 的诊断降级。

## 任务 4：分钟/逐笔基础完整性

- [x] `get_minutes("600519", "20260701")` fixture 返回字段只含 `price/volume`，价格 > 0。
- [x] `get_trans("600519", "20260701")` fixture 返回 `time/price/volume/amount/order_count/direction`。
- [x] 缺文件返回空 list，不抛异常。

## 任务 5：人工抽样脚本

`backend/scripts/spike_tdx_quality.py` 已落地，做只读输出：

- 固定 symbols：`600519.SH,000001.SZ,300059.SZ,688981.SH,513050.SH,02577.HK`
- 输出每个 symbol：wide/day 命中、最新日期、close、volume、amount、amount/volume、minutes 行数、trans 行数。
- 输出 JSON 或 Markdown 表，不写 data。

## 任务 6：验证

```bash
cd backend
uv run --extra dev pytest tests/data_providers/test_engine_data_disk_quality.py -q
TDX_DATA_DIR=/Volumes/vol3/tdx uv run python scripts/spike_tdx_quality.py
```

## 验收标准

- 无 TDX 挂载时离线测试可过，真盘 smoke 自动 skip。
- 有 TDX 挂载时 `600519.SH 2012-10-26 close=241.0` 断言通过。
- 文档记录港股 amount=0 和全市场不扫描的决策。
