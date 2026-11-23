"""Saved definitions from the former preset release, used only as compatibility fixtures."""

from app.indicators.chart import ChartIndicator, ChartLibrary


def presets() -> list[ChartIndicator]:
    def parameter(name, value):
        return {"name": name, "value": value, "min": 2, "max": 512, "integer": True}

    def output(key, formula, color="#f59e0b", **options):
        return {"key": key, "label": key, "formula": formula, "color": color, **options}

    def definition(key, name, outputs, parameters=(), pane="sub", format="number", precision=2):
        return ChartIndicator.model_validate(
            {
                "id": f"preset_{key}",
                "name": name,
                "pane": pane,
                "format": format,
                "precision": precision,
                "parameters": list(parameters),
                "outputs": outputs,
            }
        )

    periods = [5, 10, 20, 60]
    colors = ["#a1a1aa", "#3b82f6", "#f97316", "#8b5cf6"]
    result = [
        definition(
            key,
            label,
            [
                output(
                    f"{label}{n}",
                    f"{function}(C,N{i + 1})",
                    colors[i],
                    label=f"{label}{{N{i + 1}}}",
                )
                for i, n in enumerate(periods)
            ],
            [parameter(f"N{i + 1}", n) for i, n in enumerate(periods)],
            pane="main",
            format="price",
        )
        for key, label, function in (("ma", "MA", "MA"), ("ema", "EMA", "EMA_FIRST"))
    ]
    result.append(
        definition(
            "boll",
            "BOLL",
            [
                output("MID", "MA(C,N)"),
                output("UPPER", "MID+WIDTH*STD(C,N)", "#a855f7"),
                output("LOWER", "MID-WIDTH*STD(C,N)", "#06b6d4"),
            ],
            [
                parameter("N", 20),
                {"name": "WIDTH", "value": 2, "min": 0.1, "max": 10, "integer": False},
            ],
            pane="main",
            format="price",
        )
    )
    result.append(
        definition(
            "macd",
            "MACD",
            [
                output("DIF", "EMA_FIRST(C,SHORT)-EMA_FIRST(C,LONG)"),
                output("DEA", "EMA_FIRST(DIF,SIGNAL)", "#a855f7"),
                output("HIST", "(DIF-DEA)*2", "#C74040", style="histogram"),
            ],
            [parameter("SHORT", 12), parameter("LONG", 26), parameter("SIGNAL", 9)],
            precision=3,
        )
    )
    rsi = [
        output("DELTA", "C-REF(C,1)", hidden=True),
        output("GAIN", "IF(DELTA>0,DELTA,0)", hidden=True),
        output("LOSS", "IF(DELTA<0,-DELTA,0)", hidden=True),
    ]
    for i, n in enumerate((6, 14, 24)):
        loss = f"SMA_FIRST(LOSS,N{i + 1},1)"
        rsi.append(
            output(
                f"RSI{n}",
                f"100-100/(1+SMA_FIRST(GAIN,N{i + 1},1)/IF({loss}==0,0.000000000001,{loss}))",
                colors[i],
                label=f"RSI{{N{i + 1}}}",
            )
        )
    result.append(
        definition(
            "rsi", "RSI", rsi, [parameter(f"N{i + 1}", n) for i, n in enumerate((6, 14, 24))]
        )
    )
    result.append(
        definition(
            "kdj",
            "KDJ",
            [
                output("RSV", "(C-LLV(L,N))/(HHV(H,N)-LLV(L,N))*100", hidden=True),
                output("KVAL", "SMA_FIRST(RSV,SMOOTH1,1)", label="K"),
                output("DVAL", "SMA_FIRST(KVAL,SMOOTH2,1)", "#3b82f6", label="D"),
                output("JVAL", "3*KVAL-2*DVAL", "#8b5cf6", label="J"),
            ],
            [parameter("N", 9), parameter("SMOOTH1", 3), parameter("SMOOTH2", 3)],
        )
    )
    result.append(
        definition(
            "volume",
            "成交量",
            [
                output("VOLBAR", "V", "#C74040", style="volume", color_mode="candle", label="量"),
                output("VOLMA5", "MA(V,N1)", label="VOL{N1}"),
                output("VOLMA10", "MA(V,N2)", "#a855f7", label="VOL{N2}"),
            ],
            [parameter("N1", 5), parameter("N2", 10)],
            format="volume",
        )
    )
    result.append(
        definition(
            "bias",
            "乖离率 BIAS",
            [output("BIAS", "(C/MA(C,N)-1)*100")],
            [parameter("N", 20)],
            format="percent",
        )
    )
    return result


def migrate_library(raw: object) -> ChartLibrary:
    """Add presets once; preserve IDs, formulas and independent view overrides."""
    library = ChartLibrary.model_validate({} if raw is None else raw)
    if library.presets_version is not None:
        return library
    data = library.model_dump()
    existing = {d.id for d in library.definitions}
    additions = [d.model_dump() for d in presets() if d.id not in existing]
    # Name collisions never authorize overwriting user definitions or enabling them.
    data["definitions"].extend(additions)
    by_id = {d["id"]: d for d in additions}
    for view, defaults in (
        ("daily", ("preset_ma", "preset_volume")),
        ("minute", ("preset_volume",)),
    ):
        data["views"][view] = [
            {"id": id, "parameters": {p["name"]: p["value"] for p in by_id[id]["parameters"]}}
            for id in defaults
            if id not in existing
        ] + data["views"][view]
    data["presets_version"] = 1
    return ChartLibrary.model_validate(data)
