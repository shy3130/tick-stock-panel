"""策略种子 — 去内置化迁移补全: 仅旧版本升级用户, 原内置策略按 ID 补回。

新版本没有内置策略 (产品设计如此, 新装环境策略库为空)。此迁移只服务
「从有内置策略的旧版本升级上来」的用户: 他们的策略池/监控引用了内置
策略 ID, 升级后丢失。判断依据是数据目录里的旧版使用痕迹 — 新装首次
启动既无日K数据也无自选/策略文件, 视为新用户直接跳过并落标记。
已存在(含用户已自行迁移的同名策略)一律不覆盖; 标记保证一次性,
删除后重启不复活。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SEEDS_DIR = Path(__file__).resolve().parent / "seeds"


def _has_upgrade_traces(data_dir: Path) -> bool:
    """数据目录是否有旧版本的使用痕迹 (新装首次启动为 False)。

    三个信号任一命中即视为升级用户:
    - strategies/{custom,ai,composite} 下已有任何策略文件 (用户自己的)
    - kline_daily 已有 parquet (拉过日K; 内置策略跑起来必须有数据)
    - watchlist.parquet 存在 (加过自选)
    """
    for sub in ("custom", "ai", "composite"):
        if any((data_dir / "strategies" / sub).glob("*.py")):
            return True
    if any((data_dir / "kline_daily").rglob("*.parquet")):
        return True
    return (data_dir / "user_data" / "watchlist.parquet").exists()


def seed_missing_builtins(data_dir: Path) -> int:
    """仅升级用户: 把内置策略种子按文件名补入 data/strategies/custom。

    - 幂等: ``.seeded_v1`` 标记存在 (无论目录后来是否被清空) 不再种入。
    - 新用户 (无旧版使用痕迹) 跳过并落标记 — 新版本没有内置策略,
      新装策略库保持为空; 之后再拉数据/用久了也不会回头种 (标记已落)。
    - 仅写缺失的同名文件, 已有一律不覆盖: 已自行迁移过的老环境零写入,
      只迁移了一部分的按缺口补全。
    - 写入前逐一过 _validate_safety (种子随包分发, 纵深防御),
      校验失败跳过该文件不阻断启动。
    """
    custom_dir = data_dir / "strategies" / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)
    marker = custom_dir / ".seeded_v1"
    if marker.exists():
        return 0

    if not _has_upgrade_traces(data_dir):
        marker.write_text("skipped: fresh install\n", encoding="utf-8")
        logger.info("strategy seeds: fresh install, skip seeding")
        return 0

    from app.strategy.ai_generator import AIStrategyGenerator

    written = 0
    for seed in sorted(SEEDS_DIR.glob("*.py")):
        # 跳过 _ 前缀 (包的 __init__.py 等): 引擎把 _*.py 当依赖文件, 不是策略
        if seed.name.startswith("_"):
            continue
        target = custom_dir / seed.name
        if target.exists():
            continue
        code = seed.read_text(encoding="utf-8")
        try:
            AIStrategyGenerator._validate_safety(code)
        except (ValueError, SyntaxError) as e:
            logger.warning("strategy seed %s skipped (validation): %s", seed.name, e)
            continue
        target.write_text(code, encoding="utf-8")
        written += 1
    marker.write_text(f"seeded: {written}\n", encoding="utf-8")
    if written:
        logger.info("strategy seeds: wrote %d builtin strategies to custom", written)
    return written
