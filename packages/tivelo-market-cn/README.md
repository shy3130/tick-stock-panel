# Tivelo Market CN

独立 A 股市场包（`0.1.0a1`），不依赖 TSP、数据引擎、回测引擎或供应商 SDK。包含原有市场时间与涨跌幅算法的兼容入口、沪深北股票规则预设，以及从快照原价 K 线或显式日历确定交易日期的日线配置。

## 许可

**Tivelo 自有实现未经权利人单独书面授权不得商用。** 自有实现采用 [PolyForm Noncommercial License 1.0.0](LICENSE)，按许可条款允许的用途可免费使用。

此前已按 MIT 许可授出的材料保留原有权利和版权声明，见 [MIT 许可](LICENSE-MIT)和[许可说明](NOTICE)。

## 兼容入口

```python
from tivelo_market_cn.legacy import market_time, price_limits
```

`legacy` 保留 TSP 基线的时间、价格限制、标量/Polars/NumPy 规则和舍入。TSP 的 `app.market_time` / `app.price_limits` 指向同一模块，旧调用与 monkeypatch 继续有效。迁移不改动历史日期分界、税费、复权、撮合或现金账本。

旧分钟策略 CLI 使用 `tivelo.market.cn.legacy-minute` 入口，按证券代码、行情日和历史名称提供原涨停比例。运行锁包含本规则包的内容摘要。兼容回测的日期来自快照行情，手续费仍由请求的撮合参数提供。

这些是 `legacy.tsp.v1` 兼容规则，**不是已核验的完整交易所预设**。其中周末判断不识别法定休市，缺少精确交易日序号时新股窗口存在旧近似路径；代码前缀和当前名称识别也不能代替历史证券规格。新严格引擎不能把这些近似路径当成完整市场准入。

## 显式日历

`calendar.CNCalendar` 接收 `CalendarVersion`，所有日期（包括休市日）、UTC 时段、可知时间、来源摘要和 bar 时段口径均由调用方提供。其版本 ID 由完整内容摘要生成；没有隐式网络、服务器时区或“工作日就是交易日”的推断。

- 每个日期必须明确开市或休市，日历覆盖范围内不允许缺日期。
- 开市日期必须提供连续交易时段，可另提供集合竞价时段；区间采用左闭右开。
- 时段排序、不重叠，必须属于该北京时间日期。午休不属于连续时段。
- `daily_bounds` 返回连续时段的首尾边界；`next_trading_open` 跳到下一个开市日期的连续时段开始，不把午后重开当成 T+1。
- 超出日历范围、缺失下一交易日或日历尚不可知时明确失败。不会用自然日猜测或偷偷下载新版日历。
- 日历仅提供时间规则，不自行证明来源权威性或行情完整性。正式预设还需核验来源，并将日历摘要随运行输入锁定。

`CalendarDay.available_at` 可分别记录各日期安排的已知时间，未指定时沿用日历总体值。查找下一开盘会检查经过的休市日，不能用未来才公布的假期跳过某天。严格日线入口使用 `trading_days`：执行开盘为首个连续时段开始，收盘涵盖最后的收盘集合竞价；旧 `daily_bounds` 的连续时段语义保持不变。具体日 K 的统计口径由 `bar_convention_id` 对齐。

`tests/test_calendar.py` 使用标明为合成的显式日历，验证午休、休市、覆盖缺口和时间边界；有来源的年度日历由下述预设提供。

## 离线日历预设

按分钟读数时，`CNCalendar.regular_sessions(trading_date, as_of=...)` 返回当日连续竞价开盘至收盘的各段区间，保留午休间隔与收盘集合竞价，休市日返回空元组。每个区间含 `session_id`、`start` 和 `end`。查询使用同一版本日历的可知时间。

可选的 `cn.equity.auction-calendar` **2026.1.0** 覆盖 `XSHG`、`XSHE` 的 **2024-01-01 至 2026-12-31** 已公告股票常规竞价安排。**2026.2.0** 增加同一日期范围的北交所预设。资源随 wheel/sdist 发行，来源保存 URL、原始内容摘要、发布时间精度和抓取时间。日线默认使用快照自带的日期，即使没有选择这项预设也能运行。[北交所 2024 年公告](https://www.bse.cn/important_news/200020235.html) · [2025 年公告](https://www.bse.cn/important_news/200024437.html) · [2026 年公告](https://www.bse.cn/important_news/200027428.html)

```python
from datetime import date
from tivelo_market_cn.calendar import CNCalendar
from tivelo_market_cn.presets import compile_calendar, installed_calendar_presets, load_calendar_preset

for entry in installed_calendar_presets():
    print(entry.contents.preset_version, entry.version_id)

preset = load_calendar_preset(version="2026.1.0")
version = compile_calendar(
    preset,
    venue_id="XSHG",
    start=date(2025, 1, 1),
    end=date(2026, 12, 31),
)
calendar = CNCalendar(version)
```

工作台用 `installed_calendar_presets()` 发现本机可选版本。它会核验随包文件与发行清单的摘要，按核验日期从新到旧返回预设。每项的 `contents.years` 给出已公告的交易所与年度。定时研究使用指定版本，在创建计划时保存所用日期和 UTC 派发时点。[交易日计划和请求示例](../../docs/engine-platform/workbench-research-jobs.md#按交易日派发)

`load_calendar_preset` 可传 `expected_sha256=preset.version_id` 锁定内容；未知版本、`latest`、缺失/损坏文件或摘要不符明确失败。`compile_calendar` 只生成显式请求的闭区间，未核验场所/年度不作工作日推断。编译结果是既有 `CalendarVersion`，可直接作为 `CNMarketProfile.calendar` 保存，由原已安装工厂读取；已保存的完整配置不会因安装另一预设版本而变化。

公告只确认日期时，使用**次日北京时间零点**作为保守可知时间。每个日期取该年度休市来源与当时有效时段来源的最晚可知时间；2026 年新规则的时段来源从 7 月 6 日切换，公告前不能提前使用它。新规则和新年度不会把整个旧年度的可知时间推迟到新版发布日。运行时仍由 `CNCalendar` 逐日检查 `as_of`；跨年查询不能借用尚未公布的下一年度日历，查询终点之外的 T+1 也不会自动补齐。调用方应显式覆盖所需下一交易日。

时段是 09:15–09:25 开盘集合竞价、09:30–11:30/13:00–14:57 连续竞价、14:57–15:00 收盘集合竞价。`trading_days` 的执行开盘为 09:30，收盘为 15:00；`daily_bounds` 保留连续时段结束 14:57 的原接口语义。`bar_convention_id="cn.equity.regular-auction.daily.v1"` 明确限定常规股票竞价日 K；数据适配器必须核对真实数据的统计范围，含盘后交易量的供应商日 K 不能仅改名后使用。

这份预设是**交易所已公告的日历安排**，不证明未来实际开市、不处理证券个别停牌，也不涵盖盘后交易、大宗、基金/指数或其他资产的时段。临时全市场停市需要新的来源及不可变预设修订，旧任务保留原版本。来源摘要用于追踪与完整性核对，不等于交易所为软件预设提供签名或认证。完整市场预设仍需证券历史状态、费用和账户准入等独立输入。

## 显式历史交易规则

`rules.CNTradingRules(RuleConfiguration(...), calendar)` 实现 `MarketRules`、`ExecutionRules` 和 `DailyExecutionRules`，提供 `trading_days` / `next_open`。配置为不可变记录，`reference` 的内容摘要同时绑定规则与日历。当前实现只接纳 `cn.equity`、股票、CNY、股数量及与日历匹配的场所、时区；不根据代码前缀或当前简称推断历史板块、ST 状态和新股窗口。

`TradingTerms` 由证券的 `spec_revision` 精确关联，包含生效/可知时间、申报下限/步长/上限、零股政策、卖出解锁时段数、最小价位及涨跌幅状态。配置 1.0/1.1 保持每个规格一条规则；1.2 允许同一规格关联多条历史规则，独立表达证券分类变化与交易规则更新。配置或证券状态在执行时尚不可知、已失效或不匹配时失败。

`buy_quantity` / `sell_quantity` 量化合法数量；是否接受缩量由调用方决定，精确账户选择拒绝非法申报。`sellable_at` 逐个使用显式交易日历计算，不能通过跨过午夜绕过 T+1。`all_remaining` 保持只有清空可卖余额时才豁免普通数量网格的语义；配置 1.3 新增下述 `remainder_once`，不能把两种政策互换。

`price_bounds` 要求调用者提供对应标的、日期和可知时间的 `ReferencePrice`；除权参考价不能用旧收盘或复权因子代猜。配置明确区分相对幅度、不设限和未知；未知返回 `RULES_UNAVAILABLE`。相对界限按 tick 四舍五入、至少相隔一个 tick；该算法仍须随正式预设核验具体适用市场及历史范围。

`FeeRule` 对每项费用明确费率、最低额、计费量子、舍入、买卖侧、有效区间和来源。必需费用缺失或未来才公布时失败；零费率也必须明确配置。各项独立舍入后合计，最低额须能用计费量子表示；含规费佣金与被包含项目不能同时扣取。券商费率不作为软件默认事实。

交易规则测试中的证券规格和费率为合成数据，日历测试分别标明合成与公告来源。库提供可配置的规则执行机制，尚未交付经完整来源、生效历史和券商契约核验的官方默认配置。

## 分类与规则历史

`classification.CNEquityClass` 显式声明场所、板块、风险状态与上市阶段；这些状态须来自数据适配器提供的历史事实。板块支持 `main`、`star`、`chinext`，风险状态支持 `normal`、`risk_warning`；上市阶段区分 `regular`、`ipo_first_five`、`relisting_first`、`delisting_first`、`delisting`。字段不会按代码前缀、名称中的 ST 或自然日天数自动填充，缺失信息不得猜测为普通主板证券。

```python
from tivelo_market_cn.classification import CNEquityClass

classification = CNEquityClass(
    venue_id="XSHG", board="main", risk_status="normal", listing_phase="regular",
)
bound = classification.bind(historical_instrument)
```

`historical_instrument` 是调用方提供的 `Instrument`。`bind` 校验市场、场所、股票类型、币种和时区，只替换 `spec_revision`，保留原始来源、修订标识和时间区间。一个分类须适用于整条记录；状态改变时由适配器划分历史区间并保存修订，不覆写过去版本。分类内容摘要形成稳定的 `cn.equity.class.*` 引用，多只证券可以共用分类。分类本身不包含官方涨跌幅、数量或费率，不能当作实际历史覆盖证明。

使用分类目录或同一规格的多条规则必须选择 **`RuleConfiguration.schema_version="1.2"` 或 `"1.3"`**。非空 `instrument_classes` 与 `terms` 中的规格集合必须完全对应，场所和证券类型须匹配。同一规格的规则保持资产身份一致；每个规格及每种 `fee_id` 内的 `revision_id` 不得复用，`(valid_from, available_at)` 组合不得冲突。

规则选择同时检查生效和可知时间：从 `valid_from <= at` 且 `available_at <= at` 的记录中，先取最新生效起点，再取该起点最新已知修订，最后检查 `valid_to`。公告已知但尚未实施时沿用旧规则；较旧区间的迟到修订不能覆盖较新生效区间。最新选中记录已失效时返回 `RULES_UNAVAILABLE`，不恢复被替换的旧规则。记录失效公告时应添加有自己可知时间的修订，不能把新获知的结束时间直接写入原记录。

1.2 的费用按 `fee_id` 独立使用同一选择规则，支持费率或有效区间的修订；明细保存实际采用的 `rule_revision`。旧 1.0/1.1 继续禁止费用区间重叠，并保留既有修订标签语义。必需项目仍由锁定的 `required_fee_ids` 决定；收费项目及包含政策的复杂历史变化尚不能仅靠费率版本宣称完整支持。

市场配置、分类目录、全部规则版本和日历共同进入指纹；历史证券事实也进入数据版本摘要。更新软件不会改写已保存配置。此前 CN 制品明确拒绝 1.2，新实现读取旧 1.0/1.1 时保持规范字节及原有指纹；没有分类目录时不额外编码空字段。上述时间机制已连通数据快照和独立回测，真实供应商的状态覆盖、来源核验及完整官方默认预设仍是独立交付项。

## 按委托类型区分数量

`TradingTerms.order_quantity_rules` 可保存按 `order_type` 排序且不重复的 `OrderQuantityTerms`。每个项目明确 `market` 或 `limit` 的买卖下限、步长、上限和零股政策；非空时，这个列表也是允许类型的完整集合。它继承所属 `TradingTerms` 的历史生效、可知时间与来源，不通过当前证券名称猜测。

使用该字段的 `RuleConfiguration.schema_version` **必须为 `"1.1"`、`"1.2"` 或 `"1.3"`**。旧 `"1.0"` 配置拒绝携带新约束，使旧 CN 适配器不能在新 Contracts 已安装时将新规则当作旧规则加载。没有新字段的旧配置保持原默认编码和摘要；无需迁移旧用户配置。

`CNTradingRules` 实现可选的 `OrderExecutionRules`：

```python
from decimal import Decimal

selected = rules.quantity_terms(instrument, "limit", at=execution_at)
quantity = rules.order_quantity(
    instrument, "buy", Decimal("60000"), order_type="limit", at=execution_at,
)
sell_quantity = rules.order_quantity(
    instrument, "sell", Decimal("199"), available=Decimal("199"),
    order_type="limit", at=execution_at,
)
```

上述是已实现的方法签名，`rules`、`instrument` 和时刻仍需调用者提供。输入数量必须符合公共十进制契约；预算除以价格形成的内部高精度中间值由调用者显式向下取至契约精度。返回的是不超过请求的合法数量，最终是否采用缩量由执行模型决定。

`buy_quantity` / `sell_quantity` 也接受显式 `order_type`。有分类规则时省略类型明确失败；不存在该类型时返回 `UNSUPPORTED_CAPABILITY`，不使用通用上限替代。旧配置没有分类规则时，两种显式类型继续使用原数量规则。单笔上限同样适用于卖出全部余额，零股处理不能绕过它。

此处验证委托数量。连续竞价价格使用单独的盘口输入。累计限制、投资者权限、盘后交易、盘口容量与完整市场预设仍需按计划实现和验证。

## 零股与有来源的数量预设

配置 **1.3** 的 `remainder_once` 要求卖出最小量等于步长。对于 100 股一手、可卖余额 299 股的情况，允许卖出 99、100、199、200 或 299 股；98、198、298 股会拆分零股，不能直接成交。数量函数返回不超过请求、上限与可卖余额的最大合法量，账户仍拒绝不合法的原始委托。零股可以单独卖出，也可以与整手合并，但不能因为包含零股就突破单笔上限。CN 此政策拒绝非整数的可卖股数；碎股现金补偿仍须另行处理。旧 1.0/1.1/1.2 拒绝该新政策，旧 `all_remaining` 编码及行为保持稳定。

内置 `cn.equity.auction-quantities` **2026.1.0**，配置覆盖 2024-01-01 至 2026-12-31 的连续竞价数量与类型子规则。文件保存 11 项来源及 8 个场所/板块/生效版本，包含两所 2023/2026 年规则及发布生效材料、[上交所零股通知](https://www.sse.com.cn/lawandrules/guide/stock/jyglywznylc/tz/c/c_20230209_5716007.shtml)和[深交所零股通知](https://www.szse.cn/lawrules/rule/trade/current/t20150914_565054.html)。原始公告日期与现网页路径日期分别处理；来源摘要固定在预设内，不在运行时取数。

| 场所/板块 | 普通买卖下限/步长 | 限价单笔上限 | 市价单笔上限 | 零股政策 |
| --- | --- | --- | --- | --- |
| 沪深主板 | 100 / 100 股 | 100 万股 | 100 万股 | `remainder_once` |
| 科创板 | 200 / 1 股 | 10 万股 | 5 万股 | `all_remaining` |
| 创业板 | 100 / 100 股 | 30 万股 | 15 万股 | `remainder_once` |

```python
from datetime import UTC, datetime
from tivelo_market_cn.quantity_presets import load_quantity_preset, resolve_quantity_rules

preset = load_quantity_preset(version="2026.1.0")
selection = resolve_quantity_rules(
    preset, classification, at=datetime(2025, 1, 3, 2, tzinfo=UTC),
)
order_quantities = selection.order_quantities
```

`classification` 是前述显式历史分类。`selection` 带预设摘要、采用规则 ID、原始生效日、保守可知时间和来源/条款引用；公告已知但新规则尚未生效时仍选旧版本。沪市主板风险警示及退市整理阶段只返回限价数量；深市无涨跌幅阶段也只返回限价数量。科创板重新上市首日尚无已核验配置，明确拒绝。规则的覆盖区间只是本版本支持范围，不能冒充官方首次生效或终止日期。

加载必须指定版本，可传 `expected_sha256` 固定内容。未知版本、覆盖外日期、缺失/损坏资源或改写后重新计算自带摘要的替代文件均拒绝。新增年度或交易所修订须形成新预设版本，已锁定配置保持原内容。

返回值用于组成 `TradingTerms.order_quantity_rules`，作用域为 `continuous_auction_quantity_and_type`。完整执行仍需日历/会话、涨跌幅、保护限价与价格笼子、累计数量、账户权限、费用及结算政策。此数量组件没有把这些约束标为已实现，也没有把单笔上限作为账户当日累计上限。

## 官方费率与明确计费政策

`fee_presets` 提供离线 `cn.equity.auction-fees` **2026.1.0**，支持 2022-07-01 至 2026-12-31 的沪深普通 A 股竞价费率。12 份来源和 9 条费率历史固定在资源中，分别记录买卖侧、十进制费率、生效依据和保守可知时间。它与日历的覆盖范围独立；可运行日期须同时满足全部输入覆盖，不能据此宣称已有 2022 年日历。

| 费用 | 2023-08-28 起的标准费率 | 历史与依据 |
| --- | --- | --- |
| 印花税 | 卖出成交额 × 0.0005 | 印花税法附表为 0.001，2023 年第 39 号公告自该日起减半；买入不收 |
| 经手费 | 买卖成交额分别 × 0.0000341 | 沪深 2015 年起为 0.0000487，2023-08-28 下调 |
| 监管费 | 买卖成交额分别 × 0.00002 | 沪市按 2012-09-01 清算参数生效记录；深市按 2019-05-31 官方资料确认当时已施行，不把确认日冒充首次生效日 |
| 过户费 | 买卖成交额分别 × 0.00001 | 港交所 CT/037/22 同期通告明确记载沪深 A 股自 2022-04-29 调整的费率 |

印花税正文及附表、两所调整通知和来源摘要均见随包 JSON。中国结算的两份 2026 年收费表用于后续交叉核对。`2026.2.0` 另收入北交所股票的经手费、监管费、过户费和印花税。[经手费](https://www.csrc.gov.cn/csrc/c100028/c7426794/content.shtml) · [监管费](https://www.ndrc.gov.cn/xxgk/zcfb/tz/202201/t20220107_1311590_ext.html) · [过户费公告转载](https://jrj.wuhan.gov.cn/ynzx_57/xwzx/202204/t20220429_1964160.shtml)

官方费率与账户收费政策是两个输入。`BrokerFeePolicy` 要求明确填写佣金、最低额、包含费用、各实际收费分项的量子和舍入，以及政策的有效/可知时间和出处。没有默认券商佣金。当前只接受 `account_scope="domestic_cash_standard"` 与 `aggregation="per_fill_research"`，表示标准境内现金账户的逐成交研究计费；不是券商交割单认证。按订单累计最低额、按日归集、税费退款和跨政策归集尚未实现，输入这些模式会明确拒绝。

以下佣金与舍入是**演示假设**，实际使用须替换为明确账户政策：

```python
from datetime import UTC, date, datetime
from tivelo_market_cn.fee_presets import (
    BrokerFeePolicy, FeeRounding, compile_fee_rules, load_fee_preset,
)

policy = BrokerFeePolicy(
    policy_id="example.research", policy_version="1",
    account_scope="domestic_cash_standard", aggregation="per_fill_research",
    commission_rate="0.0002", commission_minimum="5",
    commission_includes=("handling", "regulatory"),
    rounding=tuple(
        FeeRounding(fee_id=key, quantum="0.01", rounding="half_up")
        for key in ("commission", "stamp", "transfer")
    ),
    valid_from=datetime(2024, 1, 1, tzinfo=UTC),
    available_at=datetime(2024, 1, 1, tzinfo=UTC),
    revision_id="example-1", source_id="example",
    source_reference="explicit-research-assumption",
)
compiled = compile_fee_rules(
    load_fee_preset(version="2026.1.0"), policy,
    venue_id="XSHG", start=date(2025, 1, 2), end=date(2025, 1, 7),
)
```

编译产生现有 `FeeRule` 历史，实际计算仍由 `CNTradingRules` 执行。被佣金包含的费用保留在包含声明及来源追踪中，不再生成额外收费分项；印花税不能通过佣金包含列表消失。所有独立分项先执行自己的最低额和舍入，再求和。官方费率及政策公告都不能提前生效，编译结果在请求范围结束时失效，缺少已知初始费率不会退化成零费用。一个政策必须覆盖请求区间，不能自行拼接不同券商政策。

### 保存预设与政策输入

使用新 `tivelo-cn-profile-v2` 保存完整编译输入、预设版本/摘要、政策与结果。下面的 `configuration` 和 `calendar_version` 是调用者已有的交易条款配置和不可变日历：

```python
from tivelo_market_cn.adapter import CNMarketProfile

profile = CNMarketProfile(
    profile_format="tivelo-cn-profile-v2",
    fee_compilation=compiled,
    configuration=configuration.model_copy(update={
        "schema_version": "1.3",
        "required_fee_ids": compiled.required_fee_ids,
        "fee_rules": compiled.fee_rules,
    }),
    calendar=calendar_version,
)
configuration_json = profile.model_dump_json()
```

工厂加载时从固定资源重新编译并比对；政策、执行费率、场所、资产类型或来源记录不一致会拒绝。完整 profile 通过既有市场定义文件进入 CLI/工作台归档，费用明细的 `source_reference` 包含逐成交研究口径及输入摘要，修改政策会改变市场指纹。读取旧报告仍走结果读取接口，无须重新执行市场工厂。

旧 `tivelo-cn-profile-v1` 的规范字节保持不变，仍可加载手工完整 `FeeRule` 配置；它不接受新的编译输入字段。旧市场包不能加载 v2，会明确拒绝。新增预设版本须保留被旧配置引用的资源，不能改写同名 JSON；加载器不提供 `latest` 或运行时下载。

## 离线每日价格预设

`price_presets.load_price_preset(version="2026.1.0")` 提供 `cn.equity.daily-price-limits`，支持 2024-01-01 至 2026-12-31 的沪深普通股。9 份来源固定 8 条板块/生效日记录，分别引用 2023、2026 年交易规则、发布通知及 2023 年实际启用事件。以下是**每日涨跌幅和价格最小单位**，不是限价申报价格笼子或完整订单准入：

| 分类 | 2024-01-01 至 2026-07-05 | 2026-07-06 起 |
| --- | --- | --- |
| 沪深主板普通股票 | 10% | 10% |
| 沪深主板风险警示股票 | 5% | 10% |
| 主板退市整理期（首日除外） | 10% | 10% |
| 科创板、创业板普通/风险警示/退市整理股票（例外阶段除外） | 20% | 20% |

价格最小单位为人民币 0.01 元；新股前五个交易日、退市整理首日，以及适用板块的重新上市首日，明确返回无每日涨跌幅限制。科创板重新上市等未覆盖分类明确拒绝；特殊临时调整、证券实际所属阶段由历史资料提供，不根据名称、代码或上市日期猜测。2025 年调整风险警示股票的征求意见/技术准备不是生效依据；2026 年公告日期也不会提前切换规则。

```python
from tivelo_market_cn.price_presets import (
    load_price_preset, resolve_price_rules, compile_price_terms,
)

preset = load_price_preset(version="2026.1.0", expected_sha256=pinned_price_digest)
selected = resolve_price_rules(preset, historical_class, at=execution_time)
compiled_prices = compile_price_terms(preset, historical_class, base_terms)
```

示例中的摘要、分类、执行时刻和 `base_terms` 由调用者提供。每条基础条款必须匹配分类，且具有完全位于预设覆盖内的有限生效区间。编译只替换 `price_tick`、`price_limit_mode`、`limit_up_ratio`、`limit_down_ratio`，按价格规则生效边界拆分历史条款；数量、T+1 等其他输入不会因此被认证为官方规则。原始条款（包括被替换的价格字段及原来源）保留在 `CompiledPriceTerms.inputs`，执行条款来源引用绑定该完整输入摘要。

编译保留基础条款的有效与可知顺序；价格新阶段不能使较早基础条款或过期条款重新生效。已生效价格规则的来源尚不可知时明确失败，不能继续使用旧比例。涨跌幅金额仍由既有 `CNTradingRules.price_bounds` 根据显式原价参考计算，执行已有四舍五入、至少一档及最低正价规则；不能把复权收盘价当作交易所原价参考。

归档使用 `tivelo-cn-profile-v3`，同时保存费用输入及非空 `price_compilations`。所有配置条款和分类都必须由这些价格编译结果覆盖，工厂离线重新编译并比对。构造方式：

```python
profile = CNMarketProfile(
    profile_format="tivelo-cn-profile-v3",
    configuration=configuration_with_compiled_terms,
    calendar=calendar_version,
    fee_compilation=compiled_fees,
    price_compilations=(compiled_prices,),
)
```

多个分类分别编译，同一分类的基础历史合并为一次输入。旧 v1/v2 的缺省编码保持不变；它们不接受价格编译字段，不自动升级已归档输入。只支持 v2 的旧市场包明确拒绝 v3。`configuration_with_compiled_terms`、不可变日历和费用政策仍需调用者显式配置；完整默认市场组合、账户资格、累计申报限制及结算继续按原计划实现。

## 生成 A 股市场配置

`tivelo-market-cn profile` 根据起止日期和一份 `BrokerFeePolicy` 生成回测请求的 `market`。默认选择 `2026.2.0` 的沪深主板、创业板、科创板和北交所正常上市普通股。证券历史保留原交易所的日历 ID 和对应的 `CNEquityClass`。通过 `instrument_classes` 可以选定已核实的分类范围。[证券历史绑定](#分类与规则历史) · [账户计费输入](#官方费率与明确计费政策)

```sh
tivelo-market-cn profile-schema > cash-equity.schema.json
tivelo-market-cn profile --request ./cash-equity.json > market.json
jq --slurpfile profile market.json '.market = $profile[0]' ./run-template.json > run.json
```

`cash-equity.json` 的 Python 构建示例使用前文定义的账户 `policy`。

```python
from datetime import date
from pathlib import Path

from tivelo_market_cn.profile_presets import CashEquityProfileRequest

request = CashEquityProfileRequest(
    start=date(2025, 1, 24), end=date(2025, 2, 6), broker_policy=policy,
)
Path("cash-equity.json").write_text(request.model_dump_json(), encoding="utf-8")
```

Python 也可用 `compile_market_adapter(request)` 取得同一个 `MarketAdapterSpec`，交给独立 `tivelo-backtest prepare` 或 TSP 研究任务的 `prepare.run.market`。默认按所选证券的快照原价日线确定回测日期，整天没有 K 线时直接跳过。沪深北共用人民币现金和持仓，每笔订单按证券历史分类计算涨跌幅、申报数量、费用和次一交易时段的可卖数量。严格运行仍会检查开盘观察、证券元数据与公司行为；单只证券缺失的行情按快照覆盖和执行策略处理。

日线可显式使用 `calendar_source="preset"` 逐日核对已公告日历。使用这个模式时，日期范围包括预热、回测与持仓解锁所需的下一交易日。按行情日期回放时，最后一根之后没有开盘记录的信号记入诊断。日线可用 `venue_id` 缩小证券范围。沪深逐笔当前使用 `2026.1.0` 和 `profile_format="tivelo-cn-profile-v4"`。采样盘口使用 v5 与 `observed_book_max_age_ms`。

北交所日线预设包含正常股票的 30% 涨跌幅、100 股起且按 1 股递增、单笔 100 万股上限、零股一次卖出和次一交易时段可卖。日线价格边界取官方允许区间内的有效报价。账户模型采用显式逐成交研究计费，佣金及最低收费来自请求，一并锁定到归档。特殊账户准入、累计申报及北交所逐笔撮合仍待验收。[北交所 2021 年规则](https://www.bse.cn/jygl_list/200010919.html) · [北交所 2026 年规则](https://www.bse.cn/jygl_list/200028217.html)

## 连续竞价申报价格

`tivelo-cn-profile-v4` 启用 `CNContinuousLimitOrders.check_limit_price`。它在既有价格预设的日期和分类上校验沪深普通 A 股限价，按申报方向取盘口基准，并同时核对每日涨跌幅与最小价位。连续竞价政策 2026.1.0 固定八个板块与生效日组合。沪深主板、创业板取 2% 和十个最小价位中较宽的一档，科创板按 102% / 98% 计算。按适用条款四舍五入至最小价位，保留距基准至少一档和最低正价的规则。[上交所 2023 年规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/repeal/rules/c/10824490/files/dcbe58edb194451d93f19b1f7dd8fb4c.docx) · [上交所 2026 年规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/10816492/files/58bd5ce4dfc94b57a5391f60eb2719ab.docx) · [深交所 2023 年规则](https://docs.static.szse.cn/www/lawrules/rule/repeal/rules/W020230217564423808793.pdf) · [深交所 2026 年规则](https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf)

先准备带已编译价格、费用和官方竞价日历的 v3 profile，再加入连续竞价政策引用。下面的 `instrument` 为申报时有效且当时可知的证券记录，`exchange_reference` 为该证券当日的交易所参考价。

```python
from decimal import Decimal

from tivelo_market_cn.adapter import create_rules
from tivelo_market_cn.order_prices import ContinuousBook, continuous_limit_reference

profile_v4 = profile_v3.model_copy(update={
    "profile_format": "tivelo-cn-profile-v4",
    "continuous_limit": continuous_limit_reference(version="2026.1.0"),
})
rules = create_rules(configuration_json=profile_v4.model_dump_json())
book = ContinuousBook.model_validate_json(book_json)
check = rules.check_limit_price(
    instrument, "buy", Decimal("2.11"), at=book.at, book=book,
    reference=exchange_reference,
)
```

`ContinuousBook` 需要同一标的、同一时段、覆盖申报时刻的人民币原价盘口，以及来源、修订和可知时间。买入依次使用卖一、买一、当日最近成交及当日交易所参考价，卖出时交换前两项。字段 `best_bid`、`best_ask`、`last_trade` 都必须提供，只有来源已确认无对应档位或成交时才填写 `null`。最近成交需要 `last_trade_at`，时间相同时由盘口接入方确认成交和盘口事件先于申报。晚到、错标的、跨时段、过期的盘口会被明确拒绝。交易所参考价还用于核对每日涨跌幅。

结果记录当时采用的基准、单侧边界、每日涨跌幅、条款和源修订。调用方可据 `price_valid`、`invalid_limit_tick`、`outside_daily_limits`、`outside_price_cage` 做价格校验。日线回测选择 v4 会返回 `UNSUPPORTED_CAPABILITY`。当前 v4 覆盖 2024–2026 年已核验规则范围内的连续竞价价格，集合竞价、临时停牌申报、账户资格和累计申报仍需各自的规则。[从归档创建并验证限价的用例](tests/test_order_prices.py)

已发布的 `order_book_observation` 可用原数据读器或 `PortableSnapshotReader` 做同样的校验。`book_dataset` 从清单选出，保留完整的 `dataset_id` 和 `version_id`。

```python
receipt = rules.check_snapshot_limit_price(
    instrument, "buy", Decimal("2.11"), at=submitted_at, reader=reader,
    dataset=book_dataset, reference=exchange_reference,
)
```

收据保存 `snapshot_id`、数据集引用和原价校验结果。查询要求 `event_at` 与申报时刻相同，`as_of` 取申报时刻。缺档和晚到返回 `DATA_COVERAGE_INSUFFICIENT`，只有 `historical_versions` 可进入此校验。接入方须用来源记录证明申报时实际可见的盘口顺序。[导入与查询命令](../tivelo-data/README.md#保存与查询历史盘口)

使用定时采样盘口做回测时，可选择 v5。以下 500 毫秒是示例，实际窗口由研究者按数据源的采样和配送设置。

```python
profile_v5 = profile_v4.model_copy(update={
    "profile_format": "tivelo-cn-profile-v5",
    "observed_book_max_age_ms": 500,
})
rules = create_rules(configuration_json=profile_v5.model_dump_json())
```

v5 从申报时已可知的修订中，选择事件时点最新的一条原价盘口。它必须与申报同属一段连续竞价，且从事件时点到申报时点的差值在窗口内。最新记录显示停牌时，受理报错。收据的 `book_event_at` 和 `book_available_at` 记录所用样本的两个时点，运行锁和报告记录窗口。该模式使用已知样本模拟申报价格校验。要逐时点重建当时的交易所盘口，使用 v4 和同时点见证。

通过回测包受理现金订单时，市场适配器还核对正式数量预设和股票 T+1。用 `resolve_quantity_rules` 取得各历史时段的适用数量，把对应的 `order_quantity_rules` 归档到基础条款后再编译价格。含 `remainder_once` 的配置使用规则格式 1.3。普通主板买入可进入受理。科创板、创业板、风险警示及退市整理买入需要进一步的投资者权限或累计约束，目前明确返回 `UNSUPPORTED_CAPABILITY`。[回测账户接入](../tivelo-backtest/README.md#连续竞价限价委托受理)

## 发行与待完成范围

已安装规则入口组为 `tivelo.market_rules.v1`，入口 ID 为 `tivelo.market.cn.cash-equity`，工厂 `adapter.create_rules(*, configuration_json)` 返回同一 `CNTradingRules`。`adapter.CNMarketProfile` 的旧 v1 包含 `configuration: RuleConfiguration` 与 `calendar: CalendarVersion`；v2 还保存费用编译输入，v3 再保存价格编译输入。调用者把完整配置以 JSON 固定，工厂不推断缺失日历、费率或历史规格；这提供了独立回测 CLI 的真实加载入口，仍不是完整官方市场预设。

库使用 `tivelo-contracts`、NumPy 和 Polars；只有兼容向量计算模块才导入后两者。独立 wheel 测试禁止宿主和两个引擎作为依赖。MIT 来源说明保留在 `LICENSE-MIT`，宿主选择规则与行情日期的说明见[数据与市场契约](../../docs/engine-platform/data-and-market-contracts.md)。

P3 仍需完成：真实历史证券规格的解析与覆盖、完整费用及结算政策、更多历史年份/临时市场事件、完整市场预设及严格回测的真实数据调用链。当前不宣称港股、美股、期货或完整 A 股交易账户模型已支持。
