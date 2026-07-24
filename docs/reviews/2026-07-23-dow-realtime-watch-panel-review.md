# 道氏实时监控独立需求证据复核

日期：2026-07-23
复核结论：十项需求均有实现、可执行测试和 01347.HK 语义证据；未发现
TickFlow 本地重算道氏锚点/信号、静默实时源回退或新增 systemd 调度任务。

| 需求 | 实现与测试复核 | 独立结论 |
| --- | --- | --- |
| REQ-DOW-WATCH-UI-001 | `DowMonitor.tsx`、卡片/迷你图；`DowMonitor.test.tsx` | 紧凑多卡片和五周期摘要由服务状态驱动。 |
| REQ-DOW-WATCH-DETAIL-001 | `DowMonitorDetailDialog.tsx` 及其测试 | 详情复用现有 K 线控件，不建立第二套指标语义。 |
| REQ-DOW-WATCH-FILTER-001 | 页面与 hook 测试 | 市场/信号筛选只改变视图，不修改启用状态。 |
| REQ-DOW-WATCH-DATA-001 | strict ClickHouse provider/data tests | 01347 strict 1655 唯一分钟、无缺口；未走 HTTP 行情回退。 |
| REQ-DOW-WATCH-MTF-001 | bars/service tests | 五周期统一 source timestamp，均持久化 LIVE。 |
| REQ-DOW-WATCH-SIGNAL-001 | typed Longbridge client/service tests | 真实 CLOSE_LONG 样本的 line/role/anchors 与 Longbridge 完全一致。 |
| REQ-DOW-WATCH-NOTIFY-001 | service/store tests | 首次、维持、清除、再激活和不可变快照均有端到端证据。 |
| REQ-DOW-WATCH-BACKGROUND-001 | app lifecycle/API/health tests | 页面关闭不控制后端循环；健康探针只检查服务状态。 |
| REQ-DOW-WATCH-STALE-001 | data/service tests及生产故障恢复 | SESSION_GAP 阶段保持旧状态并阻断通知；补齐后恢复 LIVE。 |
| REQ-DOW-WATCH-MARKET-001 | session/bar tests | A/HK/US 规则均有测试；本次只做 HK 01347 语义验收。 |

## 下层优先复核

先验证 T-1 完整分钟数据，再验证五周期聚合与通知。最初只有 PostgreSQL
回补而 ClickHouse 未启用写入时，验收明确失败；启用 ClickHouse sink 后，
T-1 达到 331 唯一分钟。随后又发现 `lb_intraday_lines` SQL 分支遗漏
`1347.HK` 别名，测试先 RED，再以提交 `072afde` 修复并达到 115 项相关测试
通过。没有用截图、黄金文件或下游通知替代这些下层语义门槛。

## 身份与持久化复核

真实 30m 样本控制线为 `SUPPORT-MAIN-2`：

- role/side：`MAIN / SUPPORT`
- anchors：`2026-07-08 15:00 @ 183.6`、
  `2026-07-09 13:30 @ 192.3`

这些字段来自 Longbridge 响应，并进入不可变通知快照。受控四项通知测试
共 `4 passed`；生产当前为 WATCH，未伪造激活信号。

## 调度复核

应用只保留既有 `longbridge-api.service`。没有新增 timer、crontab 或
systemd 调度单元。独立复核确认 Chronicle 仅有一个
`tickflow-dow-monitor-health` 事件，已启用并按 10 分钟周期运行；实际
run `jmrxo5s5095` 以 `job_complete/code=0` 完成。crontab、系统级 timer 和
用户级 timer 的重复项均为 0。

## 最终独立复审

最终复审先发现并阻止发布两个当前状态语义问题：

1. 历史回放信号曾可能覆盖当前 `WATCH` 徽标颜色；
2. 健康巡检曾只按工作日和时段判断开市，节假日可能误报 stale。

修复后，卡片先使用当前 snapshot 的 `OPEN_LONG`、`CLOSE_LONG`、`OPEN_SHORT`、
`CLOSE_SHORT` 和 `WATCH`，只有当前 action 缺失时才兼容历史 chart signal。
市场开市状态同时要求当前时刻位于常规会话、持久化 source 与本地日期一致，且
source 本身位于常规会话。对应测试覆盖当前 WATCH 与历史信号冲突、短仓动作与
历史 BUY 冲突、以及工作日节假日没有当日常规 K 线的情况。

最终只读复审结论：

- Critical：0；
- Important：0；
- SPEC：PASS；
- QUALITY：PASS；
- READY：YES。

## 2026-07-24 股票候选搜索独立复核

复核从 `REQ-DOW-WATCH-UI-001` 反向检查：实现只修改趋势监控顶部输入区，
复用既有股票搜索 API，没有改变监控清单接口、WebStock 数据、趋势线或买卖信号。
可执行测试直接验证名称搜索、候选展示、规范代码回填和显式添加的顺序，没有用
截图或快照替代行为验收。生产 DOM 又以真实 `TENCENT` 查询确认候选与回填，
且选择后监控数量未改变。下层监控与道氏语义不在本次变更范围内。

右边界布局复核直接断言输入宽度、候选宽度和 `right-0` 锚点，并拒绝原
`left-0` 锚点；实现差异只有两个布局类，不涉及搜索接口和业务状态。
生产几何测量进一步证明候选框右边缘小于视口宽度，独立复核结论为
`REQ-DOW-WATCH-UI-001` 的搜索可见性要求通过。

## 2026-07-24 价格主文字色修复复核

独立复核确认问题来自 Tailwind 工具类命名冲突，而不是 WebStock 行情缺失或卡片
背景色错误。`text-base` 在本项目中同时命中 16px 字号和主题 `base` 颜色；
暗色主题的 `--base` 正是接近黑色的页面背景色。

修复差异只包含：

1. 价格类名改为 `text-[16px] text-foreground`；
2. 新增回归测试，要求价格使用主文字色且禁止重新引入 `text-base`。

生产 DOM 同时给出实际价格、前景色和背景色证据，未依赖截图作为唯一证明。
后端、WebStock、K 线、趋势线和买卖点文件均未修改。最终复核：

- Critical：0；
- Important：0；
- SPEC：PASS；
- QUALITY：PASS；
- READY：YES。

复审提出的唯一 Minor 为健康脚本测试导入顺序，已在 `f7ad021` 机械修正；Ruff 和
该脚本的 5 项测试随后通过。生产最终运行镜像为
`tickflow-stock-panel-app:dow-monitor-short-side-0fdd9e7-20260723-2358`，
01347.HK 五周期继续保持 `LIVE / WATCH`，Chronicle 探针输出
`Dow monitor healthy`。

## 2026-07-24 卡片空间分配独立复核

复核范围只包含 `REQ-DOW-WATCH-UI-001` 的卡片展示分配。实现差异只涉及
`DowMonitorCard.tsx` 和 `DowMonitor.test.tsx`；没有后端、数据源、
`DowMiniChart` 图表选项或 Longbridge 道氏算法文件发生变化。

需求到证据复核如下：

| 要求 | 可执行或生产证据 | 结论 |
| --- | --- | --- |
| 摘要为两行紧凑信息区 | 测试断言 `data-layout=compact-two-row`；生产实测 `64px` | PASS |
| 价格、时间和控制信息不得删除 | 原字段测试继续通过；生产 DOM 完整显示所有字段 | PASS |
| 五周期继续全部可见 | 原五周期交互测试继续通过；生产实测五按钮均为 `20px` | PASS |
| 迷你 K 线高度为 `180px` | 测试直接断言内联高度；生产 DOM 实测 `180px` | PASS |
| 底部中文操作与形态保留 | 原通知测试通过；生产显示“买入（开多）/强势回归确认” | PASS |
| 不改变道氏语义 | 代码差异不含图表选项、后端或引擎文件；98 项前端测试通过 | PASS |

独立要求到证据复核没有发现以截图或黄金文件代替语义验收的情况：布局尺寸由可执行
测试和生产 DOM 同时证明，趋势线与信号语义继续沿用既有下层验收。最终结论：

- Critical：0；
- Important：0；
- SPEC：PASS；
- QUALITY：PASS；
- READY：YES。

## 2026-07-24 WebStock 实时刷新独立复核

复核从 `REQ-DOW-WATCH-DATA-001` 与 `REQ-DOW-WATCH-STALE-001` 反向检查实现、
测试和生产证据：

| 要求 | 证据 | 结论 |
| --- | --- | --- |
| 监控股票可被实时订阅 | 回环只读清单接口；Longbridge状态显示2只强制监控股票 | PASS |
| WebSocket 1分钟优先 | provider三源合并中 WebSocket 优先级为3；同分钟覆盖测试 | PASS |
| 历史分钟不丢失 | 原 minute bars 与 intraday lines 分支保留 | PASS |
| stale保护不降低 | 90秒报价、120秒分钟阈值代码未修改；相关测试继续通过 | PASS |
| 浏览器刷新真实可见 | 生产DOM显示数据源10:19及两张卡片的新行情时间 | PASS |

生产监控错误已由 `QUOTE_TOO_OLD / MINUTE_TOO_OLD` 恢复为
`last_error=null / errors={}`。没有用页面截图替代下层时间戳和表级验证。
最终复核未发现 Critical 或 Important 问题。

## 2026-07-24 卡片价格方向颜色独立复核

复核范围为 `REQ-DOW-WATCH-UI-001`。实现只复用 TickFlow 已有的
`text-bull`、`text-bear` 和 `text-muted` 主题类，没有新增颜色常量，也没有修改
道氏信号、趋势线或数据源语义。测试同时覆盖正涨幅和负涨幅卡片，确认价格与涨跌幅
使用同一方向颜色。独立复核还确认生产镜像包含新的 `DowMonitor` 静态资源，
健康检查和 WebStock 后台轮询均正常。结论：Critical 0，Important 0，READY YES。

## 2026-07-24 卡片操作周期、时间与价格独立复核

复核范围为 `REQ-DOW-WATCH-UI-001`。实现只消费既有通知快照字段，没有修改通知
生成、去重、道氏信号或行情刷新语义。测试明确区分了信号触发时间与卡片顶部行情、
成功时间，并验证触发价格按两位小数显示；同时令当前卡片保持 5 分钟而通知周期为
日 K，确认页面显示 `周期 日K`，不存在用当前所选周期冒充触发周期的问题。生产通知
记录的 `timeframe=day` 与正式镜像静态资源分别证明字段来源完整、展示代码已发布；
健康检查及 WebStock `last_error=null` 证明后台监控未受影响。结论：Critical 0，
Important 0，READY YES。

## 2026-07-24 涨跌幅、02714.HK 与北京时间独立复核

复核范围为 `REQ-DOW-WATCH-DATA-001` 和 `REQ-DOW-WATCH-UI-001`。数据链路从生产
原始快照反向核查到查询、标准化和卡片展示，确认缺失涨跌幅来自同一分钟最后一条
WebStock 增量快照字段为空，而不是前端颜色或布局。修复只在严格监控查询内补齐同日
昨收并重算涨跌幅，通用行情接口的既有精度测试继续通过。

`02714.HK` 的元数据、实时价、分钟线和日线均使用港股存储键 `2714.HK`；独立查询的
`002714.SZ` 价格不同，未发现跨市场串数。前端共享格式函数只改变展示时区，不改写
后端时刻或不可变通知。回归测试和生产库候选查询均为下层语义证据，不以截图替代。
正式镜像复核同时确认报价字段和北京时间代码均已发布。生产的
`01347.HK: QUOTE_TOO_OLD / MINUTE_TOO_OLD` 来自外部 WebStock 表停更，严格阻断符合
`REQ-DOW-WATCH-STALE-001`，不能将报价仍实时误判为 K 线可交易。结论：
Critical 0，Important 0，READY YES。

## 2026-07-24 卡片内消息通知独立复核

复核从 `REQ-DOW-WATCH-UI-001` 反向检查权威设计、实现、测试和生产 DOM：

| 要求 | 证据 | 结论 |
| --- | --- | --- |
| 顶部不再显示集中通知 | 页面不再导入通知栏组件；生产 DOM 数量为 0 | PASS |
| 通知归属对应股票 | 页面按 `notification.symbol` 分组传入卡片；跨股票隔离测试通过 | PASS |
| 独立文本消息框 | 每张卡片使用带股票名的 `role=log`；生产三张卡片均存在 | PASS |
| 多条消息可查看 | 消息框固定 `h-20` 并 `overflow-y-auto`；02714.HK 多条消息可滚动 | PASS |
| 信息完整 | 每条消息显示操作、周期、北京时间、价格和形态；行为测试逐字段断言 | PASS |
| K 线空间不被缩小 | 生产实测迷你 K 线仍为 180 像素，消息框独立为 80 像素 | PASS |
| 未读操作保留 | “已读”按钮迁移到卡片消息，双通知并发测试继续通过 | PASS |

实现差异只涉及监控页、卡片和被移除的旧通知栏组件；后端 WebStock、道氏趋势线、
信号计算和通知持久化均未修改。完整前端 101 项测试、生产构建、规格检查和生产健康
检查构成独立证据。当前 `01347.HK` 数据延迟仍按既有规则可见。最终结论：
Critical 0，Important 0，SPEC PASS，READY YES。

## 2026-07-24 卡片消息纯文本与高度扩展独立复核

复核范围为 `REQ-DOW-WATCH-UI-001` 的消息区样式和容量，不涉及通知生成、道氏信号
或 WebStock 数据语义。

| 要求 | 独立证据 | 结论 |
| --- | --- | --- |
| 不再嵌套卡片式通知 | 组件只保留文本布局与 `border-b`；生产计算样式为透明背景、0 像素圆角、无上/左/右边框 | PASS |
| 消息框显示更多行 | 组件使用 `h-32`；生产三张消息框实测约 128 像素且独立滚动 | PASS |
| 信息字段不丢失 | 生产 DOM 仍显示操作、周期、北京时间、价格、形态及“已读”操作 | PASS |
| K 线空间保持 | `DowMiniChart` 未修改，生产迷你 K 线仍为 180 像素 | PASS |
| 可执行回归 | 监控页 30 项、完整前端 101 项测试及生产构建通过 | PASS |
| 正式发布 | 镜像 `tickflow-stock-panel-app:dow-monitor-plain-messages-20260724-1401` 运行，`/health` 正常 | PASS |

需求到证据反向检查未发现以截图替代语义验收的情况：样式由行为测试和生产计算样式
共同证明，信息内容由生产 DOM 证明，下层信号逻辑沿用既有验收且本次无代码差异。
最终结论：Critical 0，Important 0，SPEC PASS，READY YES。

## 2026-07-24 最新消息特殊标记独立复核

复核范围仅为 `REQ-DOW-WATCH-UI-001` 的最新消息视觉辨识。实现以通知数组索引 0
作为接口返回的第一条记录，没有在前端重排通知，也没有用触发时间重新推断顺序。

| 要求 | 独立证据 | 结论 |
| --- | --- | --- |
| 每股最新消息可识别 | 第一条记录显示“最新”并使用 `border-l-2 border-l-accent` | PASS |
| 标记只能出现一次 | 测试验证第二条无标记；生产 01347.HK 和 02714.HK 均为 `markedCount=1` | PASS |
| 无消息时不误标 | 生产 0981.HK 为 0 条通知、0 个“最新”标记 | PASS |
| 不恢复嵌套卡片 | 最新记录仍无独立背景、圆角或四周边框，只增加左侧强调线 | PASS |
| 不改变信号语义 | 后端、通知生成、通知排序、道氏引擎和 WebStock 代码均未修改 | PASS |
| 可执行与生产证据 | 监控页 30 项、完整前端 101 项、生产构建及 `/health` 通过 | PASS |

反向需求审查未发现 Critical 或 Important 问题，且没有以页面截图代替 DOM 和行为测试
证据。最终结论：SPEC PASS，QUALITY PASS，READY YES。

## 2026-07-24 港股前导零等价代码独立复核

复核从 `REQ-DOW-WATCH-DATA-001` 反向检查实现、测试和生产状态。根因是监控清单
允许同时保存 `02714.HK` 与 `2714.HK`，而不是 WebStock 缺少 `2714.HK` 行情；
生产严格实时和分钟查询均已直接返回该股票数据。

| 要求 | 独立证据 | 结论 |
| --- | --- | --- |
| 等价港股代码不能重复监控 | `DowMonitorStore.upsert_symbol` 在持久化边界使用去前导零身份比较 | PASS |
| 保留用户已有卡片身份 | 第二次加入等价别名更新原记录并返回原 symbol，不静默改写代码 | PASS |
| 不跨市场误合并 | 身份折叠仅作用于 `.HK` 数字代码；`002714.SZ` 在生产清单中独立保留 | PASS |
| 行为可执行 | 新测试先 RED 后 GREEN；相关后端回归 141 项通过 | PASS |
| 下层数据语义 | 生产 `2714.HK` 五周期均为 LIVE，K 线数为 923/308/154/84/112，来源时间为北京时间 15:51 | PASS |
| 数据可恢复 | 修改生产清单前保存 symbols/states 两份时间戳备份；通知历史未改动 | PASS |
| 正式发布 | 镜像 `tickflow-stock-panel-app:dow-monitor-hk-alias-20260724-1542` 运行，`/health` 正常 | PASS |

本次实现只封闭了监控清单的等价代码重复入口，没有修改 WebStock 查询、五周期聚合、
趋势线、买卖点或通知生成。前端 104 项测试、生产构建和规格检查均通过。完整后端
测试收集仍受工作树中既有的 `longbridge_stock.system_patterns` 与
`structure_breakout_scanner` 缺失阻断；扩大后的后端回归为 652 项通过、10 项既有
失败，与本次存储幂等修改无关。最终结论：Critical 0，Important 0，
SPEC PASS，QUALITY PASS，READY YES。
