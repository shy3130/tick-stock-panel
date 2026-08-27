# Issue #16 最终设计：MACD(10/20/7) 逐日阶段研究

## 1. 固定计算定义

参数不可覆盖：快线周期 `10`、慢线周期 `20`、信号线周期 `7`。对每个标的、每个市场日 D：

- `ema_fast_D = EMA(close, 10)`；
- `ema_slow_D = EMA(close, 20)`；
- `dif_D = ema_fast_D - ema_slow_D`；
- `dea_D = EMA(dif, 7)`；
- `hist_D = dif_D - dea_D`。

递推只能使用 D 及以前已经冻结的输入；不足预热窗口时不产生阶段。

## 2. 逐日状态机

状态机按市场日顺序运行。每行包含主状态和零轴侧：

| 主状态 | 判定（与前一日比较） |
|---|---|
| `below_shrink` | `dif < dea` 且 `abs(hist)` 较前日收窄 |
| `below_expand` | `dif < dea` 且 `abs(hist)` 较前日扩大 |
| `cross_up` | `dif` 从 `<= dea` 变为 `> dea` |
| `above_expand` | `dif > dea` 且 `hist` 较前日扩大 |
| `above_shrink` | `dif > dea` 且 `hist` 较前日收窄 |
| `cross_down` | `dif` 从 `>= dea` 变为 `< dea` |

`zero_side` 为 `negative`、`positive` 或 `zero`，由 D 日 DIF 符号决定；首个可判定日的主状态为 `initial`。相等、缺值或无法确认前一日时不猜测，返回缺失状态。

## 3. PIT、generation 与 raw

未来实现的每行必须同时携带：

```json
{
  "market_date": "YYYY-MM-DD",
  "symbol": "600000.SH",
  "state": "cross_up",
  "zero_side": "positive",
  "available_from": "YYYY-MM-DD",
  "raw": {
    "snapshot_ref": "...",
    "close": 0.0,
    "source_fields": ["close", "raw_close"]
  },
  "pit": {
    "as_of": "YYYY-MM-DDT23:59:59Z",
    "generation": "published-generation-id"
  },
  "generation": "published-generation-id",
  "macd": {
    "ema_fast": 0.0,
    "ema_slow": 0.0,
    "dif": 0.0,
    "dea": 0.0,
    "hist": 0.0
  }
}
```

- `raw` 是原始快照字段及其引用，不得由派生值倒填；
- `pit.as_of` 是读取边界，`generation` 是该边界下发布的构建代次；
- 顶层 `generation` 必须与 `pit.generation` 一致；不一致即拒绝输出；
- 当前 PIT 读取器不可用时禁止回退到当前视图。

## 4. T+1

D 日阶段只在 D 日收盘后成立，`available_from` 必须指向次一市场日。服务端负责表达该延迟；调用方不能把 D 日事件当作 D 日已知输入。若无法解析下一市场日，整行不可用。

## 5. OOS 协议

- 用冻结的日期或 generation 边界切分 IS 与 OOS；
- 参数、状态规则和字段口径在边界前冻结；
- OOS 只评估边界后的行，报告必须分别给出 IS/OOS 覆盖和指标；
- 禁止把两个区间合并成单一成绩；
- 当前没有阶段研究专用 OOS 执行器，因此本端点恒不可用。

## 6. 当前 API 契约（fail-closed）

`GET /api/research/macd-stages` 返回 HTTP 200，载荷为能力声明：

```json
{
  "schema": "tickflow.research.macd-stages.v1",
  "status": "unavailable",
  "params": {"fast": 10, "slow": 20, "signal": 7},
  "reasons": ["state_machine_not_implemented", "oos_not_implemented"],
  "missing_capabilities": {
    "daily_state_machine": true,
    "oos_evaluation": true,
    "pit_reader": true
  },
  "contract_preview": {
    "required_fields": ["raw", "pit", "generation", "available_from"],
    "state_values": ["initial", "below_shrink", "below_expand", "cross_up", "above_expand", "above_shrink", "cross_down"]
  }
}
```

即使 PIT 与行情能力恢复，状态机或 OOS 任一未实现仍必须返回 `unavailable`；当前阶段不返回 `rows`、`series` 或任何阶段数值。

## 7. 能力矩阵

| 能力 | 当前状态 | 是否阻断 |
|---|---|---|
| 固定参数契约 | 已定义 | 否 |
| 10/20/7 数值计算 | 未接入研究服务 | 是 |
| 逐日状态机 | 未实现 | 是 |
| PIT/generation 读取 | 当前不可用 | 是 |
| T+1 服务端表达 | 已定义，未有数据行 | 随实现 |
| OOS 评估 | 未实现 | 是 |

## 8. 演进边界

后续实现必须先补齐 PIT 读取器，再实现纯状态机和 OOS 分层；每一步都保留本契约的 schema、固定参数和 fail-closed 语义。不得为获得部分结果而静默降级。
