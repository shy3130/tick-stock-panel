# 道氏趋势监控列表与 WebSocket 独立复核

状态：独立复核通过（本地实现与语义），生产发布证据待补

复核范围：

- `REQ-DOW-MONITOR-LIST-LAYOUT-001`
- `REQ-DOW-MONITOR-LIST-INDICATORS-001`
- `REQ-DOW-MONITOR-LIST-REALTIME-001`
- `REQ-DOW-MONITOR-LIST-SIGNAL-STABILITY-001`
- `REQ-DOW-MONITOR-INLINE-DETAIL-001`

复核要求：

1. 从每条需求反向定位实现与可执行测试。
2. 单独验证完成分钟决策与实时行情覆盖的层次边界。
3. 不以快照、构建成功或页面存在替代指标语义验收。
4. 记录遗漏、冲突与剩余风险。

## 需求到证据复核

### REQ-DOW-MONITOR-LIST-LAYOUT-001

- 实现：`DowMonitor.tsx`、`DowMonitorList.tsx`、`paginateMonitorSymbols`。
- 证据：页面集成测试证明三个互斥市场、固定 20 行、翻页和筛选归一；列表组件测试证明
  十个字段和固定“查看详情”。
- 结论：通过。

### REQ-DOW-MONITOR-LIST-INDICATORS-001

- 实现：`monitorListPresentation.ts`。
- 证据：纯函数测试以手工字面量验证完成 K 线、均线排列、控制线回退、动量、量比、
  主动资金质量、正式通知和 warning/failed 边界。
- 结论：通过；页面没有重新生成正式交易建议。

### REQ-DOW-MONITOR-LIST-REALTIME-001

- 实现：`DowMonitor.tsx` 仅把当前页启用代码传给既有 `useRealtimeMarketData`；
  `DowMonitorSparkline.tsx` 只渲染一个 polyline。
- 证据：页面测试验证第 1/2 页订阅集合；实时客户端 12 项测试验证批量发布；
  浏览器 DOM 验证单折线与无背景。
- 结论：通过。

### REQ-DOW-MONITOR-LIST-SIGNAL-STABILITY-001

- 实现：`deriveMonitorRow` 将 WebSocket 限制在价格、涨跌和 sparkline；决策字段只读
  HTTP 完成分钟状态和通知；延迟只抑制新的 warning，不删除正式通知。
- 证据：纯函数测试验证形成中 K 线被排除、失败突破被排除、延迟时 warning 为空、
  正式信号和时间仍保留；页面测试验证实时价格变化时正式信号不变。
- 结论：通过。

### REQ-DOW-MONITOR-INLINE-DETAIL-001

- 实现：`DowMonitorDetailPanel.tsx` 与页面选中状态。
- 证据：组件测试验证不是 dialog 且保留周期/叠加层控制；页面测试和浏览器快照验证
  详情位于列表下方。
- 结论：通过。

## 反向遗漏检查

- 每条权威需求均有实现路径、可执行契约测试、语义验收和独立复核路径。
- 底层 `/ws/realtime` 网关先由 5 项后端测试验收，再验收页面订阅和展示，没有用页面快照
  替代网关语义。
- 没有把构建成功、黄金图或截图当作信号算法正确性的证明。
- 未改变后端信号生成、通知持久化、3018/19912 端口职责或生产容器。

## 生产发布独立复核

- 发布镜像只在上一生产镜像之上替换 `/app/static`，没有替换后端、信号逻辑或数据卷。
- 发布前保存原始股票文件、API 快照、偏好文件、容器 inspect 和回滚镜像标签。
- 发布前后 13 只股票的 API 响应逐字节一致；原始数据文件 SHA-256 也一致。
- 生产镜像 revision、入口、列表分包和实时分包哈希与本地验收构建一致。
- 生产 WebSocket 使用正式 Origin 完成 `hello/v1` 和当前港股页 5 只股票的三数据集订阅。
- 生产容器健康、重启次数 0、单一 3018 监听且发布日志无错误。
- 密码认证阻止了无凭证的生产 DOM 自动化；复核没有读取密码或绕过登录。页面结构的
  生产证据由相同静态包哈希、组件/页面行为测试和发布前浏览器验收共同构成。

复核结论：五条需求的实现、持久化股票迁移、生产静态包与 WebSocket 接入通过。
盘中持续推送以及完成分钟内决策字段稳定性仍属于交易时段观察项，不由单次快照替代。
