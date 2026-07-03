# 0006 — 交易桥接必须先过 Mandate Gate 与 Kill Switch

- **状态**：已接受（2026-07-03）
- **相关**：Vibe 迁移候选 C10、未来 QMT/掘金/Ptrade 桥接、C11 公式导出

## 背景

panel 当前定位是选股、监控、回测与交易复盘，不直连券商、不内置实盘下单。路线图只允许把交易桥接作为远期独立立项。

调研 `../Vibe-Trading` 的 `agent/src/live/mandate`、`halt.py`、`order_guard.py` 与 `trading/profiles.py` 后，能借鉴的是安全模式，不是券商连接器本身：
- 用户授权的 mandate 合约：单笔、总敞口、杠杆、每日次数、品种白名单。
- fail-closed 下单守卫：任何配置缺失、授权过期、哨兵触发、审计落盘失败都拒绝。
- 文件哨兵 kill switch：本地文件存在即立即熔断。
- 审计记录：所有拦截、放行、人工确认与执行结果都必须可追溯。

风险：一旦未来接 QMT/掘金/Ptrade 或任何“信号文件→交易终端”的桥接，如果先实现下单再补风控，会把资金安全依赖在 UI 自律或调用方自律上。

## 决策

任何交易桥接实现之前，必须先落地 **Mandate Gate + Kill Switch**，并以 fail-closed 作为默认语义。

### Mandate 合约

mandate 必须是不可变配置快照，至少包含：
- `mandate_id`、`version`、`created_at`、`expires_at`
- `mode`: `paper` / `signal_file` / `live`
- `allowed_symbols` 或 `allowed_universes`
- `max_single_order_value`
- `max_position_value`
- `max_total_exposure`
- `max_daily_orders`
- `max_daily_turnover`
- `max_leverage`
- `allow_short`: 默认 `false`
- `requires_manual_confirm`: `live` 默认 `true`
- `consent_ref`: 用户确认来源，不存敏感券商凭证

任何缺失关键字段、过期、版本不匹配、或无法解析的 mandate 都视为无效。

### Kill Switch

必须支持两层熔断：
- 文件哨兵：`{DATA_DIR}/live/HALT` 存在即停；内容可写入原因，空文件也有效。
- 运行态熔断：进程内 `halted=true`，用于 API 一键停机；重启后仍以文件哨兵为准。

检查顺序固定：
1. `HALT` 文件存在 → 拒绝。
2. 运行态 halted → 拒绝。
3. mandate 不存在/过期/无效 → 拒绝。
4. connector profile 不在允许模式 → 拒绝。
5. order intent 超过 mandate 上限 → 拒绝。
6. 审计预写失败 → 拒绝。
7. 执行或导出。
8. 审计补写执行结果；补写失败必须触发运行态 halted。

### 守卫接口

未来代码必须把所有交易桥接统一收口到一个守卫函数，不允许各 connector 自行判断：

```python
guard_order(intent: OrderIntent, mandate: Mandate, context: GuardContext) -> GuardDecision
```

`OrderIntent` 是中性意图，不含券商 SDK 对象：
- `symbol`
- `side`: `buy` / `sell`
- `quantity`
- `price_limit`
- `estimated_value`
- `strategy_id`
- `source`: `manual` / `monitor` / `backtest_export` / `agent`
- `client_order_id`

`GuardDecision` 必须显式包含：
- `allowed: bool`
- `reason_code`
- `message`
- `checked_limits`
- `audit_id`

### 审计

审计必须先于执行落盘，目录建议：

```text
data/user_data/trading_audit/YYYY-MM-DD/*.jsonl
```

单条审计至少包含：
- `audit_id`
- `timestamp`
- `mandate_id`
- `intent`
- `decision`
- `reason_code`
- `connector_profile`
- `execution_result`（执行后补写）

审计文件写失败时不得继续交易。

## 非目标

- 不在当前阶段实现 QMT/掘金/Ptrade/券商 SDK。
- 不保存券商账号、密码、token 或交易证书。
- 不做自动下单 agent。
- 不把 C11 公式导出视为交易执行；公式导出仍是只读文本能力。

## 后果

- ✅ 未来交易桥接有明确先决条件：先有守卫，再有 connector。
- ✅ 文件哨兵使“停止交易”不依赖前端、数据库或调度器健康。
- ✅ fail-closed 规则把异常默认导向拒绝，适合资金安全场景。
- ⚠️ 会增加未来交易桥接的前置工作量；但这是必要成本。
- ⚠️ paper/signal_file/live 三种模式必须在 UI 上明确区分，不能用同一个“交易”按钮暗示已进入实盘。

## 验收清单

未来实现 C10 时，至少需要以下测试：
- 无 mandate → 拒绝。
- mandate 过期 → 拒绝。
- `data/live/HALT` 存在 → 拒绝。
- 单笔金额超限 → 拒绝。
- 总敞口超限 → 拒绝。
- 日内次数超限 → 拒绝。
- 审计预写失败 → 拒绝。
- 审计补写失败 → 设置运行态 halted。
- `paper` 模式不能调用 live connector。
- `signal_file` 模式只能写信号文件，不能调用 live connector。
