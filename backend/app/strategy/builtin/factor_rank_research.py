"""兼容转发: 研究模板已迁移至 ``app.strategy.research.factor_rank_research``。

旧版用户策略文件 import 本路径时透明转发到新模块, 升级零改动。
"""
import sys

from app.strategy.research import factor_rank_research as _impl

sys.modules[__name__] = _impl
