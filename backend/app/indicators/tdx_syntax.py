"""Bounded TDX source parser. Python AST is inspected, never executed."""

from __future__ import annotations

import ast
import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache

FIELDS = {
    name: name.lower()
    for name in (
        "OPEN",
        "HIGH",
        "LOW",
        "CLOSE",
        "VOLUME",
        "AMOUNT",
        "TURNOVER_RATE",
        "PREV_CLOSE",
        "RAW_CLOSE",
    )
}
FIELDS.update(O="open", H="high", L="low", C="close", V="volume", VOL="volume", AMO="amount")
CONSTANTS = {
    "DRAWNULL",
    "TRUE",
    "FALSE",
    "BARSCOUNT",
    "CURRBARSCOUNT",
    "TOTALBARSCOUNT",
    "ISLASTBAR",
    "DATE",
    "TIME",
    "TIME2",
    "YEAR",
    "MONTH",
    "DAY",
    "WEEKDAY",
    "PERIOD",
}
SIGNATURES = {
    "MA": (2, 2),
    "EMA": (2, 2),
    "EXPMA": (2, 2),
    "EXPMEMA": (2, 2),
    "SMA": (3, 3),
    "MEMA": (2, 2),
    "DMA": (2, 2),
    "WMA": (2, 2),
    "SUM": (2, 2),
    "HHV": (2, 2),
    "LLV": (2, 2),
    "HHVBARS": (2, 2),
    "LLVBARS": (2, 2),
    "STD": (2, 2),
    "STDP": (2, 2),
    "VAR": (2, 2),
    "VARP": (2, 2),
    "AVEDEV": (2, 2),
    "DEVSQ": (2, 2),
    "REF": (2, 2),
    "COUNT": (2, 2),
    "EVERY": (2, 2),
    "EXIST": (2, 2),
    "LAST": (3, 3),
    "CROSS": (2, 2),
    "LONGCROSS": (3, 3),
    "BARSLAST": (1, 1),
    "BARSSINCE": (1, 1),
    "BARSLASTCOUNT": (1, 1),
    "BARSCOUNT": (1, 1),
    "FILTER": (2, 2),
    "VALUEWHEN": (2, 2),
    "ABS": (1, 1),
    "MAX": (2, 2),
    "MIN": (2, 2),
    "IF": (3, 3),
    "IFF": (3, 3),
    "IFN": (3, 3),
    "BETWEEN": (3, 3),
    "RANGE": (3, 3),
    "REVERSE": (1, 1),
    "SGN": (1, 1),
    "SIGN": (1, 1),
    "SQRT": (1, 1),
    "POW": (2, 2),
    "POWER": (2, 2),
    "EXP": (1, 1),
    "LN": (1, 1),
    "LOG": (1, 1),
    "LOG10": (1, 1),
    "INTPART": (1, 1),
    "INT": (1, 1),
    "FLOOR": (1, 1),
    "CEILING": (1, 1),
    "ROUND": (1, 2),
    "MOD": (2, 2),
    "SLOPE": (2, 2),
    "FORCAST": (2, 2),
    "RGB": (3, 3),
    "STRCAT": (2, 2),
    "CON2STR": (2, 2),
    "VAR2STR": (2, 2),
}
DRAWINGS = {
    "STICKLINE": (5, 5),
    "DRAWTEXT": (3, 3),
    "DRAWICON": (3, 3),
    "DRAWNUMBER": (3, 3),
    "DRAWTEXT_FIX": (5, 5),
    "DRAWNUMBER_FIX": (5, 5),
    "DRAWBAND": (4, 4),
    "DRAWKLINE": (4, 4),
}
FUTURE = {
    "REFX",
    "REFXV",
    "BACKSET",
    "ZIG",
    "ZIGA",
    "PEAK",
    "PEAKBARS",
    "TROUGH",
    "TROUGHBARS",
    "BARSNEXT",
    "DRAWLINE",
}
DATA_FUNCTIONS = {
    "FINANCE",
    "DYNAINFO",
    "CAPITAL",
    "TOTALCAPITAL",
    "WINNER",
    "COST",
    "INDEXC",
    "INDEXO",
    "INDEXH",
    "INDEXL",
    "CALCSTOCKINDEX",
    "INSUM",
    "INSORT",
}
COLORS = {
    "RED": "#FF0000",
    "GREEN": "#00FF00",
    "BLUE": "#0000FF",
    "YELLOW": "#FFFF00",
    "WHITE": "#FFFFFF",
    "BLACK": "#000000",
    "CYAN": "#00FFFF",
    "MAGENTA": "#FF00FF",
    "GRAY": "#808080",
    "BROWN": "#A52A2A",
    "LIGRAY": "#D3D3D3",
    "LIRED": "#FF8080",
    "LIGREEN": "#80FF80",
    "LIBLUE": "#8080FF",
    "LIMAGENTA": "#FF80FF",
}
TOKEN = re.compile(
    r"\s*(?:(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)|(?P<string>'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")|(?P<name>[^\W\d]\w*)|(?P<op>>=|<=|<>|!=|==|&&|\|\||[+*/%><=!,()|\-]))",
    re.UNICODE,
)


def error(line, message):
    return ValueError(f"第{line}行：{message}")


@dataclass(frozen=True)
class Statement:
    name: str
    label: str
    expression: ast.AST
    output: bool
    style: dict
    line: int
    drawing: str | None = None


@dataclass(frozen=True)
class Program:
    statements: tuple[Statement, ...]
    dependencies: frozenset[str]
    warmup: int


def statements(source):
    cleaned = list(source)
    quote = None
    comment = False
    depth = 0
    start = 0
    i = 0
    while i < len(source):
        char = source[i]
        if comment:
            cleaned[i] = "\n" if char == "\n" else " "
            if char == "}":
                comment = False
        elif quote:
            if char == quote:
                if i + 1 < len(source) and source[i + 1] == quote:
                    i += 1
                else:
                    quote = None
        elif char in "'\"":
            quote = char
        elif char == "{":
            comment = True
            cleaned[i] = " "
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == ";" and depth == 0:
            value = "".join(cleaned[start:i])
            if value.strip():
                leading = value[: len(value) - len(value.lstrip())]
                yield value.strip(), source.count("\n", 0, start) + leading.count("\n") + 1
            start = i + 1
        if depth < 0:
            raise error(source.count("\n", 0, i) + 1, "括号不匹配")
        i += 1
    if quote or comment or depth:
        raise error(source.count("\n", 0, start) + 1, "引号、注释或括号未闭合")
    value = "".join(cleaned[start:])
    if value.strip():
        leading = value[: len(value) - len(value.lstrip())]
        yield value.strip(), source.count("\n", 0, start) + leading.count("\n") + 1


def split_parts(text):
    parts = []
    start = depth = 0
    quote = None
    i = 0
    while i < len(text):
        char = text[i]
        if quote:
            if char == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    i += 1
                else:
                    quote = None
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
        i += 1
    return [*parts, text[start:].strip()]


def parse_expression(text, line):
    tokens = []
    offset = 0
    while offset < len(text):
        match = TOKEN.match(text, offset)
        if not match:
            if not text[offset:].strip():
                break
            raise error(
                line, f"不支持的字符 {text[offset:].strip()[0]!r}；不支持跨证券、跨周期引用"
            )
        offset = match.end()
        if match.group("name"):
            value = match.group("name").upper()
            tokens.append({"AND": "and", "OR": "or", "NOT": "not"}.get(value, value))
        elif match.group("string"):
            value = match.group("string")
            tokens.append(
                json.dumps(value[1:-1].replace(value[0] * 2, value[0]), ensure_ascii=False)
            )
        elif match.group("number"):
            tokens.append(match.group("number"))
        else:
            tokens.append(
                {"=": "==", "<>": "!=", "&&": "and", "||": "or", "|": "or", "!": "not"}.get(
                    match.group("op"), match.group("op")
                )
            )
    if len(tokens) > 600:
        raise error(line, "单条表达式超过 600 个词元")
    try:
        return ast.parse(" ".join(tokens).strip(), mode="eval").body
    except (SyntaxError, RecursionError) as exc:
        raise error(line, "表达式语法错误，请检查运算符和括号") from exc


def style_options(parts, line):
    style = {
        "color": "#3B82F6",
        "style": "line",
        "line_width": 1,
        "line_style": "solid",
        "nodraw": False,
        "color_mode": "fixed",
        "color_explicit": False,
    }
    for text in parts:
        name = text.strip().upper()
        if name.startswith("COLOR"):
            style["color_explicit"] = True
            color = name[5:]
            if color == "STICK":
                style.update(style="histogram", color_mode="sign", color="#C74040")
            elif color in COLORS:
                style["color"] = COLORS[color]
            elif re.fullmatch("[0-9A-F]{6}", color):
                style["color"] = "#" + color[4:6] + color[2:4] + color[:2]
            else:
                raise error(line, f"不支持的颜色 {text}")
        elif re.fullmatch(r"LINETHICK[1-9]", name):
            style["line_width"] = int(name[-1])
        elif name == "NODRAW":
            style["nodraw"] = True
        elif name in {"DOTLINE", "POINTDOT", "CIRCLEDOT", "CROSSDOT"}:
            style["line_style"] = {
                "DOTLINE": "dotted",
                "POINTDOT": "points",
                "CIRCLEDOT": "circles",
                "CROSSDOT": "crosses",
            }[name]
        elif name == "VOLSTICK":
            style.update(style="volume", color_mode="candle", color="#C74040")
        elif name == "STICK":
            style["style"] = "histogram"
        elif name == "LINESTICK":
            style["style"] = "line_stick"
        elif name == "DRAWABOVE":
            style["above"] = True
        else:
            raise error(line, f"不支持的绘图属性 {text}")
    return style


def validate_node(node, available, line, depth=0):
    if depth > 24:
        raise error(line, "表达式嵌套超过 24 层")
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float, str)):
            raise error(line, "不支持该常量")
        if isinstance(node.value, str) and len(node.value) > 250:
            raise error(line, "单条文字不能超过 250 字符")
        if isinstance(node.value, (int, float)) and (
            abs(node.value) > 1e100 or not math.isfinite(node.value)
        ):
            raise error(line, "数值常量超出范围")
        return
    if isinstance(node, ast.Name):
        if node.id not in available:
            if node.id in DATA_FUNCTIONS:
                raise error(line, f"数据字段 {node.id} 尚未接入")
            raise error(line, f"未知变量 {node.id}，请先赋值或在参数中定义")
        return
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise error(line, "不支持该函数调用")
        name = node.func.id
        if name in FUTURE:
            raise error(line, f"不支持未来函数 {name}")
        if name in DATA_FUNCTIONS:
            raise error(line, f"数据函数 {name} 尚未接入")
        signature = {**SIGNATURES, **DRAWINGS}.get(name)
        if not signature:
            raise error(line, f"不支持的函数 {name}")
        if node.keywords or not signature[0] <= len(node.args) <= signature[1]:
            raise error(line, f"{name} 参数数量错误")
        for arg in node.args:
            validate_node(arg, available, line, depth + 1)
        return
    allowed = (ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare)
    operators = (
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.Mod,
        ast.USub,
        ast.UAdd,
        ast.Not,
        ast.And,
        ast.Or,
        ast.Eq,
        ast.NotEq,
        ast.Gt,
        ast.GtE,
        ast.Lt,
        ast.LtE,
    )
    if not isinstance(node, allowed):
        raise error(line, "不支持该表达式结构")
    for child in ast.iter_child_nodes(node):
        if isinstance(child, operators):
            continue
        validate_node(child, available, line, depth + 1)


def constant(node, values):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Name):
        return values.get(node.id)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = constant(node.operand, values)
        return None if value is None else -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp):
        left, right = constant(node.left, values), constant(node.right, values)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div) and right:
            return left / right
    return None


def expression_type(node, types, line):
    if isinstance(node, ast.Constant):
        return "text" if isinstance(node.value, str) else "number"
    if isinstance(node, ast.Name):
        return types.get(node.id, "number")
    if isinstance(node, ast.Call):
        name = node.func.id
        kinds = [expression_type(arg, types, line) for arg in node.args]
        if name == "STRCAT":
            if any(kind != "text" for kind in kinds):
                raise error(line, "STRCAT 需要文字，数值请先用 CON2STR 转换")
            return "text"
        if name in {"IF", "IFF", "IFN"} and kinds[0] == "number" and kinds[1] == kinds[2]:
            return kinds[1]
        text_index = {"DRAWTEXT": 2, "DRAWTEXT_FIX": 4}.get(name)
        if any(kind == "text" and index != text_index for index, kind in enumerate(kinds)):
            raise error(line, f"{name} 的数值参数不能使用文字")
        return "text" if name in {"CON2STR", "VAR2STR"} else "number"
    if any(expression_type(child, types, line) == "text" for child in ast.iter_child_nodes(node)):
        raise error(line, "文字不能参与数值运算")
    return "number"


def expression_warmup(node, warmups, horizons):
    if isinstance(node, ast.Name):
        return warmups.get(node.id, 0)
    return min(
        4096,
        max(
            (expression_warmup(child, warmups, horizons) for child in ast.iter_child_nodes(node)),
            default=0,
        )
        + horizons.get(id(node), 0),
    )


@lru_cache(maxsize=128)
def compile_source(source: str, parameters: tuple[tuple[str, float], ...] = ()) -> Program:
    if not source.strip():
        raise error(1, "请输入指标源码")
    if len(source) > 40000:
        raise error(1, "源码超过 40000 字符")
    available = set(FIELDS) | CONSTANTS | {name.upper() for name, _ in parameters}
    roots = {name: {field} for name, field in FIELDS.items()}
    warmups = {name: 0 for name in available}
    constants = {name.upper(): value for name, value in parameters}
    types = {}
    result = []
    dependencies = set()
    output_count = 0
    output_names = set()
    total_nodes = 0
    for index, (text, line) in enumerate(statements(source)):
        if index >= 80:
            raise error(line, "最多支持 80 条语句")
        assignment = re.match(r"^([^\W\d]\w*)\s*(:=|:)\s*(.+)$", text, re.S)
        label = assignment[1] if assignment else f"输出{index + 1}"
        name = label.upper() if assignment else f"_OUTPUT_{index}"
        output = not assignment or assignment[2] == ":"
        parts = split_parts(assignment[3] if assignment else text)
        expression = parse_expression(parts[0], line)
        validate_node(expression, available, line)
        total_nodes += sum(1 for _ in ast.walk(expression))
        if total_nodes > 4000:
            raise error(line, "源码表达式过于复杂")
        drawing = (
            expression.func.id
            if isinstance(expression, ast.Call) and expression.func.id in DRAWINGS
            else None
        )
        if drawing and assignment:
            raise error(line, "绘图语句不能作为数值变量赋值")
        if any(
            isinstance(child, ast.Call) and child.func.id in DRAWINGS
            for child in ast.walk(expression)
            if child is not expression
        ):
            raise error(line, "绘图函数必须单独成句")
        style = style_options(parts[1:], line)
        if drawing and style["nodraw"]:
            raise error(line, "绘图语句的 NODRAW 文字栏尚不支持；数值输出可使用 NODRAW")
        kind = expression_type(expression, types, line)
        if output and not drawing and kind == "text":
            raise error(line, "文字请使用 DRAWTEXT 绘制，不能作为数值输出")
        types[name] = kind
        fields = set().union(
            *(
                roots.get(child.id, set())
                for child in ast.walk(expression)
                if isinstance(child, ast.Name)
            )
        )
        dependencies.update(fields)
        # Bounded warmup also covers recursive/event functions and variable windows.
        calls = [child for child in ast.walk(expression) if isinstance(child, ast.Call)]
        horizons = {}
        for call in calls:
            function = call.func.id
            period_index = (
                1
                if function
                in {
                    "MA",
                    "EMA",
                    "EXPMA",
                    "EXPMEMA",
                    "SMA",
                    "MEMA",
                    "WMA",
                    "HHV",
                    "LLV",
                    "HHVBARS",
                    "LLVBARS",
                    "SUM",
                    "COUNT",
                    "EVERY",
                    "EXIST",
                    "STD",
                    "STDP",
                    "VAR",
                    "VARP",
                    "REF",
                    "AVEDEV",
                    "DEVSQ",
                    "SLOPE",
                    "FORCAST",
                    "FILTER",
                }
                else None
            )
            period_value = (
                constant(call.args[period_index], constants) if period_index is not None else None
            )
            if period_value is not None and not math.isfinite(period_value):
                raise error(line, "周期必须是有限整数")
            if period_value is not None:
                if period_value < 0:
                    raise error(line, "不支持未来引用或负周期")
                if period_value != int(period_value) or period_value > 4096:
                    raise error(line, "周期必须是 0～4096 的整数")
                if period_value == 0 and function in {
                    "EMA",
                    "EXPMA",
                    "EXPMEMA",
                    "SMA",
                    "MEMA",
                    "WMA",
                    "SLOPE",
                    "FORCAST",
                }:
                    raise error(line, f"{function} 周期必须大于 0")
            if function == "SMA":
                weight = constant(call.args[2], constants)
                if weight is not None and (
                    not math.isfinite(weight)
                    or weight <= 0
                    or (period_value is not None and weight > period_value)
                ):
                    raise error(line, "SMA 权重必须满足 0 < M <= N")
            constraints = {
                "DRAWICON": [(2, lambda v: v in {1, 2}, "DRAWICON 当前支持图标 1、2")],
                "STICKLINE": [
                    (3, lambda v: 0 <= v <= 20, "柱宽必须在 0～20 内"),
                    (4, lambda v: v in {-1, 0, 1}, "STICKLINE EMPTY 当前支持 -1/0/1"),
                ],
                "DRAWTEXT_FIX": [
                    (1, lambda v: 0 <= v <= 1, "固定文字坐标必须在 0～1 内"),
                    (2, lambda v: 0 <= v <= 1, "固定文字坐标必须在 0～1 内"),
                    (3, lambda v: v in {0, 1}, "对齐必须为 0 或 1"),
                ],
            }
            if function == "DRAWNUMBER_FIX":
                constraints[function] = constraints["DRAWTEXT_FIX"]
            for arg_index, valid, message in constraints.get(function, []):
                value = constant(call.args[arg_index], constants)
                if value is not None and not valid(value):
                    raise error(line, message)
            if function in {
                "BARSLAST",
                "BARSSINCE",
                "BARSLASTCOUNT",
                "VALUEWHEN",
                "FILTER",
                "BARSCOUNT",
                "LONGCROSS",
                "LAST",
                "DMA",
            }:
                horizons[id(call)] = 4096
            elif function in {
                "MA",
                "EMA",
                "EXPMA",
                "EXPMEMA",
                "SMA",
                "MEMA",
                "WMA",
                "HHV",
                "LLV",
                "HHVBARS",
                "LLVBARS",
                "SUM",
                "COUNT",
                "EVERY",
                "EXIST",
                "STD",
                "STDP",
                "VAR",
                "VARP",
                "REF",
                "AVEDEV",
                "DEVSQ",
                "SLOPE",
                "FORCAST",
            }:
                value = call.args[1]
                period = constant(value, constants)
                period = 4096 if period is None else period
                horizons[id(call)] = (
                    4096
                    if period == 0
                    else min(
                        4096,
                        int(period)
                        * (20 if function in {"EMA", "EXPMA", "SMA", "MEMA", "EXPMEMA"} else 1),
                    )
                )
        warmups[name] = expression_warmup(expression, warmups, horizons)
        roots[name] = fields
        available.add(name)
        value = constant(expression, constants)
        if value is not None:
            constants[name] = value
        else:
            constants.pop(name, None)
        if output and not drawing:
            if name in output_names:
                raise error(line, f"输出变量 {label} 重复，请使用不同名称")
            output_names.add(name)
            output_count += 1
        if output_count > 32:
            raise error(line, "最多支持 32 条数值输出")
        result.append(Statement(name, label, expression, output, style, line, drawing))
    if not result or not any(statement.output for statement in result):
        raise error(1, "至少需要一条输出或绘图语句")
    return Program(tuple(result), frozenset(dependencies), max(warmups.values(), default=0))
