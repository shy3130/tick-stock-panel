# 生产方案一审

结论：**不通过，必须修订后再实施。**

一审确认八项 P1：

1. 六列/七列 CSV 表头不能按年份判断；2026-07 仍存在合法六列表，必须按实际 header 精确选择 parser。
2. raw `09:30` 是分钟起始标签，必须规范化为 1m close `09:31`；否则 5m/15m 会提前一分钟并破坏确认时点。
3. complete day 不能只表示文件存在；每个 symbol/day 必须精确得到上午、下午各 120 根连续 1m close bar。
4. size/hash 校验与解析必须基于同一个拒绝 symlink 的已打开文件描述符，避免校验后路径被替换。
5. OOS 边界不能继续按请求窗口中点漂移，必须成为请求/运行 provenance 中预先固定的输入。
6. 无条件、动量、SMA5 基线必须冻结 IS 拟合、预测、flat/缺值与同样本比较规则。
7. production reader 不能由 service 直接读 env/root；必须经 active provider capability/factory。
8. 多标的同时间标签同样重叠；用于样本量、Wilson CI 与 verdict 的集合必须全局 purge 或按时间簇处理。

上述问题全部进入 v2；v1 不作为实现依据。