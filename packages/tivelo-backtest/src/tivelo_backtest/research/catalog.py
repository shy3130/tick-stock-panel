"""Legacy research catalog and dependency planning, independent of TSP.

The ordered catalog and all numerical/window semantics are extracted from
TSP's public factor registry. Financial metadata is not a correctness proof.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Literal, Protocol

Kind = Literal["base", "virtual", "composite", "custom"]
Direction = Literal["high", "low", "none"]
Unit = Literal["ratio", "pct", "score", "count", "days", "currency", "none"]
PitSource = Literal["financial_announce", "share_capital_announce", "none"]
Stability = Literal["stable", "experimental", "deprecated"]

_ALL_ASSETS = frozenset({"stock", "etf"})
_STOCK_ONLY = frozenset({"stock"})


@dataclass(frozen=True)
class FactorSpec:
    id: str
    label: str
    group: str
    formula_text: str
    kind: Kind = "base"
    version: int = 1
    # base: 空集合 = 已物化列自身; virtual: 展开到 enriched base 列
    dependencies: frozenset[str] = field(default_factory=frozenset)
    direction: Direction = "none"  # P1 不预填: 方向以最近检验 IC 符号为准 (见平台方案 §3.6)
    unit: Unit = "none"
    warmup_bars: int = 1
    pit: bool = False
    pit_source: PitSource = "none"
    asset_types: frozenset[str] = _ALL_ASSETS
    incremental_safe: bool = True
    scale_free: bool = True
    null_policy: Literal["keep", "drop_row"] = "keep"
    stability: Stability = "stable"
    tags: tuple[str, ...] = ()
    # 公式语言: "dsl" (受限公式) | "python" (T2 表达式级 Python, P3)
    lang: str = "dsl"
    # composite 专用: ((成员 id, 权重), ...); 其余类型为空
    components: tuple[tuple[str, float], ...] = ()
    # 保留中间引用; 子因子编辑后, 计算计划需要追踪其当前依赖。
    # 每个直接输入及沿公式路径额外需要的历史根数。展开后的 dependencies 仅是注册时的快照。
    input_windows: tuple[tuple[str, int], ...] = ()
    declared_warmup: int = 1

    @property
    def inputs(self) -> frozenset[str]:
        if self.input_windows:
            return frozenset(name for name, _ in self.input_windows)
        if self.components:
            return frozenset(name for name, _ in self.components)
        return self.dependencies

    def column_view(self) -> dict:
        """历史 FACTOR_COLUMNS 条目视图 (键与顺序兼容)。"""
        return {"id": self.id, "label": self.label, "group": self.group, "desc": self.formula_text}


def _base(fid: str, label: str, group: str, desc: str, **overrides) -> FactorSpec:
    return FactorSpec(id=fid, label=label, group=group, formula_text=desc, kind="base", **overrides)


def _virtual(fid: str, label: str, group: str, desc: str, deps: frozenset[str], **overrides) -> FactorSpec:
    return FactorSpec(
        id=fid, label=label, group=group, formula_text=desc,
        kind="virtual", dependencies=deps, **overrides,
    )


def _financial(fid: str, label: str, desc: str) -> FactorSpec:
    return FactorSpec(
        id=fid, label=label, group="财务", formula_text=desc,
        kind="base", pit=True, pit_source="financial_announce", asset_types=_STOCK_ONLY,
    )


# 顺序即历史 FACTOR_COLUMNS 顺序 (mining_schedule 取前 48 个, 不得重排)。
BUILTIN_FACTORS: tuple[FactorSpec, ...] = (
    # --- 动量 ---
    _base("momentum_5d", "5日动量", "动量", "5个交易日累计收益率"),
    _base("momentum_10d", "10日动量", "动量", "10个交易日累计收益率"),
    _base("momentum_20d", "20日动量", "动量", "20个交易日累计收益率"),
    _base("momentum_30d", "30日动量", "动量", "30个交易日累计收益率"),
    _base("momentum_60d", "60日动量", "动量", "60个交易日累计收益率"),
    _base("change_pct", "日涨跌幅", "动量", "当日收盘相对前收盘的收益率"),
    # --- 均线偏离 (虚拟) ---
    *(
        _virtual(
            f"ma{period}_bias", f"MA{period}乖离", "均线偏离", f"收盘价 / MA{period} - 1",
            deps=frozenset({"close", f"ma{period}"}),
        )
        for period in (5, 10, 20, 30, 60)
    ),
    *(
        _virtual(
            f"ema{period}_bias", f"EMA{period}乖离", "均线偏离", f"收盘价 / EMA{period} - 1",
            deps=frozenset({"close", f"ema{period}"}),
        )
        for period in (5, 10, 20, 30, 60)
    ),
    # --- 超买超卖 ---
    _base("rsi_6", "RSI(6)", "超买超卖", "6日相对强弱指标"),
    _base("rsi_14", "RSI(14)", "超买超卖", "14日相对强弱指标"),
    _base("rsi_24", "RSI(24)", "超买超卖", "24日相对强弱指标"),
    # --- 趋势 ---
    _base(
        "macd_hist", "MACD柱(原值)", "趋势",
        "兼容历史研究; 跨股票比较建议优先使用MACD柱强度",
        scale_free=False,
    ),
    _virtual("macd_dif_pct", "MACD DIF强度", "趋势", "MACD DIF / 收盘价", deps=frozenset({"close", "macd_dif"})),
    _virtual("macd_dea_pct", "MACD DEA强度", "趋势", "MACD DEA / 收盘价", deps=frozenset({"close", "macd_dea"})),
    _virtual("macd_hist_pct", "MACD柱强度", "趋势", "MACD柱 / 收盘价, 消除股价尺度影响", deps=frozenset({"close", "macd_hist"})),
    _base("kdj_k", "KDJ-K", "趋势", "KDJ指标K值"),
    _base("kdj_d", "KDJ-D", "趋势", "KDJ指标D值"),
    _base("kdj_j", "KDJ-J", "趋势", "KDJ指标J值"),
    _virtual(
        "boll_position", "布林位置", "趋势", "收盘价在布林带下轨到上轨之间的位置",
        deps=frozenset({"close", "boll_upper", "boll_lower"}),
    ),
    # --- 波动率 ---
    _base("annual_vol_20d", "20日波动率", "波动率", "20日收益率年化标准差"),
    _base("atr_14", "ATR(14)原值", "波动率", "兼容历史研究; 跨股票比较建议优先使用ATR相对波动", scale_free=False),
    _virtual("atr_pct", "ATR相对波动", "波动率", "ATR(14) / 收盘价", deps=frozenset({"close", "atr_14"})),
    _base("amplitude", "日振幅", "波动率", "当日高低价差 / 前收盘价"),
    _virtual(
        "boll_width", "布林带宽", "波动率", "布林带上下轨宽度 / MA20",
        deps=frozenset({"ma20", "boll_upper", "boll_lower"}),
    ),
    # --- 量价 ---
    _base("vol_ratio_5d", "5日量比", "量价", "当日成交量 / 前5日平均成交量"),
    _virtual(
        "vol_ratio_10d", "10日量比", "量价", "当日成交量 / 前10日平均成交量",
        deps=frozenset({"volume"}), warmup_bars=11,
    ),
    _virtual(
        "vol_trend_5_10", "成交量趋势", "量价", "5日平均成交量 / 10日平均成交量 - 1",
        deps=frozenset({"vol_ma5", "vol_ma10"}),
    ),
    _base("turnover_rate", "换手率", "量价", "使用历史时点流通股本计算的当日换手率"),
    _virtual(
        "turnover_ratio_5d", "换手率放大", "量价", "当日换手率 / 前5日平均换手率 - 1",
        deps=frozenset({"turnover_rate"}), warmup_bars=6,
    ),
    _virtual(
        "log_amount", "成交额对数", "量价", "ln(成交额 + 1), 降低极端规模影响",
        deps=frozenset({"amount"}), scale_free=False,
    ),
    _virtual(
        "amount_ratio_5d", "成交额放大", "量价", "当日成交额 / 前5日平均成交额 - 1",
        deps=frozenset({"amount"}), warmup_bars=6,
    ),
    # --- 价格位置 ---
    _virtual("gap_return", "开盘跳空", "价格位置", "开盘价 / 前收盘价 - 1", deps=frozenset({"open", "prev_close"})),
    _virtual("intraday_return", "日内收益", "价格位置", "收盘价 / 开盘价 - 1", deps=frozenset({"open", "close"})),
    _virtual(
        "close_position", "收盘位置", "价格位置", "收盘价在当日最低价到最高价之间的位置",
        deps=frozenset({"high", "low", "close"}),
    ),
    _virtual(
        "distance_to_high_60d", "距60日高点", "价格位置", "收盘价 / 60日最高收盘价 - 1",
        deps=frozenset({"close", "high_60d"}),
    ),
    _virtual(
        "distance_from_low_60d", "距60日低点", "价格位置", "收盘价 / 60日最低收盘价 - 1",
        deps=frozenset({"close", "low_60d"}),
    ),
    _virtual(
        "vwap_bias", "VWAP乖离", "价格位置", "收盘价 / 当日成交均价 - 1, 成交均价 = 成交额 / (成交量x100)",
        deps=frozenset({"close", "volume", "amount"}),
    ),
    # --- 收益形态 (虚拟, 滚动窗口) ---
    _virtual(
        "max_ret_20d", "20日最大单日涨幅", "收益形态", "近20个交易日单日涨幅最大值(彩票效应, 高值代表博彩型特征强)",
        deps=frozenset({"close"}), warmup_bars=21,
    ),
    _virtual(
        "ret_skew_20d", "20日收益偏度", "收益形态", "近20个交易日日收益偏度, 高值代表右偏(偶发大涨)",
        deps=frozenset({"close"}), warmup_bars=21,
    ),
    _virtual(
        "up_days_20d", "20日上涨天数", "收益形态", "近20个交易日中上涨天数(0~20)",
        deps=frozenset({"close"}), warmup_bars=21,
    ),
    # --- 流动性 (虚拟) ---
    _virtual(
        "amihud_20d", "20日Amihud非流动性", "流动性", "近20日平均 |日涨跌幅| / 成交额(亿元), 高值代表流动性差",
        deps=frozenset({"close", "amount"}), warmup_bars=21,
    ),
    _virtual(
        "turnover_z_60d", "换手率60日z分", "流动性", "(当日换手率 - 前60日均值) / 前60日标准差, 衡量换手异动",
        deps=frozenset({"turnover_rate"}), warmup_bars=61,
    ),
    # --- 量价 (续) ---
    _virtual(
        "vol_price_corr_20d", "20日量价相关", "量价", "近20个交易日日涨跌幅与成交量的相关系数, 高值代表量价同向",
        deps=frozenset({"close", "volume"}), warmup_bars=21,
    ),
    _virtual(
        "vol_trend_5_60", "量能趋势(5/60)", "量价", "5日平均成交量 / 60日平均成交量 - 1",
        deps=frozenset({"volume"}), warmup_bars=60,
    ),
    # --- 涨停基因 (虚拟) ---
    _virtual(
        "limit_up_count_20d", "涨停基因(20日)", "涨停基因", "近20个交易日涨停次数",
        deps=frozenset({"consecutive_limit_ups"}), warmup_bars=21,
    ),
    _virtual(
        "limit_up_count_60d", "涨停基因(60日)", "涨停基因", "近60个交易日涨停次数",
        deps=frozenset({"consecutive_limit_ups"}), warmup_bars=61,
    ),
    # --- 财务 (点时, 仅股票) ---
    _financial("pb_latest", "市净率(最新公告)", "收盘价 / 最新已公告每股净资产; 无财务数据或公告前为空"),
    _financial("roe_latest", "ROE(最新公告)", "最新已公告净资产收益率(%); 无财务数据或公告前为空"),
    _financial("gross_margin_latest", "毛利率(最新公告)", "最新已公告销售毛利率(%)"),
    _financial("net_margin_latest", "净利率(最新公告)", "最新已公告销售净利率(%)"),
    _financial("revenue_yoy_latest", "营收增速(最新公告)", "最新已公告营业收入同比(%)"),
    _financial("net_income_yoy_latest", "净利增速(最新公告)", "最新已公告归母净利润同比(%)"),
    _financial("debt_ratio_latest", "资产负债率(最新公告)", "最新已公告资产负债率(%)"),
    # --- 扩充批次 (2026-09-05): 规模/收益分解/长窗口/下行风险/量能潮/换手水平 ---
    _virtual(
        "log_float_mv", "流通市值对数", "规模",
        "ln(收盘价 x 当日成交量 / 换手率), 由换手率反推流通股本, 高值代表大盘",
        deps=frozenset({"close", "volume", "turnover_rate"}), scale_free=False,
    ),
    _virtual(
        "momentum_120d", "120日动量", "动量",
        "120个交易日累计收益率 (中期动量, 与短窗口互补)",
        deps=frozenset({"close"}), warmup_bars=121,
    ),
    _virtual(
        "mom_accel_20_60", "动量加速度", "动量",
        "20日动量 - 60日动量, 衡量近期动量相对中期是否增强",
        deps=frozenset({"momentum_20d", "momentum_60d"}),
    ),
    _virtual(
        "rsi_14_delta_5d", "RSI五日变化", "超买超卖",
        "RSI(14) - 5日前的RSI(14), 衡量强弱指标的边际变化",
        deps=frozenset({"rsi_14"}), warmup_bars=6,
    ),
    _virtual(
        "overnight_ret_20d", "20日隔夜收益", "收益形态",
        "近20日累计隔夜收益(开盘价/前收盘-1求和), A股隔夜与日内收益的定价机制不同",
        deps=frozenset({"open", "prev_close"}), warmup_bars=21,
    ),
    _virtual(
        "intraday_ret_20d", "20日日内收益", "收益形态",
        "近20日累计日内收益(收盘价/开盘价-1求和), 与隔夜收益构成收益分解",
        deps=frozenset({"open", "close"}), warmup_bars=21,
    ),
    _virtual(
        "downside_vol_20d", "20日下行波动", "波动率",
        "sqrt(近20日 min(日收益,0)^2 均值), 只度量下跌侧风险",
        deps=frozenset({"close"}), warmup_bars=21,
    ),
    _virtual(
        "vol_regime_5_60", "波动率状态(5/60)", "波动率",
        "5日收益标准差 / 60日收益标准差, 高值代表波动骤然放大",
        deps=frozenset({"close"}), warmup_bars=61,
    ),
    _virtual(
        "amplitude_trend_20_60", "振幅趋势(20/60)", "波动率",
        "20日平均振幅 / 60日平均振幅 - 1",
        deps=frozenset({"amplitude"}), warmup_bars=61,
    ),
    _virtual(
        "obv_trend_20d", "20日量能潮", "量价",
        "近20日 sign(日收益)x成交量 之和 / (20日均量x20), 有界[-1,1], 净买入方向的一致性",
        deps=frozenset({"close", "volume"}), warmup_bars=21,
    ),
    _virtual(
        "amount_mean_20d", "20日均成交额(亿)", "量价",
        "近20日平均成交额(亿元), 规模/流动性水平量",
        deps=frozenset({"amount"}), warmup_bars=21, scale_free=False,
    ),
    _virtual(
        "turnover_mean_20d", "20日均换手", "流动性",
        "近20日平均换手率, A股经典低换手溢价因子",
        deps=frozenset({"turnover_rate"}), warmup_bars=21,
    ),
    _virtual(
        "turnover_std_20d", "20日换手波动", "流动性",
        "近20日换手率标准差 / 均值 (变异系数), 衡量交易活跃的稳定性",
        deps=frozenset({"turnover_rate"}), warmup_bars=21,
    ),
    _virtual(
        "position_240d", "一年价格位置", "价格位置",
        "收盘价在近240个交易日最低价到最高价之间的位置 (0~1)",
        deps=frozenset({"close"}), warmup_bars=241,
    ),
    _virtual(
        "distance_to_high_240d", "距一年高点", "价格位置",
        "收盘价 / 240日最高收盘价 - 1, 接近0代表贴近一年新高",
        deps=frozenset({"close"}), warmup_bars=241,
    ),
    _virtual(
        "kdj_kd_diff", "KDJ K-D差", "趋势",
        "KDJ K值 - D值, 正值代表快线在慢线上方",
        deps=frozenset({"kdj_k", "kdj_d"}),
    ),
)

class FactorLookup(Protocol):
    """Read-only factor metadata supplied by the caller; no implicit file I/O."""

    def get(self, fid: str) -> FactorSpec | None: ...


@dataclass(frozen=True)
class FactorCatalog:
    """An isolated snapshot of legacy research metadata, not a PIT/scale proof.

    Unknown dependency leaves remain external input columns, as in the v1
    registry. Container values are copied so callers cannot mutate a snapshot.
    """

    specs: tuple[FactorSpec, ...] = BUILTIN_FACTORS
    _by_id: Mapping[str, FactorSpec] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        by_id: dict[str, FactorSpec] = {}
        for spec in self.specs:
            if not isinstance(spec, FactorSpec):
                raise TypeError("factor catalog requires FactorSpec values")
            if spec.id in by_id:
                raise ValueError(f"duplicate factor id: {spec.id}")
            by_id[spec.id] = replace(
                spec,
                dependencies=frozenset(spec.dependencies),
                asset_types=frozenset(spec.asset_types),
                tags=tuple(spec.tags),
                components=tuple(tuple(item) for item in spec.components),
                input_windows=tuple(tuple(item) for item in spec.input_windows),
            )
        object.__setattr__(self, "specs", tuple(by_id.values()))
        object.__setattr__(self, "_by_id", MappingProxyType(by_id))
        factor_calculation_order(by_id, catalog=self)

    def get(self, fid: str) -> FactorSpec | None:
        return self._by_id.get(fid)

    def with_factors(self, specs: Iterable[FactorSpec]) -> FactorCatalog:
        """Return a new validated snapshot; replacements require a new version."""
        updated = dict(self._by_id)
        for spec in specs:
            previous = updated.get(spec.id)
            if previous is not None and previous.version >= spec.version:
                raise ValueError(f"factor version must increase: {spec.id}")
            updated[spec.id] = spec
        return FactorCatalog(tuple(updated.values()))


# 这些指标列在历史 parquet 中按需补算。把形成指标本身所需的窗口计入外层公式。
_INDICATOR_HISTORY = {
    **{f"ma{n}": n for n in (5, 10, 20, 30, 60)},
    **{f"ema{n}": n for n in (5, 10, 20, 30, 60)},
    **{f"momentum_{n}d": n + 1 for n in (5, 10, 20, 30, 60)},
    **{f"rsi_{n}": n + 1 for n in (6, 14, 24)},
    "vol_ma5": 5, "vol_ma10": 10, "vol_ratio_5d": 6,
    "high_60d": 60, "low_60d": 60, "boll_upper": 20, "boll_lower": 20,
    "annual_vol_20d": 21, "atr_14": 15,
    "macd_dif": 26, "macd_dea": 35, "macd_hist": 35,
    "kdj_k": 9, "kdj_d": 9, "kdj_j": 9,
}


def factor_dependencies(fids, *, catalog: FactorLookup) -> frozenset[str]:
    """递归展开依赖到 enriched base 列; 未知 id 原样保留 (与 scoring_dependencies 历史语义一致)。"""
    return frozenset(
        fid for fid in factor_calculation_order(fids, catalog=catalog)
        if (spec := catalog.get(fid)) is None or not spec.inputs
    )


def factor_warmup_bars(fids, *, catalog: FactorLookup) -> int:
    """沿当前依赖链累计窗口; 并列输入取最大值。"""
    warmups = {}
    for fid in factor_calculation_order(fids, catalog=catalog):
        spec = catalog.get(fid)
        if spec is None:
            warmups[fid] = _INDICATOR_HISTORY.get(fid, 1)
        elif spec.input_windows:
            warmups[fid] = max(spec.declared_warmup, *(
                warmups[name] + bars for name, bars in spec.input_windows
            ))
        elif spec.components:
            warmups[fid] = max(warmups[name] for name in spec.inputs)
        elif spec.inputs:
            warmups[fid] = spec.warmup_bars - 1 + max(warmups[name] for name in spec.inputs)
        else:
            warmups[fid] = max(spec.warmup_bars, _INDICATOR_HISTORY.get(fid, 1))
    return max(warmups.values(), default=1)


def factor_calculation_order(fids, *, catalog: FactorLookup) -> list[str]:
    """计算依赖的拓扑顺序, 保留 DSL 引用的中间因子和组合成员。"""
    ordered: list[str] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(fid: str) -> None:
        if fid in visiting:
            raise ValueError(f"factor dependency cycle: {fid}")
        if fid in visited:
            return
        visiting.add(fid)
        spec = catalog.get(fid)
        for dependency in sorted(spec.inputs if spec is not None else ()):
            visit(dependency)
        visiting.remove(fid)
        visited.add(fid)
        ordered.append(fid)

    for fid in sorted(fids):
        visit(str(fid))
    return ordered


def virtual_dependencies() -> dict[str, frozenset[str]]:
    """历史 VIRTUAL_SCORING_DEPENDENCIES 兼容视图。"""
    return {
        spec.id: spec.dependencies
        for spec in BUILTIN_FACTORS
        if spec.kind == "virtual" and spec.dependencies
    }


def scoring_warmups() -> dict[str, int]:
    """历史 _ROLLING_SCORING_WARMUP 兼容视图 (仅滚动窗口虚拟因子)。"""
    return {
        spec.id: spec.warmup_bars
        for spec in BUILTIN_FACTORS
        if spec.kind == "virtual" and spec.warmup_bars > 1
    }


BUILTIN_CATALOG = FactorCatalog()
