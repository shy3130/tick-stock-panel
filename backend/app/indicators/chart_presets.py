"""Built-in chart formulas and defaults for an unconfigured workspace."""

from app.indicators.chart import (
    ChartIndicator,
    ChartLibrary,
    ChartOutput,
    ChartSelection,
    VolumeCompareSettings,
)


def _line(key: str, formula: str, color: str, *, label: str | None = None) -> ChartOutput:
    return ChartOutput(
        key=key, label=label or key, formula=formula, color=color, color_mode="fixed"
    )


def _rsi(period: int, color: str) -> ChartOutput:
    gain = f"SMA_FIRST(GAIN,{period},1)"
    loss = f"SMA_FIRST(LOSS,{period},1)"
    return _line(f"RSI{period}", f"100-100/(1+{gain}/MAX({loss},0.000000000001))", color)


_MA = ChartIndicator(
    id="builtin_ma",
    name="MA",
    pane="main",
    format="price",
    outputs=[
        _line("MA5", "MA(C,5)", "#A1A1A1"),
        _line("MA10", "MA(C,10)", "#3B82F6"),
        _line("MA20", "MA(C,20)", "#F97316"),
        _line("MA60", "MA(C,60)", "#8B5CF6"),
    ],
)

_VOLUME = ChartIndicator(
    id="builtin_volume",
    name="成交量",
    format="volume",
    outputs=[
        ChartOutput(
            key="VOLBAR", label="量", formula="V", style="volume",
            color="#C74040", color_mode="candle",
        ),
        _line("VOL5", "MA(V,5)", "#FACC15"),
        _line("VOL10", "MA(V,10)", "#8B5CF6"),
    ],
)

BUILTIN_CHART_INDICATORS = (
    _MA,
    ChartIndicator(
        id="builtin_macd",
        name="MACD",
        precision=3,
        outputs=[
            _line("DIF", "EMA_FIRST(C,12)-EMA_FIRST(C,26)", "#FACC15"),
            _line("DEA", "EMA_FIRST(DIF,9)", "#8B5CF6"),
            ChartOutput(
                key="MACD", label="MACD", formula="(DIF-DEA)*2", style="histogram",
                color="#C74040", color_mode="sign",
            ),
        ],
    ),
    _VOLUME,
    ChartIndicator(
        id="builtin_rsi",
        name="RSI",
        precision=1,
        outputs=[
            ChartOutput(key="GAIN", label="上涨额", formula="MAX(C-REF(C,1),0)", hidden=True),
            ChartOutput(key="LOSS", label="下跌额", formula="MAX(REF(C,1)-C,0)", hidden=True),
            _rsi(6, "#FACC15"),
            _rsi(14, "#3B82F6"),
            _rsi(24, "#8B5CF6"),
        ],
    ),
    ChartIndicator(
        id="builtin_kdj",
        name="KDJ",
        precision=1,
        outputs=[
            ChartOutput(
                key="RSV", label="RSV",
                formula="100*(C-LLV(L,9))/MAX(HHV(H,9)-LLV(L,9),0)", hidden=True,
            ),
            _line("K", "SMA_FIRST(RSV,3,1)", "#FACC15"),
            _line("D", "SMA_FIRST(K,3,1)", "#3B82F6"),
            _line("J", "3*K-2*D", "#8B5CF6"),
        ],
    ),
    ChartIndicator(
        id="builtin_boll",
        name="BOLL",
        pane="main",
        format="price",
        outputs=[
            _line("MID", "MA(C,20)", "#F97316", label="中轨"),
            _line("UPPER", "MID+2*STD(C,20)", "#D58BDB", label="上轨"),
            _line("LOWER", "MID-2*STD(C,20)", "#22D3EE", label="下轨"),
        ],
    ),
)


def default_chart_library() -> ChartLibrary:
    return ChartLibrary(
        definitions=[_MA.model_copy(deep=True), _VOLUME.model_copy(deep=True)],
        views={
            "daily": [
                ChartSelection(id=_MA.id),
                ChartSelection(id=_VOLUME.id, volume_compare=VolumeCompareSettings()),
            ],
            "minute": [],
        },
    )


def migrate_library(raw: object) -> ChartLibrary:
    # Preserve historical IDs and version markers, including explicit empty libraries.
    return ChartLibrary.model_validate({} if raw is None else raw)
