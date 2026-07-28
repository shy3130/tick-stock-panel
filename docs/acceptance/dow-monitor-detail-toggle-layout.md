# 道式监控放大图开关布局验收

## 验收范围

- 需求：`REQ-DOW-MONITOR-DETAIL-TOGGLE-LAYOUT-001`
- 页面：道式监控完整 K 线弹窗
- 控件：量比、趋势线、头肩形态

## 自动化证据

- 修复前新增测试失败：趋势线和头肩形态开关圆点缺少 `left-0`。
- 修复后 `DowMonitorDetailDialog.test.tsx` 共 17 项通过。
- 测试同时确认趋势线和头肩形态开关保持默认开启，圆点以轨道左边界为定位原点。
- `pnpm build` 通过，生成入口 `assets/index-gOU3hgaX.js` 和道式监控分块 `assets/DowMonitor-CzhuTN1T.js`。

## 语义验收

- 只修正圆点定位，没有改变开关状态、点击行为、图层数据或响应式换行规则。
- 三个同类开关现在使用一致的轨道内定位方式。

