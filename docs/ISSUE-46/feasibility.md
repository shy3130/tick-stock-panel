# Issue #46 可行性

## 范围
本实现是可审计的**日频检索路由代理**，不是论文 MERA 的分钟级 Transformer/MoE/FastMoE 复现。输入来自 generation-pinned sealed canonical，特征复用 `app.backtest.factor_zoo` 的日频 Alpha 定义；核心 evaluator 只接受显式 `PinnedFactorPanel`，不访问文件和网络。

## 可行性结论
可以实现稳定契约：时序 60/20/20 切分、train-only 标准化和 forward-return 分位标签、train-only 检索库、PIT 邻居约束、冻结 K/距离选择、可序列化审计事件。分钟 embedding、论文分钟收益基线、外部行情均明确不在范围内。

## 主要威胁
时间泄漏（邻居、标签、标准化、模型选择）、尾部 forward-return 缺失、因子 warmup、样本不足、退市/生存者偏差、距离度量退化和安慰剂异常增益。门禁和显式 unavailable 覆盖这些情形；任何邻居边界违规均 fail-closed。

## 验证边界
production panel builder 已用 pinned reader fixture 验证 identity、PIT universe、forward-tail 删失和 API 接线；成功路由与 placebo 阻断由确定性 panel 验证。本次未用真实 sealed generation 生成市场有效性 verdict，因此不在文档中伪造 accepted/rejected 结论。
