# 自定义数据源接入

本项目默认使用 TickFlow。自定义数据源是一个可选扩展: 外部 HTTP 服务负责取数和整理, 本项目只把返回结果映射成内部标准字段, 然后复用现有存储、指标、enriched、策略和前端展示逻辑。

## 支持范围

当前自定义源支持三类数据:

| 数据集 | 配置名 | 说明 |
| --- | --- | --- |
| 日K | `daily` | 批量返回一组股票在指定区间内的日K |
| 除权因子 | `adj_factor` | 批量返回一组股票的复权因子 |
| 实时行情 | `realtime` | 返回全市场快照,用于盘中 enriched 增量计算 |

分钟K、财务、深度盘口暂时仍走 TickFlow。

## 配置位置

把 YAML 放到运行数据目录下:

```text
data/data_sources/*.yaml
```

在桌面版中,`data/` 位于程序目录旁;在开发环境中,通常是项目根目录的 `data/`。

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

### realtime 必填

| 内部字段 | 含义 |
| --- | --- |
| `symbol` | 标准代码 |
| `last_price` | 最新价 |
| `prev_close` | 昨收 |
| `open` / `high` / `low` | 当日 OHLC |
| `volume` | 成交量 |

建议实时接口额外提供 `amount`、`change_pct`、`change_amount`、`amplitude`、`turnover_rate`、`name`。缺失时部分字段会由 pipeline 回算,但精度取决于可用输入。

`change_pct` 和 `amplitude` 使用小数制,例如 `0.0366` 表示 `3.66%`。

## 请求约定

- `daily` / `adj_factor` 会按 `batch` 切分 symbols。
- POST 请求会发送 JSON body: `symbols`、`start_time`、`end_time`。
- GET 请求会发送 query 参数: `symbols=000001.SZ,600000.SH`。
- `realtime` 必须是全市场快照接口,不支持逐个 symbol 拉实时行情。

可通过这些字段改参数名:

```yaml
symbols_param: symbols
start_param: start_time
end_param: end_time
```

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
- 除权因子: `same_as_daily` 或 `mock_source`
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
  amount = 成交额
  change_pct = 涨跌幅 (小数, 0.0366 = 3.66%)
  change_amount = 涨跌额
  amplitude = 振幅
  turnover_rate = 换手率 (小数, 0.05 = 5%; 若上游返回 5 表示 5%, 配置 transforms: turnover_rate: "value / 100")

分钟K (minute):
  symbol = 股票代码
  datetime = 时间戳 (YYYY-MM-DD HH:MM:SS)
  open / high / low / close = OHLC
  volume = 成交量
  amount = 成交额

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

