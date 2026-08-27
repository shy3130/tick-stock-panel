# ISSUE-18 最终设计

日期：2026-08-27 · 版本：`single_yang_no_break_v1`

## 固定定义

输入必须是同一标的、按交易日升序排列的**原始不复权（raw）OHLC**。禁止把前复权
`open/high/low/close` 当作 raw 输入；当前 enriched 虽有 `raw_close/raw_high/raw_low`，
但没有 `raw_open`，所以生产 reader 仍缺失。

- 阳线：`close > open`；`close == open` 是 doji，不是阳线。
- 实体：`body = close - open`，且 `body / open >= 0.02`（固定 2%）。
- 上影线：`upper_shadow = high - max(open, close)`。
- 下影线：`lower_shadow = min(open, close) - low`。
- 形态锚点：阳线自身的 `low`，不使用开盘价替代。
- 后续不破低点：后续 K 线出现 `low < anchor_low` 才算破；`low == anchor_low` 只是触及，不算跌破。
- 观察窗口：形态日 T 之后连续 **5 根交易日**（固定 `window=5`）；窗口未完整时不确认，
  因此最早确认时点为 T+5 收盘。
- 评估起点：确认后的下一可交易日（T+6 或之后），不把 T 或观察窗口内日期作为已确认
  评估日。
- OOS：确认逻辑只能使用规定窗口；正式研究还必须把参数/规则冻结后在独立 OOS
  区间评估。当前没有该协议，不能宣称样本外结果。

## 服务契约

`detect_single_yang` 是无副作用的规格函数，只对显式传入 bars 做确定性判断。
`run_single_yang_research` 不执行检测、不读数据、不返回信号，恒返回：

```json
{
  "status": "unavailable",
  "reasons": ["pit_reader_missing", "state_machine_not_implemented", "oos_not_implemented"],
  "definition": {"id": "single_yang_no_break_v1", "price_basis": "raw_unadjusted", "window": 5},
  "note": "研究状态机与 OOS 协议未实现；即使 reader 补齐也保持 unavailable。"
}
```

API：`GET /api/research/single-yang-no-break`，HTTP 200，载荷状态为 unavailable。
这不是交易建议、订单计划或执行接口。

## 状态机/OOS 开放门

未来要开放结果，必须另行实现并评审：generation-pinned/PIT raw reader（含 raw_open）、
信号从 T+1 开始的窗口状态机、冻结规则后的独立 OOS 数据集与 provenance。任何一个
缺失都继续 unavailable，不能用配置或降级数据绕过。

## 非目标与红线

本 issue 不改 data/；不新增外部 HTTP/DB 接口；不改 provider；不接 `short_pool`；
不接 Agent；不引入交易语义或 trading 域文件。

## 实施状态更新（2026-08-27）

raw reader、状态机与 OOS/成本诊断已经实现；canonical pipeline 已改为在复权前原生持久化 `raw_open`，不是反推值。旧 schema v1 generation 仍按 §4 fail-closed；只有 schema v2 全历史 generation 原子发布后生产端点才会变为 available。

## 持续更新（2026-08-27）

schema v2 首次全量发布后，盘后管道使用 `publish_incremental_from_local` 生成下一 immutable generation：父代文件优先硬链接（失败回退复制），新增日期从通过完整性门禁的本地 enriched 分区复制；只 pin 日历校验必需的 `tdx` 与 `markets` generation，通过 `000001.INDEX` 校验 `(parent_end, through_date]` 交易日连续性。新 manifest 继承父代 `source_generations` 血统，单独记录日历校验 generation 及新增分区行数/标的数/SHA-256。全量 coverage scan 与父代未变化校验通过后才原子切换。增量失败不影响本地 canonical，也不改变上一 published generation。
