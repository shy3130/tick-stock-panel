# 道氏监控实时渲染性能验收

## 适用需求

- `REQ-DOW-MONITOR-REALTIME-RENDER-THROTTLE-001`
- `REQ-DOW-MONITOR-REALTIME-RENDER-BATCH-001`

## 故障原因

WebStock 行情持续实时接收，但客户端过去会在每条报价或盘口消息到达时复制整张状态表并通知 React。港股监控约有每秒十余条相关消息，两个终端会各自重复渲染，最终造成页面长时间加载或失去响应。K 线覆盖对象也会在内容未变化时重复创建，进一步放大重绘开销。

## 实现语义

- 每条 WebStock 消息仍立即写入客户端内存，不降低采集频率；
- React 页面状态最多每秒发布一次，多个消息合并为一次界面更新；
- 只在发布边界复制状态表，连接状态变化仍立即通知；
- K 线视觉数据未变化时复用原对象，真实价格变化仍更新末端 K 线。

## 可执行验收

测试驱动证据：

- 修复前新增的两个消息合并用例失败：快照会被立即改变，三条消息会触发三次监听回调；
- 修复后 `frontend/src/lib/realtimeMarketData.test.ts` 共 12 项通过；
- 相关前端回归共 5 个测试文件、82 项测试通过；
- `npm run build` 通过；
- `tests/spec_contracts/test_realtime_frontend_contract.py` 通过；
- 发布包合同及策略进度验证共 27 项通过。

## 生产验收

- 前端提交：`2a7ab16ce78f638b72e3421e857171671ebc315b`；
- 发布提交：`25fcdb614913e38b8bc9367a67cc0b973725a6af`；
- 镜像：`tickflow-stock-panel-app:dow-monitor-25fcdb614913`；
- 入口：`assets/index-IK01CFDS.js`；
- 实时模块：`assets/realtimeMarketData-gAzkjnAD.js`；
- `/health` 返回版本 `0.1.86`，容器重启次数为 0；
- Chrome 打开港股监控后 `document.readyState=complete`，`01347.HK` 卡片唯一存在，控制台无错误或警告；
- 页面显示 2026-07-29 当日行情、分钟和分析时间，并展示新版“今日综合决策、建议动作、平均成交成本、相对成本偏离”文案。

结论：生产服务没有回退到旧版本；实时采集保持逐条接收，界面改为一秒合并刷新，满足实时性和可用性要求。
