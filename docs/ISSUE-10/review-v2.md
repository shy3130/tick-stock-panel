# 方案 v2 Review

二审发现四项定稿修订：

- 5m 证据必须截止到 15m 信号 bar close，禁止使用信号后的 bar。
- minute_index 的零点、bar 端点、午休重置和 5m/15m 桶锚点必须冻结并随 run 输出。
- 1/2 根方向必须分别定义 return 公式、raw_close 端点与 flat 类别；缺 bar 为 censored。
- 删除与独立方向因子无关的“止损后修复”测试。

上述内容已写入 `plan-v2.md`，因当前生产 reader 不存在，编码只能交付稳定的 unavailable 门禁，不能伪造分钟研究结果。
