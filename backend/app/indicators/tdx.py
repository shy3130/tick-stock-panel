"""Vector TDX chart evaluator; bounded data only, no I/O or Python execution."""

from __future__ import annotations

import ast
import math

import numpy as np
import polars as pl

from app.indicators.tdx_syntax import FIELDS, Program, error


def truth(value):
    value = np.asarray(value, dtype=float)
    return np.isfinite(value) & (value != 0)


class Evaluator:
    def __init__(self, frame, timeframe, parameters, precision):
        self.size = frame.height
        self.times = frame["time"].to_list()
        self.precision = precision
        self.indices = np.arange(self.size)
        self.candle_colors = (
            np.where(
                frame["close"].to_numpy() < frame["open"].to_numpy(), "#2D9B65", "#C74040"
            ).tolist()
            if "open" in frame.columns
            else ["#C74040"] * self.size
        )
        self.env = {
            name: frame[column].cast(pl.Float64).to_numpy()
            if column in frame.columns
            else np.full(self.size, np.nan)
            for name, column in FIELDS.items()
        }
        self.env.update({name.upper(): float(value) for name, value in parameters})
        dates = [str(value)[:10] for value in self.times]
        self.env.update(
            {
                "DRAWNULL": np.nan,
                "TRUE": 1.0,
                "FALSE": 0.0,
                "BARSCOUNT": self.indices + 1.0,
                "TOTALBARSCOUNT": float(self.size),
                "CURRBARSCOUNT": self.size - self.indices,
                "ISLASTBAR": (self.indices == self.size - 1).astype(float),
                "YEAR": np.array([int(day[:4]) for day in dates]),
                "MONTH": np.array([int(day[5:7]) for day in dates]),
                "DAY": np.array([int(day[8:10]) for day in dates]),
                "DATE": np.array(
                    [
                        (int(day[:4]) - 1900) * 10000 + int(day[5:7]) * 100 + int(day[8:10])
                        for day in dates
                    ]
                ),
                "WEEKDAY": (np.array(dates, dtype="datetime64[D]").astype(int) + 4) % 7,
                "PERIOD": 5.0 if timeframe == "daily" else 0.0,
                "TIME": np.array(
                    [
                        int(str(value)[11:13]) * 100 + int(str(value)[14:16])
                        if len(str(value)) > 10
                        else 0
                        for value in self.times
                    ]
                ),
                "TIME2": np.array(
                    [
                        int(str(value)[11:13]) * 10000 + int(str(value)[14:16]) * 100
                        if len(str(value)) > 10
                        else 0
                        for value in self.times
                    ]
                ),
            }
        )
        self.heavy_work = 0

    def array(self, value, dtype=float):
        result = np.asarray(value, dtype=dtype)
        return np.full(self.size, result.item(), dtype=dtype) if result.ndim == 0 else result

    def periods(self, value, minimum=0):
        result = self.array(value)
        if np.any(~np.isfinite(result)) or np.any(result != np.floor(result)):
            raise ValueError("周期必须是有效整数；动态周期的起始无效值请先用 IF 处理")
        if np.any(result < minimum):
            raise ValueError(
                "不支持未来引用或负周期" if np.any(result < 0) else f"周期必须至少为 {minimum}"
            )
        if np.any(result > 4096):
            raise ValueError("周期超过 4096 根上限")
        return result.astype(int)

    def scalar(self, value):
        values = self.array(value)
        if not len(values) or not np.all(values == values[0]) or not np.isfinite(values[0]):
            raise ValueError("该参数必须是有限常量")
        return float(values[0])

    def div(self, a, b):
        a, b = self.array(a), self.array(b)
        return np.divide(
            a, b, out=np.full(self.size, np.nan), where=np.isfinite(a) & np.isfinite(b) & (b != 0)
        )

    def ref(self, a, n):
        a, periods = self.array(a), self.periods(n)
        target = self.indices - periods
        return np.where(target >= 0, a[np.maximum(target, 0)], np.nan)

    def window(self, a, period, operation):
        a, period = self.array(a), self.periods(period)
        starts = np.maximum(0, self.indices - period + 1)
        starts[period == 0] = 0
        valid = np.isfinite(a)
        clean = np.where(valid, a, 0.0)

        def sums(values):
            prefix = np.r_[0.0, np.cumsum(values)]
            return prefix[1:] - prefix[starts]

        count, total = sums(valid), sums(clean)
        mean = np.divide(total, count, out=np.full(self.size, np.nan), where=count > 0)
        if operation == "SUM":
            return np.where(count > 0, total, np.nan)
        if operation == "MA":
            return mean
        if operation in {"STD", "STDP", "VAR", "VARP", "DEVSQ"}:
            ddof = 1 if operation in {"STD", "VAR"} else 0
            if np.all(period == period[0]) and period[0] > 0:
                result = (
                    pl.Series(a, nan_to_null=True)
                    .rolling_var(int(period[0]), min_samples=ddof + 1, ddof=ddof)
                    .to_numpy()
                )
            elif np.all(period == 0):
                result = np.full(self.size, np.nan)
                seen = 0
                running_mean = squared = 0.0
                for i, value in enumerate(a):
                    if np.isfinite(value):
                        seen += 1
                        delta = value - running_mean
                        running_mean += delta / seen
                        squared += delta * (value - running_mean)
                    if seen > ddof:
                        result[i] = max(0.0, squared / (seen - ddof))
            else:
                self.heavy_work += int(np.sum(self.indices - starts + 1))
                if self.heavy_work > 20_000_000:
                    raise ValueError("动态标准差计算量超过上限，请减小数据范围")
                result = np.array(
                    [
                        np.var(a[start : i + 1][valid[start : i + 1]], ddof=ddof)
                        if count[i] > ddof
                        else np.nan
                        for i, start in enumerate(starts)
                    ]
                )
            if operation == "DEVSQ":
                return result * count
            return np.sqrt(result) if operation.startswith("STD") else result
        if operation == "WMA":
            weighted = sums(clean * (self.indices + 1)) - starts * total
            weights = sums(valid * (self.indices + 1)) - starts * count
            return np.divide(weighted, weights, out=np.full(self.size, np.nan), where=weights > 0)
        if operation == "AVEDEV":
            self.heavy_work += int(np.sum(self.indices - starts + 1))
            if self.heavy_work > 20_000_000:
                raise ValueError("平均绝对偏差计算量超过上限，请减小周期或数据范围")
            return np.array(
                [
                    np.mean(np.abs(a[start : i + 1][valid[start : i + 1]] - mean[i]))
                    if count[i]
                    else np.nan
                    for i, start in enumerate(starts)
                ]
            )
        if operation in {"HHV", "LLV", "HHVBARS", "LLVBARS"}:
            high = operation.startswith("HHV")
            values = np.where(valid, a, -np.inf if high else np.inf)
            positions = np.where(valid, self.indices, -1)
            tables = [(values, positions)]
            width = 1
            while width * 2 <= self.size:
                right_v = np.r_[values[width:], np.full(width, -np.inf if high else np.inf)]
                right_i = np.r_[positions[width:], np.full(width, -1)]
                choose_right = (right_v >= values if high else right_v <= values) & (right_i >= 0)
                values = np.where(choose_right, right_v, values)
                positions = np.where(choose_right, right_i, positions)
                tables.append((values, positions))
                width *= 2
            lengths = self.indices - starts + 1
            levels = np.floor(np.log2(lengths)).astype(int)
            result = np.full(self.size, np.nan)
            for level in np.unique(levels):
                mask = levels == level
                left = starts[mask]
                right = self.indices[mask] - (1 << int(level)) + 1
                values, positions = tables[int(level)]
                choose = values[right] >= values[left] if high else values[right] <= values[left]
                chosen = np.where(choose, positions[right], positions[left])
                result[mask] = (
                    self.indices[mask] - chosen
                    if operation.endswith("BARS")
                    else np.where(choose, values[right], values[left])
                )
            return np.where(count > 0, result, np.nan)
        raise ValueError(f"不支持窗口函数 {operation}")

    def expression(self, node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return self.env[node.id]
        if isinstance(node, ast.UnaryOp):
            value = self.array(self.expression(node.operand))
            if isinstance(node.op, ast.Not):
                return (np.isfinite(value) & ~truth(value)).astype(float)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp):
            a, b = self.array(self.expression(node.left)), self.array(self.expression(node.right))
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            if isinstance(node.op, ast.Div):
                return self.div(a, b)
            if isinstance(node.op, ast.Mod):
                return np.remainder(a, np.where(b == 0, np.nan, b))
        if isinstance(node, ast.BoolOp):
            values = [truth(self.array(self.expression(value))) for value in node.values]
            return (
                np.logical_and.reduce(values)
                if isinstance(node.op, ast.And)
                else np.logical_or.reduce(values)
            ).astype(float)
        if isinstance(node, ast.Compare):
            left = self.array(self.expression(node.left))
            result = np.ones(self.size, dtype=bool)
            comparisons = {
                ast.Eq: np.equal,
                ast.NotEq: np.not_equal,
                ast.Lt: np.less,
                ast.LtE: np.less_equal,
                ast.Gt: np.greater,
                ast.GtE: np.greater_equal,
            }
            for operator, operand in zip(node.ops, node.comparators, strict=True):
                right = self.array(self.expression(operand))
                result &= (
                    np.isfinite(left)
                    & np.isfinite(right)
                    & comparisons[type(operator)](left, right)
                )
                left = right
            return result.astype(float)
        if isinstance(node, ast.Call):
            return self.call(node.func.id, [self.expression(arg) for arg in node.args])
        raise ValueError("不支持的表达式")

    def call(self, name, args):
        if name in {
            "MA",
            "SUM",
            "HHV",
            "LLV",
            "HHVBARS",
            "LLVBARS",
            "STD",
            "STDP",
            "VAR",
            "VARP",
            "AVEDEV",
            "DEVSQ",
            "WMA",
            "REF",
            "COUNT",
            "EVERY",
            "EXIST",
        }:
            period = self.array(args[1])
            missing = ~np.isfinite(period)
            if np.any(missing):
                return np.where(
                    missing,
                    np.nan,
                    self.call(name, [args[0], np.where(missing, 1.0, period), *args[2:]]),
                )
        if name in {
            "MA",
            "SUM",
            "HHV",
            "LLV",
            "HHVBARS",
            "LLVBARS",
            "STD",
            "STDP",
            "VAR",
            "VARP",
            "AVEDEV",
            "DEVSQ",
            "WMA",
        }:
            return self.window(*args, name)
        if name == "REF":
            return self.ref(*args)
        if name in {"EMA", "EXPMA", "EXPMEMA", "SMA", "MEMA"}:
            period = self.scalar(args[1])
            self.periods(period, 1)
            weight = self.scalar(args[2]) if name == "SMA" else 1.0
            if not 0 < weight <= period:
                raise ValueError("SMA 权重必须满足 0 < M <= N")
            alpha = 2 / (period + 1) if name in {"EMA", "EXPMA", "EXPMEMA"} else weight / period
            return (
                pl.Series(self.array(args[0]), nan_to_null=True)
                .ewm_mean(
                    alpha=alpha,
                    adjust=False,
                    ignore_nulls=True,
                    min_samples=int(period) if name == "EXPMEMA" else 1,
                )
                .to_numpy()
            )
        if name == "DMA":
            values, weights = self.array(args[0]), self.array(args[1])
            result = np.full(self.size, np.nan)
            previous = np.nan
            if np.any(np.isfinite(weights) & ((weights < 0) | (weights > 1))):
                raise ValueError("DMA 权重必须在 0～1 内")
            for i, (value, weight) in enumerate(zip(values, weights, strict=True)):
                if not np.isfinite(value) or not np.isfinite(weight):
                    continue
                previous = (
                    value if not np.isfinite(previous) else weight * value + (1 - weight) * previous
                )
                result[i] = previous
            return result
        if name in {"IF", "IFF", "IFN"}:
            condition = truth(self.array(args[0]))
            condition = ~condition if name == "IFN" else condition
            dtype = (
                object
                if any(np.asarray(value).dtype.kind in {"O", "U"} for value in args[1:])
                else float
            )
            return np.where(condition, self.array(args[1], dtype), self.array(args[2], dtype))
        if name in {"COUNT", "EVERY", "EXIST"}:
            period = self.periods(args[1])
            values = truth(self.array(args[0])).astype(float)
            count = self.window(values, period, "SUM")
            return (
                count
                if name == "COUNT"
                else (
                    (count > 0)
                    if name == "EXIST"
                    else count == np.where(period == 0, self.indices + 1, period)
                ).astype(float)
            )
        if name in {"CROSS", "LONGCROSS"}:
            a, b = self.array(args[0]), self.array(args[1])
            prev_a, prev_b = self.ref(a, 1), self.ref(b, 1)
            result = (
                (a > b)
                & (prev_a <= prev_b)
                & np.isfinite(a)
                & np.isfinite(b)
                & np.isfinite(prev_a)
                & np.isfinite(prev_b)
            )
            if name == "LONGCROSS":
                result &= self.call("EVERY", [self.ref((a < b).astype(float), 1), args[2]]).astype(
                    bool
                )
            return result.astype(float)
        if name in {"BARSLAST", "BARSSINCE", "BARSLASTCOUNT", "BARSCOUNT", "VALUEWHEN"}:
            values = self.array(args[0])
            condition = np.isfinite(values) if name == "BARSCOUNT" else truth(values)
            indices = np.maximum.accumulate(np.where(condition, self.indices, -1))
            if name == "BARSLAST":
                return (self.indices - indices).astype(float)
            if name == "BARSLASTCOUNT":
                return np.where(
                    condition,
                    self.indices - np.maximum.accumulate(np.where(~condition, self.indices, -1)),
                    0,
                ).astype(float)
            if name == "VALUEWHEN":
                return np.where(indices >= 0, self.array(args[1])[np.maximum(0, indices)], np.nan)
            first = np.minimum.accumulate(np.where(condition, self.indices, self.size))
            return np.where(
                first < self.size,
                self.indices - first + (1 if name == "BARSCOUNT" else 0),
                0 if name == "BARSCOUNT" else np.nan,
            ).astype(float)
        if name == "FILTER":
            values, periods = truth(self.array(args[0])), self.periods(args[1])
            result = np.zeros(self.size)
            until = -1
            for i in np.flatnonzero(values):
                if i > until:
                    result[i] = 1
                    until = i + periods[i]
            return result
        if name == "LAST":
            a, b = self.periods(args[1]), self.periods(args[2])
            values = truth(self.array(args[0]))
            if np.any((a != 0) & (a < b)):
                raise ValueError("LAST 的 A 必须大于等于 B")
            starts = np.maximum(0, self.indices - a)
            starts[a == 0] = 0
            ends = np.maximum(0, self.indices - b + 1)
            prefix = np.r_[0, np.cumsum(values)]
            return (
                (ends > starts)
                & (self.indices >= b)
                & ((prefix[ends] - prefix[starts]) == (ends - starts))
            ).astype(float)
        if name in {"SLOPE", "FORCAST"}:
            period = self.periods(args[1], 2)
            a = self.array(args[0])
            mean = self.window(a, period, "MA")
            starts = np.maximum(0, self.indices - period + 1)
            length = self.indices - starts + 1
            weighted = self.window(a * self.indices, period, "SUM")
            total = self.window(a, period, "SUM")
            xmean = (starts + self.indices) / 2
            variance = length * (length * length - 1) / 12
            slope = np.divide(
                weighted - total * xmean,
                variance,
                out=np.full(self.size, np.nan),
                where=variance > 0,
            )
            complete = self.window(np.isfinite(a).astype(float), period, "SUM") == length
            result = slope if name == "SLOPE" else mean + slope * ((length - 1) / 2)
            return np.where(complete, result, np.nan)
        if name == "RGB":
            values = [self.array(value) for value in args]
            if any(np.any(~np.isfinite(value) | (value < 0) | (value > 255)) for value in values):
                raise ValueError("RGB 分量必须在 0～255 内")
            return values[0] + values[1] * 256 + values[2] * 65536
        if name == "STRCAT":
            left, right = self.array(args[0], object), self.array(args[1], object)
            if any(len(str(a)) + len(str(b)) > 250 for a, b in zip(left, right, strict=True)):
                raise ValueError("组合文字不能超过 250 字符")
            return np.array(
                [str(a) + str(b) for a, b in zip(left, right, strict=True)],
                dtype=object,
            )
        if name in {"CON2STR", "VAR2STR"}:
            digits = int(self.scalar(args[1]))
            if not 0 <= digits <= 6:
                raise ValueError("文本小数位必须在 0～6 内")
            return np.array(
                [
                    f"{value:.{digits}f}" if np.isfinite(value) else "—"
                    for value in self.array(args[0])
                ],
                dtype=object,
            )
        a = self.array(args[0])
        unary = {
            "ABS": np.abs,
            "REVERSE": np.negative,
            "SGN": np.sign,
            "SIGN": np.sign,
            "SQRT": np.sqrt,
            "EXP": np.exp,
            "LN": np.log,
            "LOG": np.log10,
            "LOG10": np.log10,
            "INT": np.trunc,
            "INTPART": np.trunc,
            "FLOOR": np.floor,
            "CEILING": np.ceil,
        }
        if name in unary:
            return unary[name](a)
        if name in {"MAX", "MIN"}:
            return (np.maximum if name == "MAX" else np.minimum)(a, self.array(args[1]))
        if name in {"POW", "POWER"}:
            exponent = self.array(args[1])
            if np.any(np.abs(exponent) > 16):
                raise ValueError("幂指数绝对值不能超过 16")
            return np.power(a, exponent)
        if name == "MOD":
            return np.remainder(a, np.where(self.array(args[1]) == 0, np.nan, self.array(args[1])))
        if name == "ROUND":
            digits = int(self.scalar(args[1])) if len(args) > 1 else 0
            if not 0 <= digits <= 6:
                raise ValueError("小数位必须在 0～6 内")
            factor = 10**digits
            return np.sign(a) * np.floor(np.abs(a) * factor + 0.5) / factor
        if name in {"BETWEEN", "RANGE"}:
            low, high = np.minimum(args[1], args[2]), np.maximum(args[1], args[2])
            return (
                (a >= low) & (a <= high) if name == "BETWEEN" else (a > low) & (a < high)
            ).astype(float)
        raise ValueError(f"不支持的函数 {name}")

    def drawing(self, statement):
        name = statement.drawing
        args = [self.expression(arg) for arg in statement.expression.args]
        style = statement.style
        points = []
        kind = {
            "STICKLINE": "stick",
            "DRAWTEXT": "text",
            "DRAWNUMBER": "text",
            "DRAWICON": "icon",
            "DRAWBAND": "band",
            "DRAWKLINE": "candle",
            "DRAWTEXT_FIX": "fixed_text",
            "DRAWNUMBER_FIX": "fixed_text",
        }[name]
        result = {
            "kind": kind,
            "color": style["color"],
            "line_width": style["line_width"],
            "above": style.get("above", False),
            "points": points,
        }
        if name == "STICKLINE":
            width, empty = self.scalar(args[3]), self.scalar(args[4])
            if not 0 <= width <= 20 or empty not in {-1, 0, 1}:
                raise ValueError("STICKLINE 支持宽度 0～20、EMPTY 为 -1/0/1；特殊双轴柱尚不支持")
            result.update(width=width, empty=empty)
        if kind == "fixed_text":
            x, y, align = self.scalar(args[1]), self.scalar(args[2]), self.scalar(args[3])
            if not 0 <= x <= 1 or not 0 <= y <= 1 or align not in {0, 1}:
                raise ValueError("定点文字坐标应在 0～1 内，对齐为 0 或 1")
            result.update(x=x, y=y, align=align)
        condition = (
            np.ones(self.size, dtype=bool)
            if name in {"DRAWBAND", "DRAWKLINE"}
            else truth(self.array(args[0]))
        )
        selected = np.flatnonzero(condition)
        if kind == "fixed_text":
            selected = selected[-1:]
        arrays = [
            self.array(
                value,
                object
                if isinstance(value, str) or np.asarray(value).dtype.kind in {"O", "U"}
                else float,
            )
            for value in args
        ]
        previous_index = -2
        for i in selected:
            point = {"time": self.times[i]}
            if name == "DRAWBAND":
                a, b = arrays[0][i], arrays[2][i]
                if not np.isfinite(a) or not np.isfinite(b):
                    continue
                color = int(arrays[1 if a >= b else 3][i])
                point.update(
                    value=float(a),
                    end_value=float(b),
                    break_before=bool(i != previous_index + 1),
                    color=f"#{color & 255:02X}{(color >> 8) & 255:02X}{(color >> 16) & 255:02X}",
                )
            elif name == "DRAWKLINE":
                if not all(np.isfinite(array[i]) for array in arrays):
                    continue
                point.update(
                    value=float(arrays[0][i]),
                    end_value=float(arrays[2][i]),
                    open=float(arrays[1][i]),
                    close=float(arrays[3][i]),
                )
                if style["color_explicit"]:
                    point["color"] = style["color"]
            else:
                value = 0.0 if kind == "fixed_text" else float(arrays[1][i])
                if not np.isfinite(value):
                    continue
                point["value"] = value
                if name == "STICKLINE":
                    if not np.isfinite(arrays[2][i]):
                        continue
                    point["end_value"] = float(arrays[2][i])
                elif name == "DRAWICON":
                    icon = arrays[2][i]
                    if icon not in {1, 2}:
                        raise ValueError("DRAWICON 当前支持常用图标 1（向上箭头）、2（向下箭头）")
                    point["icon"] = int(icon)
                    if not style["color_explicit"]:
                        point["color"] = "#C74040" if icon == 1 else "#2D9B65"
                else:
                    text = arrays[4 if kind == "fixed_text" else 2][i]
                    if name in {"DRAWNUMBER", "DRAWNUMBER_FIX"}:
                        if not np.isfinite(float(text)):
                            continue
                        text = f"{float(text):.{self.precision}f}"
                    if len(str(text)) > 250:
                        raise ValueError("单条绘图文字不能超过 250 字符")
                    point["text"] = str(text)
            points.append(point)
            previous_index = i
        return result


def evaluate(program: Program, frame: pl.DataFrame, timeframe, parameters, precision):
    worker = Evaluator(frame, timeframe, parameters, precision)
    outputs = []
    drawings = []
    with np.errstate(all="ignore"):
        for statement in program.statements:
            try:
                if statement.drawing:
                    drawing = worker.drawing(statement)
                    if (
                        sum(len(shape["points"]) for shape in drawings) + len(drawing["points"])
                        > 60_000
                    ):
                        raise ValueError("绘图点超过 60000 个，请减少绘图语句或数据范围")
                    drawings.append(drawing)
                    continue
                raw = worker.expression(statement.expression)
                values = worker.array(
                    raw, object if np.asarray(raw).dtype.kind in {"O", "U"} else float
                )
                worker.env[statement.name] = values
                if statement.output:
                    item = {
                        "key": statement.name,
                        "label": statement.label,
                        **{
                            key: value
                            for key, value in statement.style.items()
                            if key not in {"above", "color_explicit"}
                        },
                        "values": [
                            float(value) if math.isfinite(value) else None for value in values
                        ],
                    }
                    if statement.style["color_mode"] == "candle":
                        item["colors"] = worker.candle_colors
                    outputs.append(item)
            except (ValueError, TypeError, OverflowError, FloatingPointError) as exc:
                raise error(statement.line, str(exc)) from exc
    return outputs, drawings
