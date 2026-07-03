# Vibe-Trading 功能移植评估

- **日期**：2026-07-02
- **对象**：`../Vibe-Trading`（HKUDS，LangGraph agent 交易平台，10.2 万行 Python/纯 pandas/424 commits）
- **方法**：sonnet 子代理全量扫描（README/wiki/agent src/backtest/tests/TDX 文档）+ 主代理按 panel 架构与红线做适配判断
- **panel 基线**：选股+监控+回测工作台；Polars 流水线；data_providers 抽象层（含刚落地的 fquant_local 磁盘直读）；ext_data 扩展数据体系；无交易执行、无 agent 会话

## 一、移植评估矩阵

### ✅ 推荐移植（高价值 · 低/中耦合）

| # | 能力 | 来源 | 移植方式 | 价值/成本 |
|---|------|------|----------|-----------|
| P1 | **A股数据类工具 8 件套**：资金流/龙虎榜/北向/两融/大宗交易/股东户数/解禁/研报+一致预期EPS | `agent/src/tools/*_tool.py`（HTTP+解析，零框架耦合） | **接入 panel 现有 `ext_data`/`ext_presets` 体系**作为内置预设源（已有 THS 概念/行业先例，走同一抽象，符合红线 #2 修订精神：受控适配器）。产出直接喂：连板梯队（龙虎榜佐证）、市场复盘（北向/两融/情绪素材）、AI 四维分析（消息面输入） | 高/低——I/O 层与 DataFrame 无关，逐源搬运解析逻辑即可 |
| P2 | **Alpha Zoo 456 因子**（qlib158+alpha101+gtja191+academic） | `agent/src/factors/zoo/`（`@register` 纯函数 + AST 门禁 + lookahead 哨兵测试） | 移植 **registry 模式 + 分批 Polars 化**：先 alpha101（最经典、算子最简），pandas→Polars 翻译 + 黄金对拍（与我们 DSL 黄金测试方法论同源：同一 fixture 面板双实现比对）。接入 panel 现有 `backtest/factor.py` 因子回测 | 高/中——456 个全量翻译工作量大，分批推进；其 AST 纯函数门禁 + lookahead 哨兵设计**连同测试思路一起抄** |
| P3 | **组合优化器 4 种**（风险平价/均值方差/最大分散化/等波动率） | `agent/backtest/optimizers/*.py`（独立纯函数：协方差+收益→权重） | 改写为 NumPy 落入 panel 组合回测的 `position_sizing`（现仅 equal/score_weight 两种） | 中/低——4 个小文件，直接增强回测真实感 |
| P4 | **TDX 数据质量经验清单** | `TDX_LOCAL_DATA_INTEGRATION.md` | 不是代码移植，是**核对清单**落入我们的磁盘直读：① 量纲核对（TDX volume=股/amount=元 vs tushare 手/千元——核对 `mapping.py` 与 enriched 换手率公式的单位假设）② 早期"对数复权负价"识别（我们用 raw 重建**优于**它的直接丢弃，但可加数据质量断言）③ 港股 amount=0 边界 | 中/极低——半天核对 |

### 🔶 借鉴设计（概念 > 代码，按 panel 路线图时机启动）

| # | 能力 | 借鉴点 | 时机 |
|---|------|--------|------|
| B1 | **Shadow Account**（从券商成交记录反推行为规则→影子回测对比） | 概念完整可复刻：panel 用户导入同花顺/东财成交 CSV（其 `trade_journal_parsers.py` 解析逻辑可搬）→ 用 **panel 自己的 Polars 回测引擎**做"我的实际操作 vs 策略基准"对比报告。models 契约层（frozen dataclass）+ extractor/scanner 分层设计值得照抄；codegen/backtester 用 panel 引擎替代 | **建议作为 panel 下一个独立 feature 立项**——它是散户工具最有共鸣的功能（"我到底输在哪"），且不依赖交易执行 |
| B2 | **Mandate Gate / Kill Switch / 审计**（LLM 有界自治安全层） | fail-closed 文件哨兵熔断（`live/HALT` 存在即停）、frozen dataclass 硬上限合约（资金/单笔/敞口/品种白名单/日次数）、consent 溯源 | panel 路线图 v2 做 QMT/掘金下单时**必抄的设计模式**；代码本身绑定 Robinhood/美股，不搬 |
| B3 | **MCP Server**（把能力暴露给 Claude/Cursor 等客户端） | `fastmcp` 薄封装模式（`mcp_server.py` 的 tool 注册结构）——把 panel 的选股/回测/行情/连板梯队暴露为 MCP tools，与 panel "LLM 驱动"定位契合 | 可选增强，一层新增不动存量；有明确需求再做 |

### ✅ 追加推荐（2026-07-02 grill 修正，前提变更：fquant 项目弃维护、panel 成为唯一主维护项目）

| # | 能力 | 决策 | 说明 |
|---|------|------|------|
| P5 | **告警多通道推送**：钉钉 + 企微 webhook + **MeoW** | 新增小项 | 与飞书 `webhook_adapter` 同构（钉钉/企微=HTTP webhook+可选签名；MeoW=`api.chuckfang.com/{昵称}/{标题}/{消息}`，GET/POST、无鉴权、支持 Markdown）。每通道 ~50-80 行，照 panel 自有模式写，**不搬 Vibe-Trading 代码**。微信个人号（wechatferry 类灰色方案）不碰 |
| P6 | **港股日 K 接入** | 新增 | 磁盘已有数据（`day/hk00~hk86`、hk 分钟线、`hk-daily.sh` 更新脚本），给 `fquant_local` 加 HK capability（路径规则 4 字符分桶 + symbol 归一扩展，借 Vibe-Trading TDX loader 的 hk 解析细节）——data_providers 抽象层的自然延伸 |
| P7 | **panel 自建薄 agent 会话循环 + MCP Server（B3 升级为推荐）** | 方向确认 | fquant DSA 弃维护后会话形态承担者空缺。**不搬 LangGraph**：复用 panel `ai_provider` 自建 tool-call 循环，工具=现成 service 层（选股/回测/梯队/价位/财务）包 tool schema；同一套 tool 定义复用暴露为 MCP。落地后 **79 Skills 中 A 股相关的方法论文档改判可搬**（纯 Markdown，成本≈复制文件） |

> 附注：fquant 弃维护**不影响** panel 数据链——D3 决策特意直连上游（fstore PG/磁盘/tdx-api），不经 fquant 服务。需确认磁盘每日更新脚本、fstore、tdx-api 三个基础设施继续维护（不属 fquant 代码库）。

### ❌ 不移植（grill 后维持）

| 能力 | 理由 |
|------|------|
| LangGraph/LangChain agent 框架 + Swarm | 薄循环替代（P7）；Swarm 是会话深水区，薄循环用起来后再议，YAGNI |
| 7 回测引擎 + 18 数据 loader **框架** | panel A 股专精更深；搬 loader 会打散刚收口的"数据一律走 provider 抽象层"红线。港股需求走 P6；多资产回测（crypto/期货）是产品定位级变更，届时借鉴其 `_market_hooks.py` 模式另行设计 |
| IM **双向会话通道**运行时（pairing/bus/语音） | 与单向告警推送（P5）是两回事；等 P7 薄循环落地且确有"IM 里对话 panel"需求再议，届时参考 `channels/base.py` 抽象 |
| 券商连接器 10 个 | 美股/加密为主；panel A 股执行应走 QMT/掘金（路线图 v2），届时另行设计 |
| 期权链/定价 | A 股期权需求窄，YAGNI |
| 技术形态类 skill（缠论/艾略特/SMC…） | 无计算代码；若 P7 落地可随 Skills 批次按需挑选 |

## 二、关键事实（影响判断的）

1. **全线 pandas**：任何计算逻辑移植都要改写 Polars/NumPy，不能复制粘贴；I/O 解析层可直搬。严禁连带引入 langchain/langgraph/fastmcp 之外的 agent 依赖。
2. **它的"技术分析/情绪/财报解读"大多是 Markdown skill**（LLM 阅读推理），不是计算引擎——panel 的 Polars 指标流水线在这个维度上碾压，反向移植无意义。
3. **它没有监控规则引擎、没有连板梯队、没有关键价位、chips 筹码未接入**——panel 的差异化优势清晰，移植应聚焦"数据面拓宽（P1）+ 因子库（P2）+ 组合仓位（P3）"三个互补方向。

## 三、建议落地顺序（grill 修正后）

1. **P4**（半天）：TDX 量纲/边界核对——对刚上线的磁盘直读做数据质量加固。
2. **P5**（1 天）：钉钉/企微/MeoW 告警通道——最小成本、每天都用得上。
3. **P1**（按源逐个）：8 件套数据工具接 ext_presets——随做随用。
4. **P6**（1-2 天）：港股日 K 接入 fquant_local。
5. **P3**（1-2 天）：组合优化器 4 种进回测 position_sizing。
6. **P7**（独立立项）：薄 agent 会话循环 + MCP，落地后跟进 Skills 挑选批次。
7. **P2**（分批长线）：alpha101 先行的因子库 Polars 化 + 黄金对拍。
8. **B1**（独立立项）：Shadow Account 概念复刻（panel 化设计，先出 spec）。
