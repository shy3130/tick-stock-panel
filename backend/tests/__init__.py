"""测试包标记。

为什么必须有这个文件
--------------------
本目录下 12 个测试文件用 `from tests.xxx import ...` 互相引用, 但此前没有
`__init__.py`, 只能靠 Python 3 的命名空间包「碰巧」成立。一旦某个依赖也在
site-packages 里提供顶层 `tests` 包, 命名空间包就会输给普通包:
vectorbt 正好这么做, 于是装了 backtest extra 后
`python -m pytest tests/` 在收集阶段直接报
`ModuleNotFoundError: No module named 'tests.signal_seeds'` —— 全量测试根本跑不起来,
只有逐个文件点名才跑得动, 于是「全绿」成了假象。

显式声明为普通包后, `tests` 由本仓库定义, 与依赖的打包问题解耦。
"""
