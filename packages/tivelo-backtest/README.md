# Tivelo Backtest

独立回测包（`0.1.0a1`）。共享矩阵、兼容撮合与统计实现从 TSP 提取，不导入 `app.*`、数据引擎、供应商 SDK 或市场包。历史数据与信号由调用方显式提供；分钟取数通过回调注入。TSP 宿主使用方式见[普通回测与引擎研究](../../docs/engine-platform/workbench-research-jobs.md)。

`legacy` 保留 `legacy.tsp.v1` 的成交、浮点数值、仓位回退及连续曲线记账口径。缺分钟数据时的旧降级、取消后的部分结果和期末处理仍属兼容行为，不能作为严格新任务的成功输出。新的精确账户内核位于 `account`，严格日线参考入口位于 `daily`；CLI、参数网格和滚动验证均调用同一入口。工作台普通策略回测使用兼容接口。严格研究通过已准备的数据快照与回测归档进入，对输入覆盖和价格口径单独预检。

## 许可

**Tivelo 自有实现未经权利人单独书面授权不得商用。** 自有实现采用 [PolyForm Noncommercial License 1.0.0](LICENSE)，按许可条款允许的用途可免费使用。

此前已按 MIT 许可授出的材料保留原有权利和版权声明，见 [MIT 许可](LICENSE-MIT)和[许可说明](NOTICE)。

## 独立调用

```python
from tivelo_backtest.legacy.simulator import LegacySimulator, MatcherConfig, SimulationOptions
from tivelo_backtest.research.matrix import build_market_matrix

# panel 是已准备的 Polars 表，entries/exits 与表的行一一对应。
prepared = build_market_matrix(panel, entries, exits, entry_delay_bars=1, exit_delay_bars=1)
result = LegacySimulator().simulate_market_matrix(
    prepared, MatcherConfig(matching="open_t+1"),
    options=SimulationOptions(include_monte_carlo=False),
)
```

共享编码按实际存在的 bar 延迟信号，生成只读 NumPy 矩阵，可用于多次参数试验。证券、数量、手续费仍沿用旧撮合口径，并非完整多市场规则执行器。请求 `price_limit_pct` 字段时必须向 `build_market_data_matrix` 显式注入市场规则函数。

`LegacySimulator(minute_loader=...)` 接收 `(symbols, dates, asset_type)`，返回以 `(symbol, YYYY-MM-DD)` 为键的数组。旧数组列顺序为 open/high/low/close/volume/amount，volume 单位为手（100 股）、amount 为货币金额；v2 的股数量不得直接当成这个旧载荷。回调在执行前批量调用，逐 bar 撮合不访问网络或仓库。

## 独立研究计算与旧因子定义

`research.catalog`、`dsl`、`py_expr`、`factor_definitions`、`scoring` 复用原有因子目录、公式语法、依赖/预热推导和数值计算。`FactorCatalog` 是不可变快照；默认目录不包含宿主用户因子，不读取配置或文件。`load_factor_definitions` 接受调用方提供的旧 JSON 定义，支持按依赖加载无序的 DSL、Python 表达式和组合因子；重复 ID、未解决的 DSL 引用及循环依赖失败，不修改原目录。

```python
import polars as pl
from tivelo_backtest.research.factor_definitions import load_factor_definitions
from tivelo_backtest.research.scoring import materialize_scoring_columns

catalog = load_factor_definitions([
    {"id": "uf_mean", "label": "两期均价", "formula": "ts_mean(close, 2)"},
])
frame = pl.DataFrame({
    "symbol": ["A", "A", "A"], "date": [1, 2, 3], "close": [10.0, 12.0, 14.0],
}).sort(["symbol", "date"])
result = materialize_scoring_columns(frame, ["uf_mean"], catalog=catalog)
assert result["uf_mean"].to_list() == [None, 11.0, 13.0]
```

计算接受已按 `symbol/date` 排序的 v1 enriched 表；`volume` 仍为手，`turnover_rate` 保持原百分数口径，`raw_close` 用于需要实际成交价的量价指标。调用方必须明确准备字段、价格基准和历史范围，不能把 v2 股数量直接传入这些旧算子。中间因子与窗口临时列在结果中清理；原来已经存在的列保留。缺少输入或无效表达式沿用旧物化路径的跳过语义，严格执行准入另行判断。

### 独立运行旧因子排名策略

TSP 挖掘发布的因子排名策略可在独立副本中把类的导入替换为 `tivelo_backtest.legacy.factor_rank.FactorRankResearchMatrixStrategy`，沿用原 `SCORING`、`DIRECTIONS` 和策略参数。先在固定价格口径及预热区间准备原策略用到的字段。TSP 内运行仍通过宿主获取注册因子和决策日价格尺度。

```python
from tivelo_backtest.legacy.factor_rank import FactorRankResearchMatrixStrategy
from tivelo_backtest.legacy.simulator import LegacySimulator, MatcherConfig
from tivelo_backtest.research.matrix import (
    build_market_data_matrix, build_market_matrix_from_signals,
)
from tivelo_backtest.research.scoring import apply_matrix_selection

strategy = FactorRankResearchMatrixStrategy(SCORING, DIRECTIONS)
market = build_market_data_matrix(panel, field_columns=set(SCORING))
signals = apply_matrix_selection(
    market,
    strategy.compute_signals(market, {"entry_score": 70, "exit_score": 40, "top_rank": 1}),
)
orders = build_market_matrix_from_signals(market, signals, entry_delay_bars=1, exit_delay_bars=1)
result = LegacySimulator().simulate_market_matrix(orders, MatcherConfig(matching="open_t+1"))
```

例子使用旧策略的次日开盘模式。与原任务逐笔对照时，沿用原撮合参数、交易限制列和价格基准。有额外候选范围时，传入与行情矩阵同形的布尔遮罩作为 `candidates`。框架级评分配置可通过 `scoring`、`order_by`、`descending` 和 `directions` 传入。已物化的评分列保留旧 `Float32` 截面归一化、缺值和同分处理，缺少评分字段时由独立库报错。

缓存默认关闭。需要复用内容缓存时，显式传入实现 `MaterializationCache` 的对象；缓存键必须包含公式、版本、直接输入值与分组列。TSP 适配器继续使用原注册版本编译缓存和磁盘内容缓存，预览目录不污染进程缓存。`with_factors` 更新返回新目录，替换条目必须提升版本；修改子因子后，依赖和预热按当前目录重新推导。

Python 表达式仍是可信用户代码，语法限制不等于安全沙箱；`FactorSpec.scale_free` 和编译元数据不构成严格尺度/时点证明。受控策略现有下述独立 JSON 入口，不自动接纳旧 Python 因子。旧策略完整适配、更多 v2 字段转换和优化器接入仍需完成。

## 独立信号与盘中特征

`research.signals` 复用原有日线条件、叠加过滤、昨日值映射、分钟交叉和上升沿算法。`SignalFields` 显式提供允许字段及字符串字段；默认只包含内置目录，不发现宿主扩展或用户文件。兼容 `build_expressions` / `inject` 保持单个失败跳过并记录日志的语义，原有 TSP 路径通过薄适配器传入当前字段快照。

需要全部条件成功求值时，使用独立的 `compile_daily_signals`。它接受最多 256 个旧格式日线定义，每个最多 8 条 AND 条件，固定因子目录并预编译所需 DSL/Python 因子。调用方读取明确选择的 JSON，再传入定义；计算过程不读取文件或全局偏好。

```python
from tivelo_backtest.research.signal_plan import compile_daily_signals

plan = compile_daily_signals([{
    "id": "rising", "name": "收盘上涨", "kind": "entry",
    "conditions": [{"left": "close", "op": ">", "right": "field:close", "rightDays": 1}],
}])
assert plan.warmup_bars == 2
# frame 使用上例按 symbol/date 排序、主键唯一的研究表。
assert plan.evaluate(frame)["csg_rising"].to_list() == [False, True, True]
```

计划拒绝重复 ID/输出、覆盖输入列、非整数偏移、非有限常量、未知字段和不支持的周期。执行时拒绝缺列、重复或未排序主键、输入类型冲突与非有限计算结果；有值缺失的条件按 false 输出，不发布缺少部分信号列的成功结果。已经生成的输出列不能作为该计划的输入重新覆盖。`dependencies` 为只读根字段映射；`warmup_bars` 累计因子与条件偏移，供消费者安排预热查询，不自动改变交易区间。字符串扩展必须通过 `string_fields` 显式声明，`contains` 始终按字面量匹配。

`research.intraday.build_feature_frame(minute_df, timezone=ZoneInfo(...), session_expr=...)` 显式接收时区和会话分组表达式。表达式在转换后的本地墙钟上执行，0 表示开盘时段；TSP 适配器仍使用北京时区和原 13:00 午休分界。数量保持手、VWAP 仍除以 100，开盘范围仍使用 30 根已观测 bar；覆盖完整性由调用方提供。`apply_intraday_edges` 按标的/交易日取 false→true 上升沿，首根不触发。可配置时区与分组不等于已认证港美市场、夜盘交易日或严格分钟撮合。

这些计划是研究计算接口，不是价格尺度或时点安全证明。任意 Python 因子的可信代码边界保持原约定；可信策略可读取归档内明确声明的 `factors.json`、`signals.json` 来调用计划。它们本身不能获得未来锚点准入；下述受控 JSON 程序由核心独立验证整个决策链。

## 独立读取便携快照

```python
from tivelo_backtest.snapshot import PortableSnapshotReader

with PortableSnapshotReader("./portable-snapshot", snapshot_id=expected_snapshot_id) as reader:
    records = reader.bars(query)
    result = runner.run(run_lock, reader, strategy_binding)
```

读器接受公开 `SnapshotBundle`，不需要安装 `tivelo-data`、DuckDB 或 PyArrow，也不读取 Data 的索引。它校验清单引用、内容寻址路径、文件摘要、行数、UTC/十进制物理类型、必填空值及来源/价格基准；拒绝符号链接和缺少发布清单的目录。清单最多 16 MiB，事实大小由清单逐一核对。查询期间改变事实文件会失败，不静默接受新内容。

历史查询先按 `available_at` 选择可知的最新修订，检查冲突后再处理有效期、取消、未完成状态；不会让旧版本重新出现。支持 `bars`、`openings`、`instruments`、`actions`、`factors` 以及历史代码解析。文件校验结果按文件身份缓存，首次验证会扫描必要字段。`iter_bars` 可在读器上下文中逐条消费同样的已验证行情，直接读取原价或已复权日线时兼容回测用它构建面板。

串行多次试验可先调用 `reader.prepare(max_bytes=256 * 1024 * 1024, observer=...)`。它用同步列式批处理加载已验证事实，所有试验共享私有只读缓冲区；每次查询仍执行原来的可知时间、历史修订和有效期过滤，不缓存全量历史查询结果。取消、字节超限或文件变化均不发布部分准备结果，`close()` 释放缓冲区。`prepared_fact_bytes` 是保留的 Polars 列缓冲区估算，不包含解码瞬时内存、Python 对象及进程其他开销；操作系统 RSS 硬限制需工作进程另行实现。

### 从快照读取兼容回测的日线

`read_legacy_daily_panel` 将旧库导入后的日线快照转换成兼容回测使用的 Polars 表。日期直接取行情记录，成交量由股换回手，成交额保留人民币元。先以 `BarQuery` 指定标的、范围、价格口径和快照可知时间。通过 `symbols` 可把快照标的 ID 映射回已有策略所用的股票代码。

```python
from tivelo_backtest.legacy.snapshot_daily import read_legacy_daily_panel
from tivelo_backtest.snapshot import PortableSnapshotReader

with PortableSnapshotReader(bundle_dir) as reader:
    panel = read_legacy_daily_panel(reader, query, symbols={"security-A": "600000.SH"})
```

同一份原价快照可传入带固定锚点的 `PriceViewSpec`，由现有免费价格视图计算前复权或后复权。兼容面板直接取该投影的数值列，保留原价列与成交量。旧库的 `latest_reconstruction` 输入适用于兼容回测，严格时点回放仍按其原有证据要求。策略所需的原有因子和成交限制列可沿用已有准备流程。[旧因子排名用法](#独立运行旧因子排名策略) · [旧库导入](../tivelo-data/README.md#从既有-tsp-日线仓库导入)

### 独立运行旧策略的稳健参数优化

把上述快照日线及策略所需的特征准备为 `MarketDataMatrix`，再传入可信的旧矩阵策略和 `MatcherConfig`。`optimize_daily_matrix` 与 TSP 的稳健优化入口共用参数网格校验、分段计分、排名及全窗复核。每个区间按自然日期划分，行情切片和预热按实际行情日期推进。

已有单文件日线 `matrix_native` 策略可以直接封存为独立副本。`seal-legacy-matrix` 适配共用矩阵算子的导入路径，保留原策略的实现和 `META` 参数默认值。工作台外层的选股过滤、评分和行情字段准备仍需沿用对应口径。使用其他 TSP 服务或同目录辅助文件的策略需要先整理自己的独立依赖。

```sh
tivelo-backtest seal-legacy-matrix --source ./my-strategy.py --version 1 --output ./strategy
```

`./strategy` 可直接传给 `legacy-robust --definition`。共享指标算子使用已安装的 Numba 或现有 Python 路径，独立兼容优化会把当前环境的 Numba 版本记录到运行锁中。

已有多个 Python 文件时，可以沿用通用 `seal`。在独立副本中改用 `tivelo_backtest.research.matrix_features` 里的共用算子，通过相对导入引用辅助文件，并把所有本地模块列入封存。例如在 `./strategy/factory.py` 接入同目录策略。

```python
from tivelo_backtest.legacy.matrix_source import bind_matrix_strategy
from .strategy import META, MATRIX_STRATEGY

def build(*, parameters_json, dependencies):
    return bind_matrix_strategy(MATRIX_STRATEGY, META, parameters_json)
```

```sh
tivelo-backtest seal --root ./strategy --id my-matrix-strategy --version 1 \
  --entrypoint factory:build --file factory.py --file strategy.py --file indicators.py
```

旧日线 `polars_expr` 和 `python_history_legacy` 策略可以用 `seal-legacy-polars` 生成同一种独立定义，再交给 `legacy-robust`。它沿用策略文件中的 `filter(panel, params)` 或 `filter_history(panel, params)`、`META` 默认值、`ENTRY_SIGNALS` 和 `EXIT_SIGNALS`。历史窗口策略返回的 `symbol`、`date` 键对齐到原行情，重复键只产生一次候选。预热沿用策略的 `LOOKBACK_DAYS`，按实际日线计数。筛选和因子权重按原长表精度计算，买点在候选评分后应用，卖点独立判断。

```sh
tivelo-backtest seal-legacy-polars --source ./my-polars-strategy.py --version 1 --output ./strategy
```

Python 调用可用 `optimize_daily_panel` 直接传入已经物化的日线面板。快照调用通过顶层 `extension_fields` 绑定所需指标和信号，例如 `ma20`、`signal_ma20_breakdown`。使用与原工作台同一口径的预计算值，为每个字段提供数据集 ID 和字段摘要。行情 OHLC、成交量、成交额、代码和日期使用快照的原生字段。在 `field_columns` 中填写 `name` 可读取各行情日指定时点已知的证券名称。策略声明的输入或买卖触发列缺失时，优化会直接返回 `DATA_COVERAGE_INSUFFICIENT`。请求 `selection` 可载入用户实际配置的筛选和因子权重。

旧工作台的日线叠加策略使用 `seal-legacy-composite`，子策略须为日线矩阵策略。先封存各子策略，并从子策略目录里的 `definition.json` 用 `DefinitionBundle.model_validate_json(...).reference(parameters_json)` 生成引用 JSON。引用记录子策略 ID、版本、摘要和固定参数。将各子策略目录放在 `./children/<sha256>/`。

```sh
tivelo-backtest seal-legacy-composite --source ./my-composite.py --version 1 \
  --dependency ./first-child-ref.json --dependency ./second-child-ref.json --output ./composite
tivelo-backtest legacy-robust --request ./optimize.json --definition ./composite \
  --dependencies ./children --snapshot ./snapshot --output ./study
```

父策略统一应用 `selection.basic_filter`，省略整个 `selection` 时用策略文件和工作台的默认筛选。快照须提供筛选所需的股本和风险警示等历史字段。子策略各自按定义排序，可在父策略的 `strategy_parameters_json` 中以 `child_overrides` 按子策略 ID 传入 `params`、`scoring`、`scoring_replace` 和 `scoring_directions`。已有配置需要保留因子顺序时，用 `scoring_order` 列出该子策略的全部有效因子。可用 `merge_mode` 和 `min_confirm` 做参数网格，`matcher.max_hold_days` 同时约束卖点归属窗口和实际持仓期。归档包含子策略定义和引用，复算直接使用归档和原快照。

```python
from datetime import date

from tivelo_backtest.legacy.daily_optimization import optimize_daily_matrix
from tivelo_backtest.legacy.factor_rank import FactorRankResearchMatrixStrategy
from tivelo_backtest.legacy.simulator import MatcherConfig
from tivelo_backtest.research.matrix import build_market_data_matrix

market = build_market_data_matrix(panel, field_columns={"amount"})
result = optimize_daily_matrix(
    market,
    FactorRankResearchMatrixStrategy({"amount": 1.0}),
    MatcherConfig(matching="open_t+1", max_positions=2),
    start=date(2019, 1, 2),
    end=date(2019, 3, 29),
    params_meta=[{"id": "top_rank", "type": "int", "min": 1, "max": 3}],
    param_grid={"top_rank": [1, 2, 3]},
    base_params={"entry_score": 50, "exit_score": 40},
    objective="total_return",
    k_segments=2,
    min_trades_total=1,
    stage2_top_n=2,
)
```

回测输入可从原价或已复权日线构造。准备与旧入口一致的特征、筛选和撮合设定后，再比较 `windows`、`results` 和 `best_params`。需要预热的策略可使行情起点早于 `start`。该入口接受能直接产出矩阵信号的策略，并使用兼容日线撮合口径。

用命令行运行时，先把可调用的日线矩阵策略封存为可信定义。以下 `strategy.py` 放在 `./strategy`，其他能实现 `compute_signals` 和 `required_warmup_bars` 的矩阵策略也可由同一工厂签名返回。

```python
from tivelo_backtest.legacy.factor_rank import FactorRankResearchMatrixStrategy

def build(*, parameters_json, dependencies):
    return FactorRankResearchMatrixStrategy({"amount": 1.0})
```

请求 JSON 的 `query` 使用 `BarQuery` 的标的、起止时间、价格口径及固定 `as_of`。顶层 `start` 和 `end` 是优化的自然日期范围，其余与 Python 示例的参数同名。`matcher` 接收兼容日线的撮合、费用和仓位设置。对已经前复权的快照可直接用 `query.price_basis="qfq"`。从同一原价快照派生前复权或后复权时，在顶层提供相应的固定 `price_view`。

在请求中加入 `selection`，可以复用 TSP 的入场筛选和同日评分。以下配置按价格区间、成交额和板块选出候选，再按动量从高到低、成交额从低到高加权评分。切片预热取用已有的 K 线。

```json
{
  "selection": {
    "basic_filter": {"price_min": 3, "price_max": 300, "amount_min": 20000000, "boards": ["沪主板", "深主板", "创业板", "科创板", "北交所"]},
    "scoring": {"momentum_20d": 0.6, "amount": 0.4},
    "scoring_directions": {"amount": "low"}
  }
}
```

板块筛选使用 TSP 的证券代码。如果快照用独立标的 ID，通过顶层 `symbols` 映射为 `600000.SH`、`920000.BJ` 等代码。启用成交额上下限时，快照需要提供成交额。单字段排序也可设置 `order_by` 和 `descending`。只用策略原始信号时可以省略 `selection`。

市值、流通市值、换手率和 ST 筛选使用同一份快照中的历史字段。顶层 `extension_fields` 将矩阵列名指向数据引擎收据中的 `field_reference`。每个引用包含 `dataset_id` 和 `field_sha256`，对应的数据版本需发布到该快照。

| `selection.basic_filter` 配置 | 快照输入 |
| --- | --- |
| `market_cap_min` / `market_cap_max` | 把总股本引用绑定为 `total_shares`，单位 `share` |
| `float_cap_min` / `float_cap_max` | 把流通股本引用绑定为 `float_shares`，单位 `share` |
| `turnover_min` / `turnover_max` | 把换手率引用绑定为 `turnover_rate`，单位 `percent`。也可用 `float_shares` 按成交股数计算，例如 `5` 表示 `5%` |
| `exclude_st` | `true` 时读取快照里各日收盘时可知的证券名称，沿用 TSP 的 ST、退市名称识别 |

也可绑定 `ext_*` 数值或布尔字段，直接在矩阵策略、`scoring` 因子权重和 `order_by` 排序中使用。布尔值以 `0`、`1` 进入矩阵。字段默认按 K 线记录的 `bar_end` 读取。旧库的按日归档可能把它记作次日零点，如需按 A 股常规收盘时间取补充字段，可在请求顶层设置 `"field_cutoff_time": "15:00:00+08:00"`。配置时点必须落在当根 K 线的时间范围内，每个日期只使用该时点已知的有效记录。某行情日所需筛选字段全缺时返回 `DATA_COVERAGE_INSUFFICIENT`，单只证券缺值时跳过该条件下的入场。

Python 入口另接受 `selection=MatrixPipelineConfig(...)`。调用者可直接使用已准备好的矩阵。构造矩阵时通过 `field_columns` 保留需要的数值列。

```sh
tivelo-backtest legacy-robust-schema > request-schema.json
tivelo-backtest seal --root ./strategy --id factor-rank --version 1 \
  --entrypoint strategy:build --file strategy.py
tivelo-backtest legacy-robust --request request.json --definition ./strategy \
  --snapshot ./portable-snapshot --output ./optimization
tivelo-backtest inspect --kind legacy-robust --archive ./optimization
tivelo-backtest legacy-robust-replay --archive ./optimization \
  --snapshot ./portable-snapshot --output ./replayed
```

归档保存完整参数、快照 ID、运行环境、封存的策略与依赖和结果，报告缺失指标以 JSON `null` 表示。`inspect` 只读验证文件。运行和复算会执行封存的可信 Python。复算要求原快照和锁定运行环境，清单与原报告一致后才发布。

### 独立运行旧分钟策略

已有工作台分钟策略可用 `seal-legacy-minute` 封存。命令行沿用 `legacy-robust` 请求，顶层 `query` 为包含预热的原价日线查询。增加 `minute.query` 读取同一快照中的原价分钟 K，两份查询使用同一标的集合及 `as_of`。按源数据选择 `minute.minute_label` 的 `start` 或 `end`。

```sh
tivelo-backtest seal-legacy-minute --source ./my-minute-strategy.py --version 1 --output ./strategy
tivelo-backtest legacy-robust --request ./optimize-minute.json --definition ./strategy \
  --snapshot ./snapshot --output ./optimization
tivelo-backtest inspect --kind legacy-robust --archive ./optimization
tivelo-backtest legacy-robust-replay --archive ./optimization --snapshot ./snapshot --output ./reproduced
```

安装 `tivelo-market-cn` 即可使用默认 A 股分钟涨停规则。可在 `minute.market` 指定其他已安装适配器的 `adapter_id`、`distribution` 和 `configuration_json`。归档同时锁定适配器配置与运行包摘要。默认 `minute.mode` 为 `position`，设置为 `full` 时逐候选撮合，使用已锁定的日线继续处理信号窗口之后的离场。和工作台一样，尾部由 `matcher.max_hold_days` 或 `minute.holding_days` 确定。

从 `legacy-robust-schema` 获取完整请求。日线可用 `price_view` 固定前复权或后复权，分钟 K 继续使用成交日原价。日线策略条件及撮合用到的 `signal_limit_up`、`signal_limit_down` 或 `signal_broken_limit_up` 可以通过顶层 `extension_fields` 绑定到历史快照。报告的 `results` 为各参数组排名，`best_backtest` 包含最优参数的成交、净值、回撤和跳过的行情日。归档可用冻结的快照和环境复算。

### 接入盘中信号

分钟策略使用 `REQUIRED_FEATURES = ["csgi_open_rise"]` 声明依赖。把工作台保存的盘中信号 JSON 放到 `minute.signal_definitions` 中，独立引擎会对快照分钟 K 使用工作台的特征和上升沿计算。配置随运行锁保存，可以按原信号定义复算。

```json
{
  "signal_definitions": [
    {
      "id": "open_rise",
      "name": "相对开盘上穿",
      "kind": "entry",
      "timeframe": "intraday",
      "conditions": [{"left": "pct_from_open", "op": "cross_up", "right": "0.004"}]
    }
  ]
}
```

数据源已保存历史盘中信号时，将该布尔字段加入同一快照，并在 `minute.signal_fields.csgi_open_rise` 中填写其 `dataset_id` 和 `field_sha256`。字段须声明为证券级 `bool`，`time_basis` 为 `minute_bar_end`，并保留每根实有 K 线结束时已可见的真值或假值。迟到的观测和修订不会替换该时刻的值。缺少某根行情的可见信号会返回 `DATA_COVERAGE_INSUFFICIENT`。同名信号使用唯一来源。[数据字段发布](../tivelo-data/README.md#将-tsp-扩展表作为历史研究输入)

Python 可通过 `DefinitionLoader.load_trusted_legacy_minute` 和 `run_legacy_minute` 运行同一内核。

```python
from tivelo_backtest.definitions import DefinitionLoader
from tivelo_backtest.legacy.minute_strategy import run_legacy_minute
from tivelo_backtest.legacy.simulator import MatcherConfig
from tivelo_backtest.legacy.snapshot_minutes import read_legacy_minute_frame

# reader 是打开的快照读器。minute_query_for 返回该行情日的 1m 原价 BarQuery。
def load_day(day):
    return read_legacy_minute_frame(reader, minute_query_for(day), minute_label="end")

loader = DefinitionLoader()
with loader.load_trusted_legacy_minute(loader.inspect("./strategy")) as strategy:
    result = run_legacy_minute(
        daily_panel, strategy, MatcherConfig(exit_fill="open_t+1", max_hold_days=5),
        start=start, end=end, minute_days=observed_minute_dates, load_day=load_day,
        limit_for=limit_for,
    )
```

`minute_days` 来自实际分钟分区，`load_day` 按日期提供原价分钟 K。选择与原策略一致的起始或结束时间标签，快照读器将标签还原为北京时间墙钟，股数换算为手。需要上下文时，`daily_panel` 的日线必须包含回测前的窗口、撮合期间的行情及策略引用的历史字段。策略获取的日线始终截至前一个实际行情日。若已准备固定前复权或后复权日线，保留 `raw_close` 供复权折算。自定义盘中信号可随 `load_day` 提供。`limit_for(symbol, day, name)` 由调用方绑定市场规则并返回涨停比例。买入使用触发分钟收盘，离场沿用兼容日线撮合。成交明细在 `result.simulation.trades`，触发分钟在 `result.trigger_times`，缺分钟数据的日线日期记录在 `result.replay.skipped_days`。

### 从快照读取兼容回测的分钟成交数据

`LegacySnapshotMinutes` 将一份只读快照接入 `LegacySimulator` 的分钟取数回调。每个显示代码显式绑定快照标的 ID 和市场日历。只在策略指定的成交日期取数，同一日历的证券共用时间网格。以下示例的 `calendar` 为覆盖研究区间的 `CNCalendar`，`observed_at` 为本次研究固定的信息截止时刻。

```python
from tivelo_backtest.legacy.simulator import LegacySimulator, MatcherConfig
from tivelo_backtest.legacy.snapshot_minutes import LegacySnapshotMinutes
from tivelo_backtest.snapshot import PortableSnapshotReader

with PortableSnapshotReader(bundle_dir) as reader:
    minutes = LegacySnapshotMinutes(reader, {"600000.SH": ("security-A", calendar)}, as_of=observed_at)
    matcher = LegacySimulator(minute_loader=minutes)
    result = matcher.simulate_portfolio(panel, entry_signals, exit_signals,
                                        MatcherConfig(matching="open_t+1", minute_fill=True))
```

适配器逐根核对交易日、可见时刻、人民币原价、数量、成交额和完整时段。随后将股数换回旧撮合器要求的手数。某个成交日缺分钟或成交额时会返回覆盖不足。快照覆盖表中其他日期有缺口时，数据齐全的指定成交日仍可独立回放。此处按兼容撮合的穿越价或日内成交量加权价格进行研究。旧归档重建数据的回放时刻须晚于导入观察。[工作台与独立回测数值用例](../../backend/tests/test_engine_repository_minute.py)

## 免费固定复权视图

`StaticPriceView("raw" | "qfq" | "hfq")` 实现公开 `PriceViewProvider`，接收 v2 原始 `Bar`、带可知时间的 `AdjustmentFactor`、固定锚点 `PriceViewSpec` 和决策时刻。只支持已声明的 `cumulative_forward` 乘法因子，不猜测因子定义或缺失基准。后复权基准不随 warmup 改变；未公开修订、未生效事件不进入计算。跨 bar 内部的因子变更拒绝，避免一根 OHLC 混用两个尺度。

派生价格以局部十进制上下文计算，保留 12 位小数、half-even 舍入；溢出和舍入到零明确失败。量和成交额保留原始单位，VWAP 随价格变换。派生记录携带输入及因子摘要形成的版本标识与实际可知时间。交易最小价位舍入仍属于撮合规则。

每个任务创建独立视图对象。`price(...)` 发出与最近一次成功投影绑定的价格尺度，`to_raw(...)` 验证标的、币种、模式、锚点与决策时刻，并使用完整因子比转换。该通用投影器对未来锚点返回 `PRICE_BASIS_UNPROVEN`；有界受控程序可走下述独立的锚点消去路径，不能把它用于任意 Python 或价格输出。

TSP 原有价格计算保留在宿主兼容入口。Tivelo 的专业动态投影由独立的私有原生制品提供。公开研究计算接受准备好的矩阵和尺度元数据。

## 精确现金股票账户

`account.CashEquityAccount(currency, initial_capital, order_expiry=..., dividend_treatment=...)` 实现 `event_ledger` 数值内核。它接收契约中的历史证券状态、`ExecutionRules`、原始开盘价和明确的交易所参考价，不读取数据源、应用设置或具体市场包。

调用顺序是 `submit(signal, quantity, eligible_at=...)`、`match_open(...)`、`snapshot(...) / mark_to_market(...)`、`finish(...)`。日历调度由上层负责：`eligible_at` 必须严格晚于决策时刻，不能用当前收盘信号在当前收盘成交；下一时段订单只在指定时刻尝试，GTC 订单保留原决策时间。上层必须确保提供的开盘报价属于当前事件，并禁止把完整日线的未来字段传给策略。

- 数量在提交时确定，执行时检查有效申报单位、现金和可卖数量。开盘跳空导致现金不足时拒绝该次成交，不偷偷缩量或产生负现金。
- `Signal.limit_price` 决定 `limit` / `market` 类型。配置有 `order_quantity_rules` 时，日线仓位计算和执行时复核使用同一类型的下限、步长、上限与零股政策；历史规则在执行时收紧会阻止原订单，不重新缩量。明确禁用的类型产生 `unsupported_order_type`；规则提供者缺少 `OrderExecutionRules` 实现则失败，不能忽略新字段。
- `remainder_once` 允许全部零股单独或随整手卖出，核心独立复核剩余数量是否整手以及请求、单笔上限和可卖数量。旧 `all_remaining` 仍只允许清空可卖余额时豁免网格。卖出量超过可卖余额时记录 `insufficient_sellable_quantity`，余额足够但数量不合规则记录 `invalid_quantity`；扩展返回违反声明规则的数量会使运行失败。
- 先卖后买；同分候选按标的 ID 稳定排序。按市场最小价位对滑点向不利方向舍入，保守阻止涨停买入和跌停卖出。当前模型不承诺排队成交或部分成交，也未模拟盘口容量。
- 限价必须由发出该价格尺度的 `PriceViewProvider` 转为原价。成交、费用和估值始终使用原价；复权图价不能再次计入现金收益。
- 原价限价在执行时核对最小价位和已知涨跌幅边界，分别记录 `invalid_limit_tick` 或 `limit_price_out_of_bounds`。要求盘中价格笼子验证的市场配置需要申报时点的原价盘口，日线准备明确返回 `UNSUPPORTED_CAPABILITY`。[沪深连续竞价价格校验](../tivelo-market-cn/README.md#连续竞价申报价格)
- 每批买入分别保存可卖时间；卖出消耗已解锁批次，按 FIFO 分摊成本，剩余成本使用 12 位 half-even 舍入。卖出所得可用于再投资，但此模型不模拟现金提现交收、融资、借券、期货或多币种结算。
- 拆分改变数量、不改变总成本；零碎股需要另一个现金补偿政策，目前明确拒绝。现金分红须先记录登记日权益，除息时计应收、到账时转现金；重复事件幂等，已处理事件发生修订时拒绝重新入账。
- 分红默认拒绝；显式选择 `gross_before_personal_tax` 才计算税前股息，并在诊断中保留该口径。不能把该结果当成按个人持有期完成代扣税的净收益。配股等未实现事件明确失败。
- 公司行为取消尚未成交的旧价格/数量指令。结束或取消时终止挂单、保留真实持仓，不捏造期末平仓。持仓估值缺价格时失败。

`orders` 保留状态事件、决策/提交/可执行/成交/事件时刻、委托类型、原价限价、原始成交价和分项费用；同一订单的类型和限价不会在状态流转时变化。`ledger` 保存现金、数量及应收变化。`reference` 将不可变 `CashEquityPolicy` 的失效、分红、成本和成交政策指纹提供给 `RunLock`；初始资本单独属于运行请求。

仓位计算中，预算除以价格可能产生循环小数。传给规则前先在所选类型的上限内向下取至公共数量精度 12 位，再按实际数量网格量化并预留费用；由于所有合法网格都能精确表示为 decimal128(38,12)，这个边界处理不会删除预算内的合法数量。提供者返回超额、超限或不符合声明网格的数量时明确失败。该路径不模拟完整交易所报单、盘中价格笼子或累计买入上限。

该内核已通过下述参考运行器接入快照读器、覆盖预检和日线时钟。账户及合成集成测试不能代替真实市场数据认证，也不代表 P4-02 的所有账户事件已完成。

### 连续竞价限价委托受理

使用 `ContinuousLimitAdmission` 把申报时所见的原价盘口接到账户。示例中的 `rules` 由沪深市场包的 v4 profile 创建，`book_dataset` 从已发布快照清单获取。传入申报时生效且当时已知的 `instrument` 和交易所参考价，限价使用原来签发价格尺度的 `price_view` 转回原价。

```python
from decimal import Decimal

from tivelo_backtest.account import CashEquityAccount
from tivelo_backtest.continuous import ContinuousLimitAdmission

account = CashEquityAccount("CNY", "10000", order_expiry="end_of_day")
admission = ContinuousLimitAdmission(account, rules, reader=reader, dataset=book_dataset)
receipt = admission.submit(
    signal, Decimal(100), at=submitted_at, instrument=instrument,
    reference=exchange_reference, price_view=price_view,
)
```

`receipt.event` 保留原决策时刻、实际申报时刻、原价限价和受理结果，`receipt.price_check` 保留盘口修订、快照和价格规则。成功受理按限价与费用预留可用资金，或冻结已可卖的股份。拒绝不会冻结资产。缺档、晚到和规则不全会中止申报。以相同输入重试可取得原收据。

`account.cancel_order(order_id, at=...)` 回放已生效的撤单，释放尚未成交的预留。过交易日收盘的订单会失效。更新账户时钟时记录失效事件。选用该账户模式时日线开盘撮合会拒绝请求。[沪深账户与数量适用范围](../tivelo-market-cn/README.md#连续竞价申报价格)

### 逐笔成交回放

构造 `ContinuousLimitAdmission` 时另传同一快照内的 `trade_dataset=...`。按账户可知时间驱动回放，同一标的仍须按交易日、来源序号和成交时点依次交付。每次选择一笔成交。

```python
matched = admission.match_trade(
    at=trade_at, sequence=exchange_sequence, instrument=instrument,
    reference=exchange_reference, confirmed_at=report_at,
)
```

省略 `confirmed_at` 时按成交时刻查询。成交晚到时，`at` 是来源的成交时刻，`confirmed_at` 是回放收到该笔记录的时刻，必须达到该修订的可知时间。标的、时段、原价范围、费用和 T+1 按成交时点核对，策略在确认后才看到入账。只有成交时已申报、主动方向相反且价格严格越过限价的委托才能消耗这笔成交量。模型以委托原价限价模拟成交，按价格和申报先后分配，允许部分成交。这个研究模型用严格越价规避同价队列假设，真实成交数据覆盖及交易所完整队列仍需验收。

`matched.trade`、`matched.confirmed_at` 和 `matched.fills` 保留原始逐笔记录、确认时点和对应订单事件。订单事件的 `filled_at` 是这次成交时刻，事件与账本的入账时间是确认时刻。订单事件的 `filled_quantity` 为累计数量，费用与原价取本次分配，逐次成交数量取账本的 `quantity_delta`。以相同输入重试最近一笔可读回同一收据。提前查询返回覆盖不足，未知主动方向返回能力不足。尚需补交的成交可能影响已生效撤单、已确认的失效、已捕获权益或额外资金时，回放返回 `DATA_CONFLICT`，调用方须重新核对事件顺序。[逐笔导入与查询](../tivelo-data/README.md#保存与查询逐笔成交)

跨收盘确认时，创建 `CashEquityAccount("CNY", initial_capital, order_expiry="confirmed_end_of_day")`。委托的成交截止仍是交易日收盘，已预留的资金和股份继续冻结。回放驱动方处理完该笔委托收盘前的成交报告后，调用 `account.confirm_expiry(order_id, at=report_at)` 释放未成交余量，再结束回放。调用方需要有足够的数据覆盖来确认报告已处理完。普通 `end_of_day` 账户在收盘自动释放，可能影响已释放委托的跨收盘成交会返回 `DATA_CONFLICT`。

使用 `ContinuousTradeReplay` 处理先后到达不一致的逐笔报告。为一个标的、一个交易日内的区间提供同一来源核对过的序号列表。列表允许跳号。保持 `TradePrintQuery.sequence` 为空，由读器一次查询区间，在每个 `advance` 时点取当时可见的修订。

```python
from tivelo_backtest.continuous import ContinuousTradeReplay

replay = ContinuousTradeReplay(
    admission, query, sequences=source_sequences, instrument=instrument,
    reference=exchange_reference,
)
receipts = replay.advance(at=knowledge_time)
# 确认该区间已结束且名单内全部收到。满足整笔委托的覆盖后才可确认失效。
remaining = replay.advance(at=last_report_at, require_complete=True)
```

收到后续报告时，会等到前序报告可见再一起按序入账。等待期间可用资金仍取已确认账本。已交付的成交及账本保留当时的修订。若查询出现清单外的序号，回放报冲突。要求完整交付时仍缺报告，返回 `DATA_COVERAGE_INSUFFICIENT`。每次推进的账户与序号游标一起生效，失败后可用原输入重试。覆盖结论取决于调用方核对的源序号清单。

### 从策略归档运行逐笔回测

`tivelo-backtest prepare` 和 `run` 支持 `strict.continuous.v1`。在 `RunRequest.execution` 选择 `signal_at="scheduled"`、`fill_at="witnessed_trade"` 与 `order_expiry="confirmed_end_of_day"`，账户也选择相同的失效政策。用 `execution.continuous` 绑定盘口和逐笔数据集 ID，在 `sessions` 提供每个证券和交易日核对过的连续竞价序号清单与 `confirmed_through_at`。序号写成十进制字符串，以便在工作台和 CLI 间往返时保留整数精度。用 `observe_at` 和 `decide_at` 提供观察与策略调用时点。工作进程按这些时点、收盘及确认边界推进。

快照需要沪深普通主板的历史标的、开盘观察及交易所参考价、原始策略行情、盘口、逐笔及公司行为覆盖。固定前复权和后复权还需可验证的历史因子和锚点。已密封的可信 Python 策略用 `context.price_scales` 取得当次签发的尺度，生成带 `limit_price` 和 `limit_quantity` 的 `Signal`。撮合始终用还原后的人民币原价。报告采用 `checkpoint_equity_decimal12_half_even`，在决策、收盘及最终确认时估值，盘后持仓估值采用收盘原价。真实逐笔来源覆盖、特殊账户和盘中历史参考价修订仍需另外验收。[工作台接入与真实工作进程用例](../../docs/engine-platform/workbench-research-jobs.md#逐笔策略回测)

## 严格日线库入口

```python
from tivelo_backtest.daily import DailyBacktest, StrategyBinding
from tivelo_backtest.price_views import StaticPriceView

# reader 提供已发布的快照；规则、定义与 run_lock 由调用方固定。
runner = DailyBacktest(rules=market_rules, price_view=StaticPriceView("raw"))
result = runner.run(run_lock, reader, StrategyBinding(strategy, strategy_reference, dependencies))
```

`RunLock.contents.verifier_version` 为 `tivelo.strict.daily.v1`，请求采用 `strict.daily.v1`、`event_ledger`、下一开盘成交和期末估值。运行器核对快照/数据版本、定义依赖、市场规则/日历、账户政策和价格视图。真实代码绑定可通过下述 `DefinitionLoader` 完成；`StrategyBinding` 本身不会读取代码仓库，调用方不能把自行填写的摘要当作加载器验证。

读器需实现 `ExecutionDataReader.openings(EventQuery)`，每个可交易日、每个股票池成员都有独立的 `OpeningObservation`：已知的原始开盘价，或明确的停牌、无成交、未上市、退市状态。缺记录不等于停牌；源不能把完整日 K 线的发布时间改成开盘。规则提供 `DailyExecutionRules`，其日历包含完整交易日及声明的日 K 时段口径。

严格准入要求 bars/instrument/opening/corporate_action 及所需因子都有历史版本和范围证据；无公司行为也须有已完成查询的 `no_events` 证据。运行中继续按实际日历检查开盘事实与完整收盘数据，不能仅凭 `Coverage.status=complete` 放行缺口。

`warmup_start` 单独固定准备区间，不在预热期间下单。每次策略调用重新按决策时点选择历史修订，输入只有不可变的可见行情、证券、组合、开盘事实和参数。默认在声明的日 K 结束后决策；`decision_delay_seconds` 可明确等待数据发布，延迟必须早于下一开盘。默认零延迟/无预热字段在规范 JSON 中省略，保持原有默认锁定输入的指纹。

买入信号必须给出 `target_weight`，按决策时刻的原价权益、持仓、数量步长和费用预留确定订单；卖出未给权重时清仓，有权重时减至目标。默认费用搜索要求规则声明总买入成本随数量单调，CN 当前配置满足该条件。成交时重新检查真实现金与费用，跳空不足不会缩量。策略失败或返回其他时刻/股票池的信号使运行失败，不静默等权回退。

公司行为按登记、除权/除息和支付事件时间排序。已登记/处理事件后来被修订或取消时明确失败，需另行对账；公司行为 ID 按证券隔离。缺行情时默认失败；显式 `hold_last_valuation` 才可使用最新可见历史价格，并生成结构化 `diagnostic_events`，不能跨拆分/除息继续使用旧单位价格。扩展只能改变价格视图，不能改动成交量、币种、时间边界等非价格事实。

该入口是精确参考路径，逐决策查询可见历史，尚未完成列式批量优化、规模预算和完整旧研究定义适配。`preflight(lock, reader, strategy=reference, dependencies=...)` 验证锁定输入和覆盖，返回可交易日，不构造或调用策略；逐事件实际数据仍在运行时验证。非公开数据源的历史完整性仍由来源证据证明，测试不代表真实交易所预设已认证。没有调用网络或隐式获取缺失数据；调用方继续拥有 reader 的关闭与快照引用生命周期。

## 供策略读取的自定义研究字段

在运行请求的 `extension_fields` 数组写入 `dataset_id` 和 `field_sha256`，并将相应数据版本纳入同一便携快照。严格日线要求来源声明历史版本、覆盖所请求的市场与股票池，并在每次决策按当时可知的版本提供 `context.extension_values`。每个市场或标的最多返回一个当前观察。尚未公布或已过期时返回空集合，无值修订保留为空值。可信 Python 策略可以直接读取整数、十进制数、布尔值和文本。[完整接入步骤与排名样例](../tivelo-data/README.md#将-tsp-扩展表作为历史研究输入)

### 在受控公式中使用扩展字段

把收据中的 `field_reference` 原样加入运行请求的 `extension_fields`，再在 `program.json` 中将同一引用绑定为公式别名。选择 `format_version: "1.1"`，定义语言沿用 `controlled_daily_v1`。样例按 `-ext_rank` 评分，排名值越小越优先。入场需排名为正，单一持仓使用 50% 的权益。[完整排名程序](../tivelo-data/examples/tsp-extension-field/program.json)

下面的命令在工程根目录运行。准备好上一步采集收据 `receipt.json`、包含全部必需数据的 `snapshot`，以及已有市场和账户配置的运行请求 `base-request.json`。对自己的表，确认 `value_type` 和公式匹配真实字段。

```sh
mkdir -p rank-strategy
jq --slurpfile r receipt.json '.field_operands[0] += $r[0].field_reference' \
  packages/tivelo-data/examples/tsp-extension-field/program.json > rank-strategy/program.json
jq --slurpfile r receipt.json '.extension_fields = [$r[0].field_reference] | .strategy_parameters_json = "{}"' \
  base-request.json > rank-request.json
tivelo-backtest seal --root rank-strategy --id desk.rank-strategy --version 1 \
  --language controlled_daily_v1 --entrypoint program.json --file program.json
tivelo-backtest prepare --request rank-request.json --definition rank-strategy --snapshot snapshot --output rank-run
tivelo-backtest run --run rank-run --snapshot snapshot --output rank-result
```

- 每个别名以 `ext_` 开头，绑定一个数据集和字段摘要。策略使用的字段集合与运行请求逐项核对，预检再用快照确认类型、时间范围及标的。一个文本字段可以绑定多个不同的类别判断。
- 整数和十进制数进入 Float64 运算，无法精确表示的整数、溢出和下溢返回输入错误。布尔字段可直接写进入场或退出条件。文本字段在绑定中给出 `equals_text`，如 `"equals_text": "上行"`，公式中该别名即为精确匹配的布尔结果。
- 每个交易日按当日收盘加决策延迟读取有效且已公布的版本。预热行也执行同样的查询。后续发布的修订只作用于后续观察。市场字段在当天适用于所有标的，个股字段按标的对应。
- `missing_fields` 默认是 `error`。完成预热且可交易的标的，在公式所依赖的字段历史中遇到未公布、到期或撤回的值，会返回 `DATA_COVERAGE_INSUFFICIENT`。选用 `skip_signal` 时跳过该标的本次入场和退出，已有持仓仍由账户估值和结算。窗口检查沿用 DSL 的保守历史口径，嵌套时间算子的回看长度累计，期间缺失也触发该政策。横截面函数使用当天可见样本。
- 免费 raw、前复权和后复权可以消费同一份字段绑定。绑定值取自同一事实快照，在调整价格锚点时保持固定。像 `close > ext_rank` 这样混合价格尺度与固定数值的比较会被拒绝。切到已授权的动态价格扩展时，显式派生 `projected_prices_float64_v1` 策略。参数网格和滚动验证的各次运行保留字段绑定，分别按各自的预热和交易窗口读取。

## 已锁定的扩展价格视图

独立库的 `prepare_run` 和 `execute_run` 接收可选的 `extension_runtime`。宿主实现公开协议 `PriceViewRuntime`，在 `resolve(provider_id)` 中返回已验证的 `ExtensionPin`，在 `open(lock)` 中核对锁定版本、原生入口摘要与授权，并返回本次运行专用的价格提供者。准备和执行分别进行准入。重放按锁内版本定位制品，无法取得原制品或权益时返回失败。

提供者暴露 `extension_reference` 和 `run_lock_id`，投影视图使用 `ProjectedBar`，价格尺度可携带任务锁标识。核心核对来源、时点、股票、单位、数量、成交额与交易时段。撮合、计费和持仓估值继续读取原始市场事实。

`prepare_study`、`run_study` 和 `replay_study` 也接受同一个扩展运行时。参数网格及滚动验证分别为各候选、训练窗口和样本外窗口生成运行锁并取得授权。归档校验可离线读取结果，重新执行需要原制品和可用权益。

受控策略要使用已投影的价格，可在密封的 JSON 程序中显式选择 `numeric_semantics="projected_prices_float64_v1"`。该路径将提供者给出的价格转换为 Float64 后执行已验证的入场、退出与评分表达式。旧策略保留自己的数值语义。未提供扩展运行时的独立命令行只接受内置价格视图。

### 在独立命令行使用签名扩展

安装带 `extensions` 可选依赖的 Backtest 和研究任务所用的市场规则包。管理员通过 `TIVELO_RELEASE_TRUST_FILE` 指定允许的发行公钥及其扩展范围。以下命令在同一个安装目录管理版本和设备身份。预检结果中的 `digest` 和 `revision` 分别代入保存命令，从版本列表读取当前 `generation`，首装为 0。

```bash
tivelo-backtest extensions --state ./engine-home inspect --archive dynamic.zip
tivelo-backtest extensions --state ./engine-home stage --archive dynamic.zip --digest "$DIGEST" --revision "$REVIEW"
tivelo-backtest extensions --state ./engine-home switch activate --digest "$DIGEST" --generation 0
tivelo-backtest extensions --state ./engine-home identity
tivelo-backtest extensions --state ./engine-home license --digest "$DIGEST" --revision 0 --file customer.lic
tivelo-backtest prepare --request request.json --definition strategy --snapshot snapshot --output run --extension-state ./engine-home
tivelo-backtest run --run run --snapshot snapshot --output result --extension-state ./engine-home
tivelo-backtest inspect --archive result
```

把 `identity` 返回的 `public_key` 交给签发方申请本机凭证。用 `extensions ... status` 查询凭证修订号，首次导入为 0。用 `extensions ... list` 查看活动版本，`switch rollback` 和 `switch disable` 均以当前摘要与代际为条件。研究节点显式选用 `mode=extension` 和相应的 `provider_id`。参数研究和重放同样支持 `--extension-state`。新运行锁记录扩展归档、原生入口和实际验证依赖。仅查看归档时无需安装商业包、启动 TSP 或持有有效凭证。

## 受控日线策略与锚点消去

`controlled.ControlledProgram` 定义 `entry`、`exit`、`score` 三个现有 DSL 表达式，以及 `max_positions`、`weight_per_position`、`min_history_bars`。`tivelo-backtest controlled-schema` 输出字段级 schema。以下为格式示例：

```json
{
  "format": "tivelo-controlled-daily",
  "format_version": "1.0",
  "numeric_semantics": "anchor_cancelled_float64_v1",
  "entry": "close >= open",
  "exit": "close < open",
  "score": "rank(close / open)",
  "max_positions": 5,
  "weight_per_position": "0.2",
  "min_history_bars": 1
}
```

将其保存为 `program.json`，通过 `seal --root ./strategy --id example.controlled --version 1 --language controlled_daily_v1 --entrypoint program.json --file program.json` 密封，再使用同一 `prepare/run/inspect` 流程。请求的 `strategy_parameters_json` 必须为 `{}`，全部程序参数都写入密封文件。v1 不接收代码、定义依赖或运行依赖，不自行加载 Python/外部因子；`controlled.load_controlled(checked)` 返回真实 `StrategyBinding`，库调用的 RunLock 需包含 `binding.implementation.proof`，CLI 自动生成。

核心从验证后的字节重新编译，在每标的独立正价格尺度下检查入场/退出为不变量布尔值、评分为不变量数值。OHLC 的尺度次数为 1；`volume` 明确为原始股数量，`amount` 为原始成交额，均不随复权改变。格式 1.1 还接受在同一快照中固定的字段值。未声明的列、未证明算子、Python、自报 `scale_free`、绝对委托价和未声明字段均拒绝。例如 `close < 10`、按 `close` 排名会失败；`close / open`、按该比值排名可以证明。raw 模式也遵守此有界语言的准入规则。

该语言显式选择 `anchor_cancelled_float64_v1`：对于 qfq/hfq，使用当时可见的原价乘累积因子作为固定基准数值，消去每标的共同锚点常数后执行 Float64 运算；raw 使用原价。基准不随 warmup 或决策时刻重新归一化，不请求未来锚点因子，不产生假冒 qfq/hfq 的 Bar。它不保证与先四舍五入为 12 位小数的图表复权价再计算逐位相等，亦不消除未知历史修订的影响。仅支持声明的乘法累积因子；已知修订仍按时点重选，缺因子/混合定义/日内变更失败。

横截面计算使用同日期全部可见样本，然后限制为当前有行情且可交易的标的；不先挑选候选再计算排名。预热行数为程序最低值与三个表达式保守窗口要求的最大值，不足的标的不发信号。使用到的原始字段缺失，或预热完成后任一输出为 null/非有限值时失败。买入候选按四舍六入五成双至 12 位小数后的 score 降序、instrument_id 升序确定；持仓退出优先，不因同日满足入场而重复买入。已有持仓不做自动再平衡，发出卖出信号不会提前释放槽位，实际成交清仓后才可复用；固定目标权重乘槽位数不得超过 1。只支持下一交易时段过期订单，资金、数量、T+1 和费用仍由原价账户检查，不静默缩量。

`RunLock.contents.scale_proof` 记录策略引用、规范程序摘要、计算口径和证明范围。价格程序使用 `tivelo.scale.controlled.v1`，含字段的程序使用 `tivelo.scale.controlled.v1.1` 并保存绑定。版本 1.0 的程序和证明保持原有规范编码。执行以及参数研究生成候选时，核心都会核对密封字节并重新编译。

## 定义制品与可信 Python 加载

`definitions.seal_definition(root, definition_id=..., version=..., kind=..., entrypoint=..., files=..., language="trusted_python")` 仅读取显式选中的文件并生成 `DefinitionBundle`，不执行、写入或自动扫描其他用户文件。调用方将清单以规范 JSON 保存为 `definition.json`。可信 Python 的定义种类可为 strategy/factor/signal/indicator；所读因子 JSON 必须同时列入制品文件清单才受内容锁定保护。另一种语言 `controlled_daily_v1` 仅允许单文件 JSON 策略，见上节。

定义工厂格式为 `module:factory`，调用契约是 `factory(*, parameters_json, dependencies)`。参数是不可变规范 JSON 字符串，dependencies 是按定义 ID 索引的只读映射，仅包含直接声明的依赖实例。策略工厂必须返回实现 `Strategy.decide` 的对象；组件每次运行重新创建，同一次运行的共享依赖只创建一次。

```python
from tivelo_backtest.definitions import DefinitionLoader

loader = DefinitionLoader(dependency_root="./definitions")
checked = loader.inspect("./strategy-source", expected=run_lock.contents.spec.strategy)
# 不执行工厂；任何缺失文件、摘要不符或依赖冲突均在此失败。
with loader.load_trusted_python(checked) as binding:
    result = runner.run(run_lock, reader, binding)
```

依赖目录按 `<dependency_root>/<sha256>/definition.json` 定位，不解析 `latest`。所有文件、传递依赖、参数和已安装依赖版本都在执行前核对；相同定义 ID 的版本或参数冲突拒绝。默认最多 64 个定义、16 层、4,096 个源文件、64 MiB 源内容，每份清单最多 1 MiB。缺文件、符号链接、路径冲突、越界和无效 Python 都明确失败。

加载使用已校验字节的临时副本，不读取后来变化的源文件，不修改 `sys.path`。每次加载使用不同模块命名空间，退出时移除自己注册的模块和导入缓存；应在上下文内完成任务。即使调用者自行构造 `CheckedDefinitions`，加载前仍会重新验证其内容。该机制不是沙箱，也不能证明任意 Python 没有外部 I/O、隐藏依赖或未来信息；静态价格尺度证明仍是另一项独立要求。

## 运行环境内容锁定

`runtime.capture_runtime(requirements)` 解析当前已安装的 PEP 508 依赖闭包，检查版本、环境标记及 extras，返回 `CapturedRuntime(packages, reference)`。不会联网安装或解析 URL 要求。传入需求需包含 Backtest、实际市场/扩展发行包和 `checked.requirements`；调用方不能遗漏自己选用的适配器。

安装态使用 `installed_tree_v1`，绑定实际源码、资源、原生库、依赖/入口元数据，以及包目录内新增但未列入 RECORD 的文件。忽略 `.pyc`、安装器记账与重定位生成的启动包装脚本；因此这不是 wheel 文件的 SHA。支持静态 `src` 布局的 editable 开发环境，额外绑定源码、pyproject 和已声明的项目元数据，标记 `source_tree_v1`；源依赖与已安装元数据不一致时要求重建环境，不按旧元数据放行。

解释器、标准库/随附扩展、Python ABI 和平台进入环境引用。用 `verify_runtime(CapturedRuntime(lock.contents.packages, lock.contents.runtime), requirements)` 在运行前后核对；源或依赖文件变化时返回 `RUNTIME_MISMATCH`。这只证明声明的磁盘内容和环境属性，不是发行者认证，也不保证任意可信代码的外部状态可复现。长期存活进程中的旧模块不会因重新计算磁盘摘要自动更新；任务仍须在固定版本的工作进程中执行，不能热改环境。CLI 每次调用启动新进程。TSP 桌面工作台通过版本目录为主进程及其研究子进程选择同一套引擎。冻结分发的共享依赖使用 `frozen_image_v1`，其摘要覆盖实际交付的可执行文件与资源。单独更新的引擎保留各自的 `installed_tree_v1` 引用。[桌面组合选择与重放](../../docs/engine-platform/extensions-and-releases.md#回退与旧任务重放)

## 独立命令行与市场适配

```sh
tivelo-backtest seal --root ./strategy --id company.strategy --version 1 \
  --entrypoint strategy:build --file strategy.py
tivelo-backtest request-schema
tivelo-backtest controlled-schema
tivelo-backtest prepare --request ./request.json --definition ./strategy \
  --snapshot ./portable-snapshot --output ./run
tivelo-backtest run --run ./run --snapshot ./portable-snapshot --output ./result
tivelo-backtest inspect --archive ./result
tivelo-backtest result-replay --archive ./result \
  --snapshot ./portable-snapshot --output ./replayed
```

`seal` 只保存显式文件的摘要清单，相同内容重试幂等，不覆盖已有不同清单。依赖定义使用重复的 `--dependency <reference.json>`，安装依赖使用重复的 `--requirement <PEP508>`；均不会自动安装。`prepare` 的可选 `--dependencies` 指向按摘要组织的依赖目录。

请求由 `runner.RunRequest` 定义：universe、start/end、warmup_start、strategy_parameters_json、market、account、initial_capital、price_view 和 execution。金额用十进制字符串，日期为市场交易日期。`request-schema` 给出完整可机器校验字段；调用者不用手工填写快照、策略、市场或环境 SHA。`runner.prepare_run` 与 `runner.execute_run` 是相同流程的库入口。

market 指定 `adapter_id`、`distribution` 和 `configuration_json`。加载器只选择 `tivelo.market_rules.v1` 组中的唯一同名入口，并核对发行包；缺失、重复、发行者包名不匹配或返回协议不完整时失败。该入口执行已安装可信代码，必须先完成运行环境内容检查；不会解析任意模块路径或下载安装包。CN 包提供 `tivelo.market.cn.cash-equity`，配置格式见其 `CNMarketProfile`，必须显式提供规则与日历。未来市场通过同一协议接入，Backtest 不导入具体市场包。

`prepare` 静态核对定义闭包和快照、加载指定规则、生成实际 RunLock，并归档定义副本。它不执行策略工厂；一般固定视图在首个决策使用未来锚点时拒绝，受控 JSON 必须先通过整条决策链的尺度验证。准备之后编辑原策略不影响该任务。`run` 从归档副本创建策略，运行前后核对环境和快照；不允许缺失内容时重新解析最新版。Python 策略是可信代码，CLI 不是安全沙箱。

### 从结果归档复算

`result-replay` 使用结果内的运行锁、策略和依赖定义，配合原便携快照和锁定的运行环境。对工作台导出的 ZIP，将解包后的 `output/` 用作 `--archive`。新归档与原清单一致时才发布。快照、运行环境、定义或复算输出不符时返回明确错误。商业价格视图的执行继续核对锁定扩展和使用权，使用 `--extension-state` 选定已有扩展安装。

原环境的 CLI 只有 `run` 命令时，可在新 CLI 上恢复准备归档，再交给原环境运行。恢复沿用原运行锁和定义字节。

```sh
tivelo-backtest restore-run --archive ./result --output ./restored-run
# 切换到原运行环境后执行
tivelo-backtest run --run ./restored-run --snapshot ./portable-snapshot --output ./replayed
```

Python 库入口为 `runner.replay_result` 和 `runner.restore_run`。

## 独立参数网格与滚动验证

`study_models.StudyRequest` 固定搜索空间、目标、窗口和预算；`studies.run_study` 从已归档的基础任务运行，`evaluate_study` 则接受调用方管理的读器、规则和已核对定义。基础任务先按上文的 `prepare` 准备，不从当前用户策略目录重新解析内容。

```json
{
  "mode": "walk_forward",
  "search": {
    "target": "controlled_program",
    "axes": [{"name": "weight_per_position", "values": ["0.25", "0.5"]}]
  },
  "objective": {"metric": "total_return", "min_filled_orders": 1},
  "walk_forward": {
    "train_sessions": 120,
    "test_sessions": 20,
    "step_sessions": 20,
    "gap_sessions": 0,
    "warmup_sessions": 30,
    "training": "rolling"
  }
}
```

```sh
tivelo-backtest study-schema
tivelo-backtest study --request ./study-request.json --run ./run \
  --snapshot ./portable-snapshot --output ./study
tivelo-backtest inspect --kind study --archive ./study
tivelo-backtest study-replay --archive ./study \
  --snapshot ./portable-snapshot --output ./study-replayed
```

- `grid` 在基础任务的整个交易区间搜索，不产生样本外汇总。`walk_forward` 按已锁定市场的实际交易日切分；支持 `rolling` 固定训练长度及 `expanding` 扩展训练区间，间隔和预热也以交易日计。基础任务必须提供足够预热历史，末尾不完整折不运行，测试窗口禁止重叠。
- 可信 Python 使用 `strategy_parameters`，保留固定参数并覆盖声明的轴；受控程序使用 `controlled_program`，只允许入场、退出、评分、槽位、权重和最少历史数六类字段。轴名称有序且唯一，值为有限 JSON 标量；十进制权重用字符串。所有受控候选在首次执行前重新验证整个程序及尺度证明；标准化后重复的候选拒绝。
- 每个候选及窗口创建新的策略实例和账户。目标仅支持总收益最大化或非负最大回撤最小化；训练完全成交订单数低于 `min_filled_orders` 的候选不参与排名，同分以内容 ID 稳定排序。无候选合格时整个研究失败。只执行训练选中的那组样本外参数，测试结果不参与本折选择。
- `account_policy=reset_each_window`：不跨折传递持仓或挂单。样本外汇总为所有折（含亏损、零成交）的独立收益乘积，名称为 `independent_fold_return_product`；它不是连续组合账本，也不提供年化、夏普或稳健优化指标。
- 默认最多 128 个候选、256 次执行、256 MiB 保留事实缓冲区、512 MiB 归档。单次研究最多 512 个候选和 512 次执行，归档最多 8,192 个文件；额外依赖也计入文件及字节预算。每个试验复用一份准备事实，逐决策的严格查询和模型构造仍存在，不据此声称全市场性能已达标。

研究归档包含基础 `run-lock.json`、原始定义副本、`study-plan.json`（含请求、候选和真实交易日历）、`study-report.json` 及 `trials/<窗口>/<train|test>/<候选>/` 下各次完整结果。最外层 `manifest.json` 仅在全部试验与汇总成功后发布；取消或任一候选失败拒绝整项研究，不返回部分成功。已有相同研究幂等，未知或不同目标保留并拒绝覆盖。尚无试验级持久化续跑或多进程调度。

`study_archives.read_study` 校验摘要、定义依赖闭包、候选派生、窗口、子结果、训练排名与全部样本外汇总，不执行归档策略、不加载市场适配器、不验证现有订阅或历史环境。`study-replay` 则使用归档内的请求和定义，要求原便携快照与一致运行环境；计划或最终归档摘要不同会在发布前拒绝。研究档案不复制行情与软件环境，不是发行签名或任意 Python 的安全沙箱。

## 输入与结果归档

`archives.publish_run` 保存 `run-lock.json` 与 `definitions/<sha256>/` 下的完整已声明依赖副本。`publish_result` 在相同输入副本之外保存：

- `orders.parquet`、`equity.parquet`、`ledger.parquet`、`diagnostic-events.parquet`：有顺序号的类型化表，金额/数量为 decimal128(38,12)，时间为 UTC 微秒，费用为结构化列表。
- `summary.json`：任务语义、账户模型、期末组合和诊断代码。
- `metrics.json`：初始资本、期末权益、非年化总收益、基于每日估值的最大回撤及完全成交订单数；比率保留 12 位 half-even。未实现的年化、夏普或基准指标不会用占位值伪造。
- `manifest.json`：全部文件摘要、大小和表行数，最后发布，表示完整成功。

先在同文件系统暂存完整内容，再排他创建目标并链接文件；已有同内容幂等，已有不同或未知目录拒绝。普通失败只撤销本次创建的路径，保留期间出现的无关文件。取消在发布标记之前生效；标记已提交后的取消保留成功。文件系统必须支持硬链接，缺失能力明确失败；目录 fsync 在 POSIX 执行，Windows 不宣称同等断电持久性。标记提交后的目录同步错误可能留下有效完整结果，重试前可 `inspect` 确认。进程崩溃可能留下无标记目录，当前不自动清理或修复它。

`read_run` 验证文件与定义闭包，`read_result` 验证摘要、表结构、顺序、行数和模型，再复算指标；不会执行归档代码，也不检查当前专业订阅或要求运行环境仍与历史完全相同。默认单清单/JSON 16 MiB、归档内容 512 MiB、单表 5,000,000 行；超过预算明确失败。当前结果读取和写入仍有内存物化，列式文件不等于已经完成全流程流式优化。

包含明确委托类型的结果使用 `ResultArchive` 格式 `1.1` / `parquet-v2`，订单表新增 `order_type: String` 和 `raw_limit_price: Decimal(38,12)` 两个可空列。新读器继续读取原 `1.0` / `parquet-v1`，不会把旧报告中缺失的类型猜成市价。只含旧记录或没有订单的结果仍可保存为旧布局；格式与列布局不匹配、限价成交越界、同一订单的类型或限价发生变化均拒绝读取。旧读器不支持新版布局，不能用于覆盖新版结果。

归档保存快照引用与实际环境身份，不复制行情和软件安装环境，也不是发行签名。重算仍需要原便携快照和一致运行环境；完整快照保留策略、软件版本存档、工作台结果索引、持久化任务及报告页面尚待接入。
