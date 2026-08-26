# 可行性评估

## 结论

**可行（仅日线研究实现）**。当前 canonical sealed enriched 历史已具备 OHLCV、raw OHLC、成交额、换手率、涨停信号和上市参考数据，足以实现首板识别、低位、回调、缩量、结构保持与二次启动的确定性事件扫描。该能力不能直接证明可执行成交，也不能在样本外验证前进入默认候选池。

## 已验证的集成点

- 历史入口：`KlineRepository.get_enriched_range(start, end, symbols, columns)`；不扫描 TDX 原始 CSV，不连接外部行情。
- 数据字段：已发布 canonical history manifest 覆盖 `open/high/low/close/volume/amount/raw_close/raw_high/raw_low/turnover_rate/consecutive_limit_ups`。
- 现有模式：`screener_query._load_enriched_history` 支持严格窗口不足返回 NULL；`strategy/builtin/broken_board_recovery.py` 有两阶段历史 setup 范式。
- 评估组件：`backtest.robustness` 的有限 walk-forward、`backtest.metrics` 的置信区间、`backtest.fill_reachability` 的分钟可达性诊断、`backtest.provenance` 的数据快照描述。
- 现有短线池：`short_pool.run_short_pool` 的 preset 条件、排序、内容寻址和 Agent 只解释契约保持不变。

## 事实限制

1. 当前没有可直接复用的“首板→回调→二次启动”参数化事件状态机；固定 `SEQUENCE_FIELDS` 不能替代事件上下文，因此需要独立研究服务，不扩大普通 screener 谓词面。
2. 板块涨停制度、除权事件锚点和一字板可达性必须由服务显式识别；无法证明时返回删失或 unavailable，不把日线命中当成交事实。
3. 题材/人气核心没有可靠 PIT 数据；本 Issue 不实现人气过滤，也不事后打标签。
4. 现有分钟可达性工具是诊断，不足以把日线事件研究伪装成分钟执行回测。
5. 当前发布 manifest 的最新端点是 2026-08-17；研究输入必须携带实际读取到的 generation，超出水位 fail-closed。

## 风险等级

中等。数据链路与日线计算可复用，主要风险集中在事件确认时点、涨停制度/一字板、公司行动、重叠事件和前向收益删失口径。

## 来源核验

本地 Obsidian 整理稿明确将“翻倍”表述标为未经验证。Douyin 页面经 reader 仅返回通用页面错误/推荐内容，未取得可依赖的字幕或规则证据。因此实施只采用 `docs/TODO.md` 与本地整理稿中的待冻结研究定义。
