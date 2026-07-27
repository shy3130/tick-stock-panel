# Dow Monitor Symbol Identity Acceptance

状态：通过

## 生产证据

- 生产版本：`656eb399d9c4c91794ab4bcad7d150622657226b`
- 生产镜像：`tickflow-stock-panel-app:dow-monitor-656eb399d9c4`
- ClickHouse 当日标准代码：
  - `1347.HK` 有 1 个资金点，最新为 13:55。
  - `981.HK` 有 17 个资金点，最新为 15:05。
- `01347.HK` 在 15:41 生成新决策并成功关联 `1347.HK`；因资金超过
  15 分钟没有更新，状态为 `CAPITAL_DELAYED / 资金数据延迟`。支持理由
  只保留 15/30 分钟结构，延迟资金只进入风险项。
- `0981.HK` 在 15:41 生成新决策并成功关联 `981.HK`；状态为
  `COMPLETE / 数据完整`。
- 决策记录继续保留展示代码 `01347.HK`、`0981.HK`，历史决策存储按
  标准身份去重，没有产生重复股票或重复决策。

## 可执行证据

- 资金查询收到 `1347.HK`、`981.HK`，结果字典也以标准代码为键。
- overview 仍返回监控列表中的补零展示代码并能取得标准代码行情与资金。
- 数值零按有效资金处理，不会变成缺失。
- 无当日记录、超过15分钟延迟、窗口点数不足和完整资金分别映射为
  `CAPITAL_UNAVAILABLE`、`CAPITAL_DELAYED`、
  `CAPITAL_INSUFFICIENT` 和 `COMPLETE`。
