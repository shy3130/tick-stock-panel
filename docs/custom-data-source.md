# 自定义数据源接入

本项目默认使用 TickFlow。自定义数据源是一个可选扩展: 外部 HTTP 服务负责取数和整理, 本项目只把返回结果映射成内部标准字段, 然后复用现有存储、指标、enriched、策略和前端展示逻辑。

## 支持范围

当前自定义源支持七类数据:

| 数据集 | 配置名 | 说明 |
| --- | --- | --- |
| 日K | `daily` | 批量返回一组标的在指定区间内的日K。个股、指数与 ETF 的日K同步都会路由到生效源,并按契约传入 `asset_type`(`stock`/`index`/`etf`);仅支持股票的源(如 fuyao/tdxquant/amber)对其他资产类型返回空数据即可,该轮同步会告警并回退 TickFlow,不会写入错误数据 |
| 除权因子 | `adj_factor` | 批量返回一组股票的复权因子 |
| 实时行情 | `realtime` | 返回全市场快照,用于盘中 enriched 增量计算 |
| 分钟K | `minute` | 返回 1m 分钟K(需映射出 symbol / datetime / OHLC / 量; amount 成交额可缺省, 缺省时均价线降级显示 —) |
| 五档盘口 | `depth5` | 批量返回一组股票的买卖五档快照(逐批注入代码参数,无起止时间) |
| 全量分钟 | `full_minute` | 与 `minute` 同形;声明后可被路由为「全量分钟」生效源,内置服务盘中按当日窗口全市场批量落盘(仅修复轮语义,节奏下限 60s) |
| 财务数据 | `financial` | 一个配置覆盖全部五张财务表;请求时注入 `table` 参数由上游分发,五表按报告期累积落库(见下 `financial` 字段契约) |

`full_minute` 声明式源只提供修复轮(当日窗口批量);廉价增量端点
(`get_intraday_latest`)是 Python 插件契约,见
[plugin-development.md](./plugin-development.md)。

## 配置位置

把 YAML 放到运行数据目录下:

```text
data/data_sources/*.yaml
```

Dev 模式下，默认位置是项目根目录的 `data/`；Docker 部署中，项目的 `data/` 会挂载为容器内的 `/app/data`。可通过 `DATA_DIR` 覆盖。

修改 YAML 后可在「设置 -> 数据源」点击「重新加载」,或调用:

```bash
curl -X POST http://127.0.0.1:3018/api/settings/data-sources/reload
```

## 最小 YAML

```yaml
name: mock_source
display_name: "Mock 自定义数据源"
auth:
  type: none

datasets:
  daily:
    url: http://127.0.0.1:3021/daily
    method: POST
    batch: 100
    rpm: 200
    response_path: data
    field_map:
      ts_code: symbol
      trade_date: date
      open: open
      high: high
      low: low
      close: close
      vol: volume
      amt: amount
    transforms:
      date: "parse_date(value, '%Y-%m-%d')"

  adj_factor:
    url: http://127.0.0.1:3021/adj_factor
    method: POST
    batch: 100
    rpm: 200
    response_path: data
    field_map:
      ts_code: symbol
      trade_date: trade_date
      factor: ex_factor
    transforms:
      trade_date: "parse_date(value, '%Y-%m-%d')"

  realtime:
    url: http://127.0.0.1:3021/realtime
    method: GET
    rpm: 60
    response_path: data
    field_map:
      ts_code: symbol
      name: name
      last: last_price
      pre_close: prev_close
      open: open
      high: high
      low: low
      vol: volume
      amt: amount
      pct: change_pct
      amount_change: change_amount
      amplitude: amplitude
      turnover: turnover_rate
```

## 字段契约

### daily 必填

| 内部字段 | 含义 |
| --- | --- |
| `symbol` | 标准代码,如 `000001.SZ` |
| `date` | 交易日 |
| `open` / `high` / `low` / `close` | 不复权 OHLC |
| `volume` | 成交量 |
| `amount` | 成交额 |

### adj_factor 必填

| 内部字段 | 含义 |
| --- | --- |
| `symbol` | 标准代码 |
| `trade_date` | 除权日期 |
| `ex_factor` | 复权因子 |

可选明细列 (提供即落库, 缺省为空): `dividend` 每股现金分红、`bonus` 每股送转比例、
`allot`/`allot_price` 配股比例与配股价、`prev_close` 除权前收盘 — 供等差显示投影
与全精度因子链重建。

### realtime 必填

| 内部字段 | 含义 |
| --- | --- |
| `symbol` | 标准代码 |
| `last_price` | 最新价 |
| `prev_close` | 昨收 |
| `open` / `high` / `low` | 当日 OHLC |
| `volume` | 成交量 |

建议实时接口额外提供 `amount`、`change_pct`、`change_amount`、`amplitude`、`turnover_rate`、`name`。缺失时部分字段会由 pipeline 回算,但精度取决于可用输入。

`change_pct`、`amplitude`、`turnover_rate` 统一使用小数制,例如 `0.0366` 表示 `3.66%`。百分制单位必须在 realtime 数据集上**显式声明**,不做数值猜测(数值无法区分两种单位:`0.05` 既可能是 0.05% 也可能是 5%):

```yaml
datasets:
  realtime:
    url: https://api.example.com/snapshot
    pct_unit: percent   # 接口返回 3.66 表示 3.66%;小数制源声明 decimal 或省略
```

处理规则:

| 声明 | 行为 |
| --- | --- |
| `pct_unit: percent` | `change_pct` / `amplitude` / `turnover_rate` 无条件 `/100` |
| `pct_unit: decimal` | 三列原样透传 |
| 未声明 | `change_pct` 按截面中位数归一(A 股涨跌停 30% 上限使两种单位物理可分);`amplitude` / `turnover_rate` **置 `None`** 交由 pipeline 按价格与股本口径重算,并记录 WARNING |
| 列已配置 `transforms` | 视为用户已接管该列单位,原样透传 |

### depth5 必填

| 内部字段 | 含义 |
| --- | --- |
| `symbol` | 标准代码 |
| `asks` | 卖五档,嵌套数组 `[[价,量], ...]`,按卖一至卖五顺序 |
| `bids` | 买五档,嵌套数组 `[[价,量], ...]`,按买一至买五顺序 |

`asks` / `bids` 每档取前两个元素按数值解析为 `[价格, 手数]`,无法解析的档位被跳过;数量单位沿用现有封单量口径(手),前端计算封单额时再乘 100 换算为股。示例响应行:

```json
{ "code": "000001.SZ", "sell": [[10.01, 1200], [10.02, 800], [10.03, 500], [10.04, 300], [10.05, 200]],
  "buy":  [[10.00, 900], [9.99, 700], [9.98, 400], [9.97, 350], [9.96, 150]] }
```

对应映射 `code→symbol`、`sell→asks`、`buy→bids`。声明 depth5 后即可在设置 → 数据源 → 能力路由把五档生效源切到该自定义源;五档独立路由,失败不跨源回退。

### financial 必填

| 内部字段 | 含义 |
| --- | --- |
| `symbol` | 标准代码(唯一硬性要求,缺 symbol 的帧被丢弃) |
| `period_end` | 报告期 `YYYY-MM-DD`;缺此列的帧不参与报告期合并,页面按时间排序失效 |
| `announce_date` | 公告日期 `YYYY-MM-DD`;缺省时按报告期排序,换手率用公告日判业务可用性 |

一个 `financial` 配置覆盖全部五张表,每次请求自动注入 `table`
参数(`metrics` / `income` / `balance_sheet` / `cash_flow` / `shares`)
由上游分发返回对应表数据;`shares` 表额外注入 `latest`(当期 `True` /
全历史 `False`)。映射为「外部字段 → 内部列」一对一:同一外部字段名只能
映射到一个内部列 — 若两张表存在同名字段且含义不同(如 `total` 在
balance_sheet 是资产、在 shares 是股本),无法分别映射,需要上游区分字段名
或只映射其一。两个外部字段映射同一内部列会在保存时被校验拒绝。某批响应
里完全缺失(而非为 null)的字段该批不落该列,跨批/跨轮合并会自动补 null。

每表 canonical 列(前端财务页与回测基本面因子按此消费;不映射的列在
页面显示为 —,不影响其他列):

- `metrics` 核心指标: `eps_basic` / `eps_diluted` / `bps`(每股净资产) /
  `ocfps`(每股经营现金流) / `roe` / `roe_diluted` / `roa` /
  `gross_margin`(毛利率) / `net_margin`(净利率) / `debt_to_asset_ratio`(资产负债率) /
  `revenue_yoy`(营收同比) / `net_income_yoy`(净利同比) /
  `operating_cash_to_revenue`(经营现金/营收) / `inventory_turnover`(存货周转率)
  —— 注意: 百分比列存**百分点**(12.3 表示 12.3%),与 realtime 的小数制不同
- `income` 利润表: `revenue`(营收) / `operating_cost`(营业成本) /
  `operating_profit`(营业利润) / `selling_expense`(销售费用) /
  `admin_expense`(管理费用) / `rd_expense`(研发费用) /
  `financial_expense`(财务费用) / `non_operating_income`(营业外收入) /
  `non_operating_expense`(营业外支出) / `total_profit`(利润总额) /
  `income_tax`(所得税) / `net_income`(净利润) /
  `net_income_attributable`(归母净利) / `net_income_deducted`(扣非净利) /
  `basic_eps` / `diluted_eps` —— 金额单位元
- `balance_sheet` 资产负债表: `total_assets`(资产总计) /
  `total_current_assets`(流动资产) / `total_non_current_assets`(非流动资产) /
  `cash_and_equivalents`(货币资金) / `accounts_receivable`(应收账款) /
  `inventory`(存货) / `fixed_assets`(固定资产) / `intangible_assets`(无形资产) /
  `goodwill`(商誉) / `total_liabilities`(负债合计) /
  `total_current_liabilities`(流动负债) / `total_non_current_liabilities`(非流动负债) /
  `short_term_borrowing`(短期借款) / `long_term_borrowing`(长期借款) /
  `accounts_payable`(应付账款) / `total_equity`(所有者权益) /
  `equity_attributable`(归母权益) / `retained_earnings`(未分配利润) /
  `minority_interest`(少数股东权益) —— 金额单位元
- `cash_flow` 现金流量表: `net_operating_cash_flow`(经营净额) /
  `net_investing_cash_flow`(投资净额) / `net_financing_cash_flow`(筹资净额) /
  `capex`(资本开支) / `net_cash_change`(现金净增加) —— 金额单位元
- `shares` 股本: `total_shares`(总股本,股) / `float_shares`(流通股本,股) —
  历史换手率按公告日(缺则报告期)取有效股本,无 `float_shares` 时回退维表最新股本

被切为财务生效源后,财务同步走独立调度(每周 metrics + 手动全表),
报告期合并按 `(symbol, period_end)` 逐列取最新非空值:多源字段可并集
共存互不覆盖(如 TickFlow 补齐 metrics 历史各期,自定义源提供当期报表),
但切换生效源后**不再拉取的表不会自动清空**,旧数据会保留到被新数据覆盖。
声明了 financial 但上游某表无数据时返回空即可,已有存量靠合并写入保留。

## 请求约定

- `daily` / `adj_factor` 会按 `batch` 切分 symbols。
- POST 请求会发送 JSON body: `symbols`、`start_time`、`end_time`。
- GET 请求会发送 query 参数: `symbols=000001.SZ,600000.SH`。
- `realtime` 必须是全市场快照接口,不支持逐个 symbol 拉实时行情。
- `depth5` 会按 `batch` 切分 symbols 逐批请求,只注入代码参数(无 `start_time` / `end_time`)。
- `financial` 同样按 `batch` 切分逐批请求,只注入代码参数 + `table` 表名(无起止时间);`shares` 表额外注入 `latest`。

可通过这些字段改参数名:

```yaml
symbols_param: symbols
start_param: start_time
end_param: end_time
```

分钟数据源如果需要区分资产类型或周期，可继续配置：

```yaml
asset_type_param: asset_type
freq_param: period
```

配置后，分钟请求会分别传入 `stock` / `etf` / `index` 和 `1m`；留空时不向上游发送这两个参数，以兼容已有数据源。

### 请求超时

每个数据集可单独配置请求超时（秒），默认 30：

```yaml
timeout: 60
```

留空或省略时用默认 30 秒，可配置范围为大于 0 且不超过 300 秒；该值对数据同步与「试拉测试」均生效。在设置页编辑数据源时可在「超时」输入框修改（与 批量 / RPM / 响应路径 同行）。「试拉测试」直接使用当前表单内容，新建数据源或尚未保存的修改也可测试。

## 鉴权

支持三种简单鉴权:

```yaml
auth:
  type: bearer
  token_env: MY_DATA_TOKEN
```

```yaml
auth:
  type: header
  header: X-Token
  token_env: MY_DATA_TOKEN
```

```yaml
auth:
  type: query
  param: token
  token_env: MY_DATA_TOKEN
```

Token 可以放在系统环境变量或项目 `.env` 中。

## 联调流程

1. 启动 mock 数据源:

```bash
cd docs/examples/custom-data-source
python mock_server.py
```

2. 复制示例配置:

```bash
mkdir -p data/data_sources
cp docs/examples/custom-data-source/mock_source.yaml data/data_sources/mock_source.yaml
```

3. 在「设置 -> 数据源」点击「重新加载」。

4. 使用「试拉测试」选择 `mock_source` 和 `daily` / `adj_factor` / `realtime`。

5. 保存数据源选择:

- 日K: `mock_source`
- 除权因子: `mock_source` (或保持默认 `tickflow`)
- 实时行情: `mock_source`

6. 触发同步或开启实时行情。

## 常见错误

| 现象 | 处理 |
| --- | --- |
| 列表里没有 custom 源 | 检查 YAML 是否放在 `data/data_sources/` 并点击重新加载 |
| errors 提示 missing mapped fields | `field_map` 没映射到必填内部字段 |
| 试拉 rows 为 0 | 检查 `response_path` 是否指向数组 |
| 日期列全为空 | 检查 `parse_date` 的格式是否和返回值一致 |
| 实时行情没刷新 | 确认实时数据源已保存为 custom,且返回全市场快照 |

## 用 AI 生成映射配置

如果你的数据源 API 文档比较复杂,可以把 API 文档和返回示例丢给 AI,让它帮你生成 `field_map` 和 YAML 配置。

### 操作步骤

1. 从你的数据源获取 API 文档(接口地址、请求方式、返回字段说明)
2. 试拉一次,拿到返回的 JSON 示例
3. 把下面的 prompt 模板 + API 文档 + JSON 示例一起发给 AI
4. 把 AI 生成的 YAML 贴到 `data/data_sources/xxx.yaml`
5. 在设置页点「重新加载」,再「试拉测试」验证

### Prompt 模板

复制以下内容发给 AI(替换方括号部分):

```text
我在配置一个自定义数据源接入股票面板。请根据我的 API 文档和返回示例,生成 YAML 配置。

要求:
1. 输出标准 YAML 配置,包含 name / display_name / auth / datasets
2. 每个数据集的 field_map 把我的接口字段名映射到内部字段名
3. 日期类字段如果格式不是 YYYY-MM-DD, 加上 transforms 里的 parse_date
4. 只配置我能提供的接口, 不存在的数据集不要写

内部字段对照表:

日K (daily):
  symbol = 股票代码, 格式 000001.SZ / 600000.SH
  date = 交易日期
  open / high / low / close = OHLC
  volume = 成交量
  amount = 成交额

除权因子 (adj_factor):
  symbol = 股票代码
  trade_date = 除权日期
  ex_factor = 复权因子

实时行情 (realtime):
  symbol = 股票代码
  last_price = 最新价
  prev_close = 昨收价
  open / high / low = 当日 OHLC
  volume = 成交量
  amount = 成交额 (可缺省)
  change_pct = 涨跌幅 (小数, 0.0366 = 3.66%)
  change_amount = 涨跌额
  amplitude = 振幅 (小数, 0.024 = 2.4%)
  turnover_rate = 换手率 (小数, 0.05 = 5%)
  # 上游若返回百分数值 (3.66 表示 3.66%), 在 realtime 数据集声明 pct_unit: percent,
  # 不要依赖数值自动识别; 逐列转换也可用 transforms: turnover_rate: "value / 100"

分钟K (minute) 与 全量分钟 (full_minute, 字段同 minute):
  symbol = 股票代码
  # datetime 必须是北京时间墙钟 (如 2026-08-28 09:35:00), 不要返回 UTC;
  # 入口守卫会自动纠偏 UTC 特征帧, 但契约仍要求源头写对
  datetime = 北京时间墙钟 (YYYY-MM-DD HH:MM:SS)
  open / high / low / close = OHLC
  volume = 成交量
  amount = 成交额 (可缺省: 无法提供可靠成交额时不要映射, 前端均价线降级显示 —)

=== 我的 API 文档 ===
[把你的接口文档贴这里: URL / 请求方式 / 参数 / 返回字段说明]

=== 返回 JSON 示例 ===
[把试拉的 JSON 返回贴这里]
```

AI 会输出类似这样的结果:

```yaml
name: my_source
display_name: "我的数据源"
auth:
  type: bearer
  token_env: MY_API_TOKEN

datasets:
  daily:
    url: https://api.example.com/kline
    method: POST
    batch: 100
    rpm: 200
    response_path: data.list
    field_map:
      ts_code: symbol
      trade_date: date
      open: open
      vol: volume
    transforms:
      date: "parse_date(value, '%Y%m%d')"
```

把这段 YAML 保存为 `data/data_sources/my_source.yaml`,然后在设置页重新加载即可。
