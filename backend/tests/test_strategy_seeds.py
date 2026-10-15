"""策略种子 seed_missing_builtins — 去内置化补全回归。

原 25 个内置策略在去内置化后仅存在于 tests/fixtures 与部分老用户的
custom 目录, 升级用户丢失。此处回归种入语义:
  - 空目录一次写入全部 25 个, 标记防重;
  - 已有同名文件一律不覆盖 (用户自行迁移过的零写入);
  - 删除后重启不复活;
  - 种入的文件能被引擎真实加载。
"""
from __future__ import annotations

from app.strategy.strategy_seeds import SEEDS_DIR, seed_missing_builtins

_EXPECTED_COUNT = len([f for f in SEEDS_DIR.glob("*.py") if not f.name.startswith("_")])


def test_seed_writes_all_and_marks(tmp_path):
    n = seed_missing_builtins(tmp_path)
    assert n == _EXPECTED_COUNT == 25
    custom = tmp_path / "strategies" / "custom"
    assert len(list(custom.glob("*.py"))) == 25
    assert (custom / ".seeded_v1").exists()
    # 幂等: 标记存在, 再次启动零写入
    assert seed_missing_builtins(tmp_path) == 0


def test_seed_never_overwrites_existing(tmp_path):
    custom = tmp_path / "strategies" / "custom"
    custom.mkdir(parents=True)
    # 用户已自行迁移过 1 个 (内容改成自定义版本) + 1 个自己的策略
    (custom / "boll_breakout.py").write_text("# 用户自定义版本\n", encoding="utf-8")
    (custom / "my_own.py").write_text("# 用户自己的策略\n", encoding="utf-8")

    n = seed_missing_builtins(tmp_path)

    assert n == _EXPECTED_COUNT - 1
    assert (custom / "boll_breakout.py").read_text(encoding="utf-8") == "# 用户自定义版本\n"
    assert (custom / "my_own.py").read_text(encoding="utf-8") == "# 用户自己的策略\n"
    assert len(list(custom.glob("*.py"))) == _EXPECTED_COUNT + 1


def test_seed_no_resurrect_after_user_deletes(tmp_path):
    seed_missing_builtins(tmp_path)
    custom = tmp_path / "strategies" / "custom"
    for f in custom.glob("*.py"):
        f.unlink()
    assert seed_missing_builtins(tmp_path) == 0
    assert len(list(custom.glob("*.py"))) == 0


def test_seeded_strategies_load_in_engine(tmp_path):
    from app.strategy.engine import StrategyEngine

    seed_missing_builtins(tmp_path)
    engine = StrategyEngine(strategy_dirs=[tmp_path / "strategies" / "custom"])

    assert not engine.load_errors(), engine.load_errors()
    assert len(engine.list_strategies()) == 25
