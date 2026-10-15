"""策略种子 — 去内置化迁移补全: 原内置策略按 ID 种入用户自定义目录。

与 custom_signals.seed_if_empty 同族机制 (仅空判断 vs 按 ID 补缺的取舍
见各 docstring): 升级用户的 data/strategies/custom 往往已有自己的策略,
但丢失了原 25 个内置策略 —— 因此按文件名逐一补缺, 已存在(含用户已自行
迁移的同名策略)一律不覆盖; .seeded_v1 标记保证一次性, 删除后重启不复活。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SEEDS_DIR = Path(__file__).resolve().parent / "seeds"


def seed_missing_builtins(data_dir: Path) -> int:
    """把内置策略种子按文件名补入 data/strategies/custom; 返回种入数量。

    - 幂等: ``.seeded_v1`` 标记存在 (无论目录后来是否被清空) 不再种入。
    - 仅写缺失的同名文件, 已有一律不覆盖: 全新环境拿到全部 25 个;
      已自行迁移过的老环境零写入; 只迁移了一部分的按缺口补全。
    - 写入前逐一过 _validate_safety (种子随包分发, 纵深防御),
      校验失败跳过该文件不阻断启动。
    """
    custom_dir = data_dir / "strategies" / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)
    marker = custom_dir / ".seeded_v1"
    if marker.exists():
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
