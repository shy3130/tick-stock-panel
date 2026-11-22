"""Built-in chart formulas and defaults for an unconfigured workspace."""

from app.indicators.chart import ChartIndicator, ChartLibrary, ChartSelection, VolumeCompareSettings

_MA = ChartIndicator(
    id="builtin_ma",
    name="MA",
    syntax="tdx",
    pane="main",
    format="price",
    source=(
        "MA5:IF(BARSCOUNT(C)>=5,MA(C,5),DRAWNULL),COLORA1A1A1;\n"
        "MA10:IF(BARSCOUNT(C)>=10,MA(C,10),DRAWNULL),COLORF6823B;\n"
        "MA20:IF(BARSCOUNT(C)>=20,MA(C,20),DRAWNULL),COLOR1673F9;\n"
        "MA60:IF(BARSCOUNT(C)>=60,MA(C,60),DRAWNULL),COLORF65C8B;"
    ),
)

_VOLUME = ChartIndicator(
    id="builtin_volume",
    name="成交量",
    syntax="tdx",
    format="volume",
    source=(
        "量:VOL,VOLSTICK;\n"
        "VOL5:IF(BARSCOUNT(VOL)>=5,MA(VOL,5),DRAWNULL),COLOR15CCFA;\n"
        "VOL10:IF(BARSCOUNT(VOL)>=10,MA(VOL,10),DRAWNULL),COLORF65C8B;"
    ),
)

BUILTIN_CHART_INDICATORS = (
    _MA,
    ChartIndicator(
        id="builtin_macd",
        name="MACD",
        syntax="tdx",
        precision=3,
        source=(
            "DIF:EMA(C,12)-EMA(C,26),COLOR15CCFA;\n"
            "DEA:EMA(DIF,9),COLORF65C8B;\n"
            "MACD:(DIF-DEA)*2,COLORSTICK;"
        ),
    ),
    _VOLUME,
    ChartIndicator(
        id="builtin_rsi",
        name="RSI",
        syntax="tdx",
        precision=1,
        source=(
            "LC:=IF(BARSCOUNT(C)=1,C,REF(C,1));\n"
            "GAIN:=MAX(C-LC,0);\n"
            "LOSS:=MAX(LC-C,0);\n"
            "RSI6:100-100/(1+SMA(GAIN,6,1)/MAX(SMA(LOSS,6,1),0.000000000001)),COLOR15CCFA;\n"
            "RSI14:100-100/(1+SMA(GAIN,14,1)/MAX(SMA(LOSS,14,1),0.000000000001)),COLORF6823B;\n"
            "RSI24:100-100/(1+SMA(GAIN,24,1)/MAX(SMA(LOSS,24,1),0.000000000001)),COLORF65C8B;"
        ),
    ),
    ChartIndicator(
        id="builtin_kdj",
        name="KDJ",
        syntax="tdx",
        precision=1,
        source=(
            "LL:=LLV(L,9);\n"
            "SPAN9:=HHV(H,9)-LL;\n"
            "RSV:=IF(BARSCOUNT(C)>=9 AND SPAN9>0,100*(C-LL)/SPAN9,DRAWNULL);\n"
            "K:SMA(RSV,3,1),COLOR15CCFA;\n"
            "D:SMA(K,3,1),COLORF6823B;\n"
            "J:3*K-2*D,COLORF65C8B;"
        ),
    ),
    ChartIndicator(
        id="builtin_boll",
        name="BOLL",
        syntax="tdx",
        pane="main",
        format="price",
        source=(
            "中轨:IF(BARSCOUNT(C)>=20,MA(C,20),DRAWNULL),COLOR1673F9;\n"
            "上轨:中轨+2*STD(C,20),COLORDB8BD5;\n"
            "下轨:中轨-2*STD(C,20),COLOREED322;"
        ),
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
