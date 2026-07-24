# 01347.HK 道氏实时监控语义验收

日期：2026-07-23
范围：仅 `01347.HK`；未扩展到其他股票。

## 生产实况

- TickFlow 提交：`21f1493`、`072afde`；Longbridge 提交：`1430d0e`。
- 生产镜像：
  `tickflow-stock-panel-app:dow-monitor-short-side-0fdd9e7-20260723-2358`；
  上一可用镜像 `dow-monitor-badges-sessions-850a313-20260723-2350`
  保留用于回滚。
- `/health` 返回 `status=ok`；`/api/dow-monitor/status` 返回
  `running=true`、`last_error=null`、`errors={}`。
- WebStock strict 数据为 `LIVE`，缺口列表为空；01347 的严格分钟序列为
  `2026-07-17 09:30` 至 `2026-07-23 16:00`，共 1655 个唯一分钟。
- T-1（2026-07-22）在 ClickHouse 中有 331 个唯一分钟，覆盖
  `09:30` 至 `16:00`。这项下层完整性先于五周期验收通过。

五周期均由同一批最新 WebStock 1m 数据重算并持久化：

| 周期 | 状态 | source timestamp | 最后 K 线 | K 线数 | 当前动作 |
| --- | --- | --- | --- | ---: | --- |
| 5m | LIVE | 2026-07-23 16:00+08:00 | 15:55 | 858 | 观察 |
| 15m | LIVE | 2026-07-23 16:00+08:00 | 15:45 | 286 | 观察 |
| 30m | LIVE | 2026-07-23 16:00+08:00 | 15:30 | 143 | 观察 |
| 60m | LIVE | 2026-07-23 16:00+08:00 | 15:00 | 78 | 观察 |
| 日 K | LIVE | 2026-07-23 16:00+08:00 | 2026-07-23 | 745 | 观察 |

当前生产实况是 `WATCH/观察`，因此当前五个周期没有控制线，也没有伪造新的
交易通知。

## 生产页面人工验收

在已登录的生产页面 `http://192.168.10.28:3018/dow-monitor?market=hk`
直接检查生产页面；最终镜像为
`tickflow-stock-panel-app:dow-monitor-short-side-0fdd9e7-20260723-2358`：

- 页面复用现有 TickFlow 侧栏、主题和认证框架，侧栏入口为“趋势监控”；
- 页面同时显示“全部 / A股 / 港股 / 美股”市场筛选，以及“全部 / 有信号 /
  仅买点 / 仅卖点”状态筛选；
- 页面只显示已启用的 `01347.HK` 卡片，卡片开关开启，五个周期按钮和最新通知区
  同屏可见；
- 后端五个当前状态均为 `WATCH` 时，5、15、30、60 分钟和日 K 五个周期徽标均为
  黄色；历史通知仍保留在卡片文字区和通知区，但不会再把当前周期徽标错误染成
  红色或绿色；
- 点击卡片可以打开“01347.HK 完整K线”弹窗，弹窗显示实时状态、五周期切换、
  成交量、MACD、RSI、KDJ、BOLL、量能对比、OHLC 和均线信息。

当前生产数据没有开空或平空事件，因而不伪造生产短仓信号。可执行回归测试另外构造
与历史 BUY 信号冲突的当前 `OPEN_SHORT`、`CLOSE_SHORT` 快照，确认两者均按后端
风险/卖出语义显示红色，而不是回退到历史 BUY 的绿色。

该检查是生产 UI 的实际 DOM/交互观察；它不替代下层 WebStock 完整性、Longbridge
引擎语义和通知状态机验收。

## 真实引擎事件样本

使用上述 01347.HK 真实 30 分钟 K 线逐根回放 Longbridge
`/api/dow-state/evaluate`，得到以下真实激活样本：

- 触发 K 线：`2026-07-10 14:30+08:00`
- 完成状态：`FINAL`
- 形态：`二次突破确认`
- 动作：`卖出（平多）`（`CLOSE_LONG`）
- 当前 OHLC：`201.8 / 201.8 / 195.6 / 195.6`
- 控制线：`SUPPORT-MAIN-2`，`MAIN / SUPPORT`
- 锚点一：`2026-07-08 15:00+08:00 @ 183.6`
- 锚点二：`2026-07-09 13:30+08:00 @ 192.3`

Longbridge 返回的 line ID、角色、方向、两个锚点时间和价格均直接作为
TickFlow 的消费字段；TickFlow 没有重新推断锚点或买卖点。

## 受控通知序列

真实引擎身份作为语义基准；通知状态机用隔离存储执行受控序列。生产实况
仍保持 WATCH，受控序列没有写入生产通知：

1. 首次激活只产生一条 `activation_sequence=1` 通知。
2. 连续两个周期保持同一 family/line，不重复通知。
3. 先进入 WATCH 清除激活，再次激活同一 line，产生
   `activation_sequence=2`。
4. 重建 `DowMonitorStore` 和服务后，通知仍存在且不会误增 sequence。
5. WebStock stale/session-gap 时保留最后图表和快照，标为数据延迟，不调用
   引擎且不新增通知。

对应可执行证据：

```text
pytest -q tests/test_dow_monitor_service.py -k
"activation_notifies_once_then_reactivation_uses_next_sequence_and_deepcopy or
stale_webstock_retains_snapshot_and_chart_and_sends_no_notification or
restart_recovers_from_last_reliable_timestamp_without_duplicate_event or
restart_after_first_notification_write_before_any_state_does_not_emit_sequence_two"
4 passed
```

生产通知文件在重启和页面关闭后仍保留两条先前的真实 01347 通知快照；
本次 stale 故障和当前 WATCH 重算没有增加第三条。

## 部署与回滚证据

- 曾发现并发故障镜像
  `tickflow-stock-panel-app:dow-monitor-stock-ai-live-fallback-20260723-2318`
  缺少 `app.api.dow_monitor`；已原子回滚到
  `dow-monitor-20260723-2300` 并恢复 `/health`，故障镜像保留供审计。
- 最终候选基于该已验证镜像分层构建并原子切换。
- Longbridge 回补通过
  `/etc/systemd/system/longbridge-api.service.d/realtime-sinks.conf`
  同时写 PostgreSQL 和 ClickHouse。回滚方式是移除该 drop-in、执行
  `systemctl daemon-reload` 并重启既有 `longbridge-api.service`；没有新增
  systemd timer 或调度服务。

## Chronicle 调度验收

- Chronicle 事件：`tickflow-dow-monitor-health`（ID `emrxo5gnr94`）。
- 事件唯一且已启用；时区为 `Asia/Shanghai`，每小时第
  `0/10/20/30/40/50` 分钟执行。
- Chronicle 调度器状态为启用。
- 2026-07-23 手工触发 Chronicle 实际执行一次：run ID
  `jmrxo5s5095`，`action=job_complete`，`code=0`，耗时 1.242 秒。
- `crontab`、系统级 systemd timer、用户级 systemd timer 中同名健康巡检均为
  0 条，Chronicle 是该巡检的唯一调度入口。

## 2026-07-24 紧凑卡片空间回归

按 `REQ-DOW-WATCH-UI-001` 的 2026-07-24 批准补充，在生产页面
`http://192.168.10.28:3018/dow-monitor?market=hk` 对 `01347.HK` 做实际
DOM 与可视检查。运行镜像为
`tickflow-stock-panel-app:dow-monitor-card-chart-caa6380-20260724-0915`。

- 卡片实测约 `502 × 302` 像素；
- 股票摘要压缩为两行，实测高度 `64` 像素；
- 迷你 K 线实测高度 `180` 像素，约占卡片总高度的 60%，成为主要视觉区域；
- 5、15、30、60 分钟和日 K 五个按钮全部可见，实测高度均为 `20` 像素；
- 股票代码、名称、价格、涨跌幅、行情时间、成功时间、监控开关和移除按钮均保留；
- 底部继续显示中文操作“买入（开多）”和中文形态“强势回归确认”；
- K 线、主趋势线、加速线和买卖点由原 `DowMiniChart` 选项绘制，未修改道氏
  数据、信号或新鲜度逻辑。

可执行证据为 `DowMonitor.test.tsx` 新增的两行摘要与 `180px` 图表高度断言；
完整前端回归为 `26 passed / 98 passed`，生产构建和规格检查均通过。容器切换后
`/health` 返回 `status=ok`，监控状态继续为 `running=true`、`source=webstock`、
`last_error=null`。

## 2026-07-24 价格对比度回归

紧凑布局首次上线后，价格字号使用了 `text-base`。该项目同时在 Tailwind 主题中
定义 `base` 背景色，编译后的歧义工具类不仅设置字号，还把价格颜色设为
`hsl(var(--base))`，导致暗色卡片中数值接近黑色。修复改为无歧义的
`text-[16px] text-foreground`。

生产镜像
`tickflow-stock-panel-app:dow-monitor-price-contrast-945a835-20260724-0931`
中，`01347.HK` 实测结果为：

- 价格文本：`146.00`；
- 价格计算颜色：`rgb(250, 250, 250)`；
- 卡片计算背景：`rgb(24, 24, 27)`；
- 价格字号：`16px`；
- 价格元素不再包含 `text-base`。

新增回归测试先在原实现上 RED，明确收到
`shrink-0 font-mono text-base tabular-nums`；单点修复后监控页 28 项测试和完整
前端 99 项测试全部通过，生产构建、规格检查和 `/health` 均通过。

## 2026-07-24 WebStock 实时刷新链路验收

生产故障复现时，页面仍每 15 秒轮询，但 `01347.HK` 报价停在 09:25，
`0981.HK` 的 WebSocket `min_1` 已更新而 strict minute 查询仍只读取
`lb_minute_bars` 与 `lb_intraday_lines`。新实现完成两项下层修复：

1. 回环地址 `GET /api/dow-monitor/symbols` 向 Longbridge 订阅器提供监控清单；
   POST 和非回环 GET 仍需登录；
2. strict minute 以 `lb_realtime_candlesticks(min_1)` 为最高优先级，并用原两表
   补齐历史和缺失分钟。

可执行回归为 TickFlow 监控数据、API与状态机 `131 passed`。生产语义证据：

- `/health` 返回 `status=ok`；
- `/api/dow-monitor/status` 返回 `last_error=null`、`errors={}`；
- 10:18 时，01347与0981的报价和 WebSocket 1分钟K线均到10:18；
- 生产页面数据源时间为10:19；01347卡片行情/成功均为10:19，0981行情为10:19、
  成功为10:18；
- 卡片继续由 15 秒轮询刷新，没有降低90秒报价和120秒分钟线的新鲜度门槛，
  也没有静默切换其他实时源。

## 2026-07-24 卡片价格红涨绿跌验收

`REQ-DOW-WATCH-UI-001` 的价格方向颜色统一为现有市场语义：上涨价格与涨幅共同
使用 `text-bull` 红色，下跌价格与跌幅共同使用 `text-bear` 绿色，平盘使用
`text-muted`。道氏买卖信号颜色保持原定义，不受本次改动影响。

行为测试先在原实现上失败，明确显示上涨价格仍为 `text-foreground`；修改后
`frontend/src/pages/DowMonitor.test.tsx` 的 28 项测试全部通过，并同时验证上涨
与下跌两种方向。完整前端测试为 99 项通过，生产构建通过。正式镜像
`tickflow-stock-panel-app:dow-monitor-price-direction-20260724-1055` 已包含
`DowMonitor` 页面资源；生产 `/health` 正常，WebStock 监控仍按 15 秒轮询且
`last_error=null`。

## 2026-07-24 卡片操作周期、时间与价格验收

`REQ-DOW-WATCH-UI-001` 的卡片底部操作提示补齐信号触发周期、信号触发时间和
信号触发价格。显示值直接取不可变通知快照的 `timeframe`、`triggered_at` 与
`trigger_price`，不使用当前选中的卡片周期、行情时间或后台成功时间替代。布局
保持单行，中文操作、触发周期、触发时间和价格不可压缩，空间不足时只截断末尾
形态名称。

行为测试先在原实现上失败，明确找不到 `触发 2026-07-23 01:05Z`；周期测试又在
只显示所选周期的实现上失败，实际显示 `周期 5分` 而不是通知自身的 `周期 日K`。
修正后测试特意保持卡片选中 5 分钟、通知周期为日 K，并通过 `周期 日K` 断言。
监控页 29 项测试及完整前端 100 项测试全部通过，生产构建通过。生产镜像
`tickflow-stock-panel-app:dow-monitor-signal-timeframe-20260724-1310`
包含新的 `DowMonitor` 资源及“周期”“触发”字段。生产 01347.HK 最新通知快照实测
同时具有 `timeframe=day`、`triggered_at=2026-07-23T15:13:24.670379Z` 和
`trigger_price=168.5`；`/health` 正常，WebStock 继续按 15 秒轮询且
`last_error=null`。

## 2026-07-24 涨跌幅稳定性与北京时间验收

`REQ-DOW-WATCH-DATA-001` 的严格实时查询不再直接依赖最后一条增量快照是否携带
昨收和涨跌幅。同一股票当日最近的非空昨收价会被补入最新快照，监控路径再以最新价
和昨收价计算涨跌额与涨跌幅；该行为没有扩散到系统通用实时行情接口。

生产只读核查证明 WebStock 同一分钟确实存在完整快照与缺字段快照交替写入。候选
查询直接连接生产 ClickHouse 后，`01347.HK`、`00981.HK`、`02714.HK` 均返回非空且
与最新价一致的涨跌幅。`02714.HK` 返回名称 `MUYUAN`、港股价格约 32.2 港元，
`002714.SZ` 同期约 38.9 元，证明港股与 A 股没有混用；港股代码去前导零仅用于匹配
WebStock 的 `2714.HK` 存储键。

`REQ-DOW-WATCH-UI-001` 的共享时间格式统一将 UTC 时刻转换为北京时间。行为测试先
明确失败于旧的 `Z` 时间，修改后验证行情、成功、数据源和通知触发时间均增加 8 小时
且不再显示 `Z`。相关后端 132 项测试、完整前端 100 项测试及生产构建通过。

生产镜像
`tickflow-stock-panel-app:dow-monitor-quote-beijing-20260724-1339`
上线后，严格查询实测 `01347.HK`、`00981.HK`、`02714.HK` 均返回最新价、昨收价和
非空涨跌幅；正式静态资源包含 `UTC+8` 转换并移除 `Z` 后缀，`/health` 返回正常。
最终镜像严格查询同时证明同日昨收只向前取值，不读取晚于当前快照的记录。当前外部
WebStock 对 `01347.HK` 的报价和 1 分钟 K 线随后停止更新，系统按既有新鲜度规则报告
`QUOTE_TOO_OLD` / `MINUTE_TOO_OLD`，没有用本次涨跌幅修复绕过信号阻断。

## 2026-07-24 卡片内消息通知验收

`REQ-DOW-WATCH-UI-001` 按用户批准的新布局移除页面顶部集中通知栏，并将通知按股票
归入各自卡片底部的固定消息框。每个消息框高度为 80 像素、内容超出时独立滚动；
消息逐条显示中文操作、触发周期、北京时间、触发价格和中文形态，未读通知继续提供
“已读”操作。无通知、加载中和加载失败状态也只在对应卡片消息框中显示。

行为测试先在旧实现上失败，证明页面仍存在 `dow-monitor-signal-rail` 且卡片不存在
消息框；实现后监控页 30 项测试、完整前端 101 项测试和生产构建全部通过。正式镜像
`tickflow-stock-panel-app:dow-monitor-card-messages-20260724-1355` 上线后的生产 DOM
证明：

- 顶部集中通知栏数量为 0；
- `01347.HK`、`0981.HK`、`02714.HK` 均有独立 `role=log` 消息框；
- 迷你 K 线继续保持 180 像素，消息框为 80 像素；
- `02714.HK` 的多条 15 分钟和 30 分钟卖出通知均归入其卡片并可滚动查看；
- `/health` 返回 `status=ok`。

生产页面同时保留 `01347.HK` 的“数据延迟”状态，这是外部 WebStock 新鲜度阻断，
没有被本次纯前端布局调整隐藏或绕过。

## 2026-07-24 卡片消息纯文本与高度扩展验收

`REQ-DOW-WATCH-UI-001` 按最新批准进一步简化卡片消息区：消息框由 80 像素增至
128 像素；每条通知直接显示为文本，不再设置独立背景、圆角或四边边框，仅使用底部
细分隔线区分相邻通知。操作、触发周期、北京时间、触发价格、中文形态和“已读”操作
均继续保留。

行为测试先在旧实现上失败，明确检测到 `h-20` 和卡片式消息样式；修改后监控页
30 项测试及完整前端 101 项测试全部通过，生产构建通过。正式镜像
`tickflow-stock-panel-app:dow-monitor-plain-messages-20260724-1401` 上线后，生产
DOM 实测三张消息框高度均约为 128 像素且 `overflow-y=auto`。`02714.HK` 消息区
包含 8 条通知，抽查前 4 条均满足：透明背景、0 像素圆角、上/左/右边框为 0，仅保留
约 1 像素底部分隔线。页面可在消息框内连续看到更多通知，迷你 K 线仍保持 180 像素。
`/health` 返回 `status=ok`。

## 2026-07-24 最新消息特殊标记验收

`REQ-DOW-WATCH-UI-001` 的每股消息框将通知接口返回的第一条记录标记为最新消息。
最新消息增加“最新”文字和左侧强调线；其余记录不显示该标记。强调方式没有增加独立
背景、圆角或四周边框，继续符合纯文本消息规范。

行为测试先在旧实现上失败，明确证明第一条记录缺少 `border-l-2`、`border-l-accent`
和“最新”文字；最小实现后监控页 30 项、完整前端 101 项测试和生产构建全部通过。
正式镜像 `tickflow-stock-panel-app:dow-monitor-latest-marker-20260724-1414` 上线后，
生产 DOM 验证结果为：

- `01347.HK` 有 2 条通知，仅第一条存在“最新”和左侧强调线；
- `02714.HK` 有 9 条通知，仅第一条存在“最新”和左侧强调线；
- `0981.HK` 没有通知，因此不显示“最新”；
- 各股票第二条及后续通知仍为普通纯文本分隔行；
- `/health` 返回 `status=ok`。
