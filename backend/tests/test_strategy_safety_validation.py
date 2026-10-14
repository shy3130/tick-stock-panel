"""策略安全校验回归 — GH #475 AST 黑名单绕过。

覆盖已报告的逃逸载荷 (别名赋值 / 字符串拼接下标 / dunder 字符串参数) 与
同类未报告逃逸 (模块命名空间句柄 pl.os / np.lib、文件读写原语、通配导入),
以及合法策略代码不被误伤。
"""
from __future__ import annotations

import textwrap

import pytest

from app.strategy.ai_generator import AIStrategyGenerator


def _validate(code: str, **kwargs) -> None:
    AIStrategyGenerator._validate_safety(textwrap.dedent(code), **kwargs)


# ================================================================
# 已报告逃逸 (GH #475 PoC 与最小变体): 必须全部拦截
# ================================================================
REPORTED_ESCAPES = {
    "issue_poc_alias_and_concat": '''
        import datetime

        g = getattr
        s = "__imp" + "ort__"
        imp = __builtins__[s]
        os_mod = imp("os")
        os_mod.popen("(id; uname -srm) > /tmp/TICK_RCE_PROOF.txt 2>&1").read()

        def filter(df):
            return df

        META = {"id": "custom_poc_rce", "name": "poc", "description": "poc",
                "scoring": {"close": 1.0}}
    ''',
    "alias_getattr_assignment": "g = getattr",
    "alias_globals_then_call": "gg = globals\nv = gg()",
    "concat_subscript_import": 'imp = __builtins__["__imp" + "ort__"]',
    "dunder_subscript_constant": 'x = obj["__class__"]',
    "dunder_subscript_concat": 'x = obj["__cla" + "ss__"]',
    "dunder_name_reference": "x = __builtins__",
    "dunder_attribute_traversal": "x = (1).__class__",
    "subclasses_traversal": "x = ().__class__.__base__.__subclasses__()",
    "alias_vars": "v = vars()",
    "alias_type": "t = type(x)",
}

# ================================================================
# 同类未报告逃逸: 模块命名空间句柄与文件原语
# ================================================================
NAMESPACE_ESCAPES = {
    "polars_os_handle": "import polars as pl\npl.os.system('id')",
    "polars_contextlib": "import polars as pl\nx = pl.contextlib",
    "numpy_lib_os": "import numpy as np\nnp.lib._function_base_impl.os.system('id')",
    "numpy_ctypeslib": "import numpy as np\nnp.ctypeslib.ctypes.CDLL('libc.so.6')",
    "numpy_random_sys": "import numpy as np\nnp.random._common.sys.modules",
    "from_import_namespace_handle": "from polars import os as plos",
    "bare_import_namespace_attr": "import numpy\nnumpy.random.sys",
    "direct_system_call": "x = thing.system('id')",
    "direct_popen_call": "x = thing.popen('id')",
    "write_csv_primitive": "df.write_csv('/tmp/x.csv')",
    "read_parquet_primitive": "df = pl.read_parquet('/etc/passwd')",
    "savetxt_primitive": "np.savetxt('/tmp/x', arr)",
    "module_type_handle": "m = types.ModuleType('m')",
    "lambda_type_handle": "f = types.LambdaType(x)",
}


@pytest.mark.parametrize("code", list(REPORTED_ESCAPES.values()),
                         ids=list(REPORTED_ESCAPES))
def test_reported_escapes_are_blocked(code):
    with pytest.raises(ValueError):
        _validate(code)


@pytest.mark.parametrize("code", list(NAMESPACE_ESCAPES.values()),
                         ids=list(NAMESPACE_ESCAPES))
def test_namespace_and_file_primitive_escapes_are_blocked(code):
    with pytest.raises(ValueError):
        _validate(code)


def test_star_import_is_blocked():
    with pytest.raises(ValueError, match="通配导入"):
        _validate("from app.backtest.matrix import *")


def test_namespace_package_import_is_blocked():
    with pytest.raises(ValueError, match="白名单"):
        _validate("import app.strategy")


# ================================================================
# 合法策略不被误伤
# ================================================================
LEGITIMATE_SNIPPETS = {
    "polars_matrix_strategy": '''
        import datetime

        import numpy as np
        import polars as pl
        from app.backtest.matrix import MarketDataMatrix, SignalMatrix, valid_shift

        def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
            entry = (pl.col("close") > pl.col("open")) & (pl.col("volume") > 0)
            return entry

        META = {"id": "custom_x", "name": "x", "description": "x",
                "scoring": {"close": 1.0}}
    ''',
    "market_open_column": "entry = market.open > market.close",
    "market_volume_column": "v = market.volume.rolling_mean(5)",
    "dynamic_index_by_loop_var": "for i in range(3):\n    v = arr[i]",
    "string_key_subscript": "v = row[str('close')]",
    "tuple_type_annotation": "def f() -> tuple[str, float]:\n    return ('a', 1.0)",
    "main_guard": 'if __name__ == "__main__":\n    pass',
    "legit_get_method": "v = row.get('k', 0)",
    "legit_isinstance": "if isinstance(x, dict):\n    pass",
    "legit_str_format": 'txt = "{}".format(1)',
    "legit_read_none": "x = None",
    "dunder_mention_in_docstring": '''
        """策略说明: 不使用 __class__ 等 dunder 遍历。"""

        def filter(df):
            return df
    ''',
    "numpy_public_api": "import numpy as np\nx = np.arange(10)\ny = np.float32(1.0)",
}


@pytest.mark.parametrize("code", list(LEGITIMATE_SNIPPETS.values()),
                         ids=list(LEGITIMATE_SNIPPETS))
def test_legitimate_code_still_passes(code):
    _validate(code)


def test_existing_strategies_in_repo_all_pass():
    """仓库内全部现有策略文件必须仍通过加固后的校验 (兼容性回归)。"""
    from pathlib import Path

    backend_dir = Path(__file__).resolve().parents[1]
    project_root = backend_dir.parent
    roots = [
        project_root / "data" / "strategies",
        backend_dir / "app" / "strategy" / "research",
    ]
    files: list[Path] = []
    for root in roots:
        if root.exists():
            files.extend(sorted(root.rglob("*.py")))
    files = [f for f in files if f.name != "__init__.py"]
    if not files:
        pytest.skip("no strategy files present")

    for path in files:
        AIStrategyGenerator._validate_safety(path.read_text(encoding="utf-8"))


# ================================================================
# 纵深防御: 引擎加载层必须同样拦截 (文件被直接投放磁盘的场景)
# ================================================================
_POC_FILE = '''
import datetime

g = getattr
s = "__imp" + "ort__"
imp = __builtins__[s]
os_mod = imp("os")
os_mod.popen("id > /tmp/TICK_RCE_PROOF.txt").read()

def filter(df):
    return df

META = {"id": "custom_poc_rce", "name": "poc", "description": "poc",
        "scoring": {"close": 1.0}}
'''


def test_engine_load_file_rejects_bypass_payload(tmp_path):
    """绕过保存接口直接投放磁盘的策略文件, 加载时必须被拦截。"""
    from app.strategy.engine import StrategyEngine

    path = tmp_path / "custom_poc_rce.py"
    path.write_text(textwrap.dedent(_POC_FILE), encoding="utf-8")

    with pytest.raises(ValueError):
        StrategyEngine._load_file(path)

    assert not (tmp_path / "TICK_RCE_PROOF.txt").exists()


def test_engine_load_file_rejects_dependency_using_vars(tmp_path):
    """依赖文件不再放行 vars(): 拿到调用栈即拿到任意代码执行原料。"""
    from app.strategy.engine import StrategyEngine

    (tmp_path / "_helpers.py").write_text(
        "def f(obj):\n    return vars(obj)\n", encoding="utf-8",
    )
    main = tmp_path / "custom_dep.py"
    main.write_text(
        '"""依赖文件策略"""\n'
        "import polars as pl\n"
        "import _helpers\n\n"
        "def filter(df: pl.DataFrame, params: dict) -> pl.Expr:\n"
        "    return pl.lit(True)\n\n"
        'META = {"id": "custom_dep", "name": "dep", "description": "dep",\n'
        '        "scoring": {}}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        StrategyEngine._load_file(main)


def test_engine_load_file_fails_closed_on_unreadable_dependency(tmp_path):
    """依赖读取异常时 fail-closed, 不静默跳过校验直接 exec_module。

    跨平台确定性: 不依赖 chmod 语义 (Windows 上不可靠), 改用目录占位
    触发同类 OSError (IsADirectoryError 是 OSError 子类)。
    """
    from app.strategy.engine import StrategyEngine

    ghost = tmp_path / "_ghost.py"
    ghost.mkdir()  # 目录占同名路径: read_text 抛 IsADirectoryError
    (tmp_path / "custom_ok.py").write_text(
        '"""正常策略"""\n'
        "import polars as pl\n\n"
        "def filter(df: pl.DataFrame, params: dict) -> pl.Expr:\n"
        "    return pl.lit(True)\n\n"
        'META = {"id": "custom_ok", "name": "ok", "description": "ok",\n'
        '        "scoring": {}}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="安全校验失败"):
        StrategyEngine._load_file(tmp_path / "custom_ok.py")
