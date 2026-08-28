# ISSUE-29 review-v3：对 plan-v3 / final-design 的最终门禁复审记录

> 审查对象：[plan-v3.md](plan-v3.md) 与 [final-design.md](final-design.md)（基线 `7bf2982`）。
> 审查来源：最终独立 coding review（结论经 Main 转达；本文件忠实记录，未做辩护或改写）。
> 日期：2026-08-28 · 对应 Issue：[wf2311/fm-workbench/issues/29](https://github.com/wf2311/fm-workbench/issues/29)

## 总体结论

**verdict：`incorrect`（最终门禁 reject）——仅剩 R8 major，其余全部 resolved。**

R1–R7、R9 已 resolved；R8（响应契约完整度）为 major，未达可进入实现的门禁。R 编号沿用 [review-v1.md](review-v1.md) / [review-v2.md](review-v2.md) 的 finding 序号。

## Finding

### R8（major）evaluate 响应 schema 仍不可直接映射 Pydantic

现有 §7 只给出顶层字段与两个枚举，缺少可直接实现为 Pydantic model / TypedDict 的完整嵌套定义，无法建立可验证的 `response_model`：

1. 未定义 `ArmEnum`（六臂封闭枚举）与 `CensorCode`（事件 censor 封闭枚举，含 nullable 语义）。
2. `CensorEvent` 缺少固定字段集 `code / symbol / entry_id / signal_date / arm / detail` 及各字段可空性。
3. `Segment` 未包含 `censor_code` 的 nullable 字段，无法区分 complete 与 censored 段。
4. 缺 `ArmSegmentResult`、`SummaryStat` / 置信区间（CI）、`Diagnostics`、`Provenance`、`Verdict`、`UnavailableResponse` 的完整嵌套定义与字段类型。
5. 未冻结 `status="unavailable"` 时 `arms / events / segments / censored` 的固定空结构，也未保证 `status="ok"` 时六臂必然齐备。
6. 各 nullable / enum / 字段类型未逐一写清，客户端无法区分事件 censor 与整单 unavailable 的完整取值空间。

## 与本次修订的关系

上述各点已逐条落入 [plan-v3.md](plan-v3.md) §7 与 [final-design.md](final-design.md) 响应契约；追加不变式已修正，方案已获最终 reviewer approve。
 
## R8 追加 finding（major）

最终复核进一步指出，schema 虽已列出字段，却仍未冻结 `EntryEvent` 与 `CensorEvent`
的引用不变式：必须明确 `status="entry_executed"` iff `censor_code=null`；
`status="censored"` 必须非空 `censor_code`，并存在恰好一个同 `entry_id`、`arm=null`、
`code` 相同的 CensorEvent；`detail` 不得为空。该追加项已修正，最终 reviewer approve。
 
## 最终批准

最终 reviewer 已 approve，确认 plan-v3 与 final-design 的 R8 response schema、不变式、空结构与六臂约束均无 blocker/major；方案门禁通过，可进入后续实施波。此结论仅批准文档方案，不代表已有代码实现或任何回测收益结果。
