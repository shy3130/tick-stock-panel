# 道氏趋势监控列表与 WebSocket 验收

状态：已发布；本地语义验收与生产静态包、存量股票、WebSocket 验收通过

## 语义验收场景

1. 分别选择 A 股、港股、美股，列表只出现对应市场股票，每页最多 20 只。
2. 在超过 20 只的受控数据集中切换页码，WebSocket 订阅集合只包含当前页已启用股票。
3. 连续推送 quote 与形成中 1 分钟 K 线，价格、涨跌幅、mini 趋势线末端更新，而通道、控制线距离、动量、量比、资金和买卖信号不变。
4. 返回新的完成分钟概览后，列表决策字段同步更新。
5. 后端正式通知存在时，列表展示对应买入、卖出或风险信号及其时间；失败突破不得升级为操作信号。
6. 数据延迟超过 90 秒或后端标记延迟时，列表明确显示延迟且不生成新的正式信号。
7. 点击任意行或“查看详情”，详细 K 线区域出现在列表下方，不出现模态框；再次点击
   同一股票时详情收拢并取消选中状态，点击另一只股票时直接切换详情。
8. mini 趋势线只有一条折线，无背景、坐标轴、K 线或其他叠加线。

## 自动化证据

- `frontend/src/components/dow-monitor/monitorListPresentation.test.ts`
  - 完成 K 线过滤、15m/30m 通道、控制线回退、5m/15m 动量、资金质量、信号持久性、
    失败突破、延迟抑制、日内线和 20 只分页：`8 passed`。
- `frontend/src/components/dow-monitor/DowMonitorList.test.tsx`
  - 必要列、单折线、无背景、选中行、固定“查看详情”、延迟状态和分页：`3 passed`。
- `frontend/src/components/dow-monitor/DowMonitorDetailPanel.test.tsx`
  - 非 dialog 内嵌详情和周期/叠加层控制：`1 passed`。
- `frontend/src/pages/DowMonitor.test.tsx`
  - 三市场、20 行、当前页订阅、实时/决策边界、内嵌详情展开/收拢、市场切换和筛选：
    `7 passed`。
- `frontend/src/lib/realtimeMarketData.test.ts`
  - 订阅、重连、最新状态与每秒批量发布：`12 passed`。
- `tests/spec_contracts/test_dow_monitor_list_websocket_contract.py` 与
  `tests/spec_contracts/test_realtime_frontend_contract.py`：`2 passed`。
- `backend/tests/test_realtime_websocket.py`：`5 passed`。
- 除已知基线失败 `src/pages/Screener.dow-strategy.test.tsx` 外的前端套件：
  `35 files passed, 137 tests passed, 2 obsolete modal-integration tests skipped`。
- `pnpm --dir frontend build`：成功。

## 浏览器证据

在 1440×900 Chromium 中使用只读模拟 API 响应检查：

1. 市场入口仅有 A 股、港股、美股；
2. 表头包含全部十个约定字段；
3. mini 图 DOM 只有一个 `polyline`，没有背景矩形；
4. “查看详情”点击后行变为选中状态；
5. `1.HK 详细走势` region 位于列表和分页之后，页面没有 dialog；
6. 详情保留 5/15/30/60 分钟、日线、成交量、MACD、RSI、KDJ、BOLL、趋势线和头肩形态控制。

浏览器截图：`.playwright-cli/page-2026-07-29T05-57-21-674Z.png`（临时验收产物，
不纳入版本库）。

## 已知非本次阻断项

- 全量前端套件仍有基线已有的 Screener 文案测试失败：
  `Screener.dow-strategy.test.tsx` 查找“道氏趋势 · 多周期”失败；本次未修改 Screener。
- `scripts/check_spec_compliance.py` 仍报告两个基线问题：已过期的采集监控预验收例外，
  以及旧详情需求把前端测试路径登记在 `tests/` 目录之外。本次五条新需求不再新增该类错误。
- 生产页面受密码认证保护；自动化浏览器确认登录门禁正常，但未绕过认证执行生产 DOM
  操作。生产镜像中的静态包哈希与已经过浏览器语义验收的本地构建完全一致。

## 生产发布证据

发布时间：2026-07-29 14:13（Asia/Shanghai）。

- 源码 revision：`0429bf65b2abb79525124864ae4b4168a18f8567`。
- 镜像：`tickflow-stock-panel-app:dow-monitor-list-ws-0429bf65-20260729-141117`。
- 镜像 ID：`sha256:2ab149a4999293fabf3dc60d26479e1feb6018433c1fef457b786a77fa7062f1`。
- 发布前备份：
  `/home/alwin/backups/tickflow-dow-monitor-list-ws-predeploy-20260729T141117`。
- 原始数据文件与发布后容器文件 SHA-256 均为
  `1d5955494b4a74d8ae32bd550e4f744e09c4cab80c85f190acd01f3717bef59e`。
- 发布前后 `/api/dow-monitor/symbols` 响应逐字节一致，共 13 只：
  1 只 A 股、5 只港股、7 只美股，全部保持启用状态和原始创建时间。
- `/health` 返回版本 `0.1.86`；容器为 `running`，重启次数 0，3018 只有一个监听，
  发布窗口日志无 `ERROR`、`CRITICAL` 或 `Traceback`。
- 生产入口：`assets/index-DM-MIE_k.js`；
  道氏列表分包：`assets/DowMonitor-Bsh30vpz.js`；
  实时分包：`assets/realtimeMarketData-BdMyWeJ8.js`。
- 上述三个分包在生产容器内的 SHA-256 分别为：
  `020c48f1f05c75415b3f54dab9a4c7e2e605daa88409084856962d6031e2f469`、
  `67c4e465eafc1944179fa180d5488e2405f6d3171845593e2cc39b0fdbeb5221`、
  `44bebf62be59d4b4e188943b4c60563fd1e8d90562f77cc0d18bb4518530b39a`。
- 使用生产 Origin `http://192.168.10.28:3018` 连接
  `ws://192.168.10.28:3018/ws/realtime`，收到 `hello/v1`，并对当前港股页 5 只股票
  分别收到同时含 `quote`、`depth`、`candlestick` 的 snapshot。

生产验收不把一次 WebSocket 快照当作“盘中持续稳定”的证明；持续更新和完成分钟内
决策字段不抖动仍应在对应市场交易时段观察。

## 2026-07-29 最终广泛复核修复

- 权威决定 `DEC-20260729-DOW-MONITOR-CONTROL-FALLBACK-001` 修正旧规范：
  控制线距离和相对成交量分别只允许稳定 15m → 稳定 30m 回退，永不读取 5m。
- RED 证据：旧实现把 `FORMING` 15m 的控制线 `1.5` 当成稳定值，而不是回退到
  30m 的 `0.7`；跨到下一分钟但仅经过 70 秒的 1m K 线仍错误计算出 `0.4×` 量速。
- GREEN 证据：同一行为测试覆盖 `FORMING` 和 truthy `provisional` 两种 15m
  无效状态，控制线和量比分别回退到 30m；只有 5m 值时两者均返回缺失。量速还要求
  K 线时间戳与 `nowMs` 位于同一绝对分钟，原 20 秒和 75 秒门槛保留。
- 精确五文件 Vitest：`40 passed`；规格契约：`3 passed`；后端 WebSocket：
  `5 passed`；生产构建成功。规格检查仍只有两个已记录基线问题。

## 2026-07-29 详情收拢热修复

- 根因：`DowMonitor.selectSymbol` 无条件把点击股票写入选中状态，没有处理“当前股票已经
  选中”的分支，因此详情只能展开或切股，不能收拢。
- RED：页面测试第一次点击展开后再次点击同一“查看详情”，预期详情不存在，实际详情
  仍在，测试准确失败。
- GREEN：选中状态改为同股二次点击时置 `null`；页面测试 `7 passed`，相关测试
  `31 passed`，契约测试 `2 passed`，生产构建成功。
- 源码 revision：`36ded7fbc24adac99fc8ac5e570f39f6668e2bea`。
- 正式镜像：
  `tickflow-stock-panel-app:dow-monitor-detail-toggle-36ded7fb-20260729-142949`。
- 镜像 ID：`sha256:cc228ab6eacdeac796595b31823e37ce00ddf174f52cf1196a39ebbd9cc76a13`。
- 回滚镜像：
  `tickflow-stock-panel-app:dow-monitor-list-ws-0429bf65-20260729-141117`。
- 发布前备份：
  `/home/alwin/backups/tickflow-dow-monitor-detail-toggle-predeploy-20260729T142949`。
- 发布前后 13 只监控股票 API 响应逐字节一致；数据文件 SHA-256 仍为
  `1d5955494b4a74d8ae32bd550e4f744e09c4cab80c85f190acd01f3717bef59e`。
- 生产 `DowMonitor-CnwWUXLZ.js` SHA-256 为
  `993c4c23ac757191fb8844c3c0c6cc9fdf4cc2053eccaf4ed9ac4a1d8e45f692`；
  容器运行、重启次数 0，健康检查通过，发布日志无错误。

## 2026-07-29 涨跌幅单位与实时昨收修复

- 两个根因分别位于不同层：
  1. 港股实时流的昨收基线在自然日零点过早缓存，盘中仍沿用上一交易日的旧值；
  2. WebSocket 断线时，HTTP 概览的 `change_pct` 是小数制，但列表按百分数值直接显示。
- RED：前端纯函数测试输入 `change_pct=0.0125`，旧实现返回 `0.0125%`，而要求为
  `1.25%`；测试按预期失败。
- GREEN：实时流继续使用 `(lastDone-prevClose)/prevClose*100`；HTTP 回退值只在
  展示边界乘一次 `100`。纯函数测试 `9 passed`，监控列表相关测试 `19 passed`，
  规格契约 `2 passed`，生产构建成功。
- 源码 revision：`f34eddaf5f32a2fc55b12c272c5c20ab035b36e1`。
- 正式镜像：
  `tickflow-stock-panel-app:dow-monitor-change-pct-f34edda-20260729-145154`；
  镜像 ID：
  `sha256:57bd9849fe0c3767d088c0f08abd5fdcbbab9f9b5df38f33d7491a69b59d53c0`。
- 回滚镜像：
  `tickflow-stock-panel-app:dow-monitor-detail-toggle-36ded7fb-20260729-142949`。
- 发布前备份：
  `/home/alwin/backups/dow-monitor-change-pct-predeploy-20260729T145154/`。
- 生产入口为 `assets/index-DyGszm7H.js`，道氏列表分包为
  `assets/DowMonitor-Ifxr-9VP.js`，实时分包为
  `assets/realtimeMarketData-CEPvC75R.js`；本地与容器 SHA-256 分别一致为
  `dad86cf9b4a765dbe50ac256912c7dbf7699bb247f818a567e12555c9725d4d9`、
  `f79a0c39009299e3696d987b11fc9786643ffcb0a11ad30e16bce2fcd05fdf7a`、
  `a2e83dcb0386e4d384deeaf6d08c7527d3fe305a09ad2830d69d2de34493ba90`。
- 3018 健康检查返回 `0.1.86`，容器 `running`、重启次数 0，发布日志无错误。
- 发布前后监控股票文件和 `/api/dow-monitor/symbols` 响应分别逐字节一致，仍为
  13 只（A 股 1、港股 5、美股 7）。
- 生产 WebSocket 五个港股样本已使用正确昨收；例如 `1347.HK` 实时为约 `-1.88%`，
  `1888.HK` 为约 `-7.29%`，不再使用旧基线得到约 `-9.76%` 和 `-24.39%`。
