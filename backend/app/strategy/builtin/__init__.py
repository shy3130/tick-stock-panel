"""向后兼容垫片 (去内置化迁移)。

内置策略目录已迁移: 研究模板移至 ``app/strategy/research``, 25 个公共策略
转为用户数据 (``data/strategies/custom/``)。旧版数据目录里的用户策略文件
仍可能 ``from app.strategy.builtin.factor_rank_research import ...`` ——
本包仅为该 import 保留转发, 不含任何策略定义与加载逻辑, 引擎也不会把
此目录当策略目录扫描。
"""
