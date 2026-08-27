# Issue #16 计划 v2

## 原则

- 契约先于实现；能力不完整时绝不降级、代理、插值或使用当前视图替代 PIT。
- 参数固定为 MACD(10,20,7)，端点不接受参数覆盖。
- 服务层纯函数、无 I/O，不改指标管线、存储、provider 或 `data/`。

## 组件

1. `backend/app/services/macd_stages.py`
   - 暴露固定参数与稳定 schema；
   - 返回结构化 `unavailable` 能力声明；
   - 原因至少包含 `state_machine_not_implemented`、`oos_not_implemented`。
2. `backend/app/api/research.py`
   - 新增 `GET /api/research/macd-stages`；
   - HTTP 200 返回能力声明，而非伪造阶段序列。
3. `backend/tests/test_macd_stages.py`
   - 锁定 schema、参数、原因集合、确定性和无序列输出。
4. `docs/ISSUE-16/`
   - 六篇设计文档、README 索引、验证记录。

## 状态机规格（本期只冻结，不实现）

每日按已收盘输入计算 DIF、DEA、HIST；主状态由 DIF-DEA 相对位置和 HIST 绝对值变化组成：`below_shrink`、`below_expand`、`cross_up`、`above_expand`、`above_shrink`、`cross_down`。零轴侧作为独立字段 `zero_side`。状态转移只比较相邻市场日，首日无前一状态时标为 `initial`。

## 字段与可用时点

每行必须能回溯 `raw` 原始字段、`pit` 的 as-of 上下文和 `generation` 代次。状态事件在 D 日收盘后才成立，`available_from` 固定为次一市场日；缺字段不得默认为可用。

## OOS

按日期或 generation 的冻结边界划分 IS/OOS；OOS 只使用边界前已冻结的参数与状态定义，报告分层呈现，不合并两个区间。

## 本期验收

仅交付上述 fail-closed 契约、端点、定向测试和文档。逐日状态机、OOS、PIT 读取器缺失时必须保持 unavailable。
