# 道氏监控实时 K 线渲染性能验收

## 问题复现

生产 Chrome 同时运行多个美股道氏监控标签后，正式页面无法完成截图、控制台读取或 DOM 读取。后端监控状态和行情采集正常，说明故障位于浏览器渲染层。

代码追踪确认：实时客户端的任意报价或盘口更新都会生成新的股票状态；卡片随后无条件重建 K 线覆盖对象，使所有迷你 K 线重新生成 ECharts 配置并执行全量 `setOption`。

## 可执行验收

失败用例 `keeps the Dow chart reference when only quote or depth changes` 在修复前明确失败，返回内容虽完全相同，但对象引用不同。

修复后执行：

```text
npm test -- --run src/lib/realtimeOverlays.test.ts
                  src/lib/realtimeMarketData.test.ts
                  src/components/dow-monitor
                  src/pages/DowMonitor.test.tsx
```

结果：6 个测试文件、85 项测试全部通过。

生产构建命令 `npm run build` 成功，生成新的道氏监控分包。

## 语义验收

- 相同 K 线覆盖结果直接返回原图表对象及原 K 线数组；
- 实时最高价、最低价或收盘价变化时仍生成新的末端 K 线；
- 报价和盘口继续实时进入卡片状态，但不再触发无变化 K 线的全量重绘；
- Chrome 生产页面验证结果在部署后补充。
