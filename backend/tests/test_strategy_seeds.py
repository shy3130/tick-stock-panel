"""策略种子 seed_missing_builtins — 去内置化补全回归(仅升级用户)。

新版本没有内置策略: 新装环境策略库为空、不种入; 只有检测到旧版使用
痕迹 (已有策略/日K数据/自选) 的升级数据目录才把 25 个原内置策略按
文件名补回 custom。此处回归:
  - 新装目录 (无痕迹) 跳过种入并落标记, 之后用久了也不回头种;
  - 升级目录 (有痕迹) 一次写入全部 25 个, 标记防重;
  - 已有同名文件一律不覆盖 (用户自行迁移过的零写入);
  - 删除后重启不复活;
  - 种入的文件能被引擎真实加载。
"""
from __future__ import annotations

from app.strategy.strategy_seeds import SEEDS_DIR, seed_missing_builtins

_EXPECTED_COUNT = len([f for f in SEEDS_DIR.glob("*.py") if not f.name.startswith("_")])


def _make_upgrade_dir(tmp_path) -> None:
    """造升级用户痕迹: 拉过日K (旧版本使用的最普遍特征)。"""
    d = tmp_path / "kline_daily" / "date=2026-01-01"
    d.mkdir(parents=True)
    (d / "part.parquet").write_bytes(b"fake")


def test_fresh_install_skips_seeding(tmp_path):
    """新装用户: 无旧版痕迹, 不种入, 策略库保持为空。"""
    n = seed_missing_builtins(tmp_path)
    custom = tmp_path / "strategies" / "custom"

    assert n == 0
    assert len(list(custom.glob("*.py"))) == 0
    assert (custom / ".seeded_v1").exists()
    assert "skipped" in (custom / ".seeded_v1").read_text(encoding="utf-8")


def test_fresh_install_never_seeds_later_even_after_usage(tmp_path):
    """新用户用久后 (出现数据痕迹) 重启: 标记已落, 不回头种入。"""
    seed_missing_builtins(tmp_path)
    _make_upgrade_dir(tmp_path)  # 之后拉了数据

    assert seed_missing_builtins(tmp_path) == 0
    assert len(list((tmp_path / "strategies" / "custom").glob("*.py"))) == 0


def test_watchlist_counts_as_upgrade_trace(tmp_path):
    """自选记录也构成升级痕迹 (没拉过K线但用过的用户)。"""
    w = tmp_path / "user_data"
    w.mkdir(parents=True)
    (w / "watchlist.parquet").write_bytes(b"fake")

    assert seed_missing_builtins(tmp_path) == _EXPECTED_COUNT


def test_seed_writes_all_and_marks(tmp_path):
    _make_upgrade_dir(tmp_path)
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
    # (已有策略文件本身就是升级痕迹)
    (custom / "boll_breakout.py").write_text("# 用户自定义版本\n", encoding="utf-8")
    (custom / "my_own.py").write_text("# 用户自己的策略\n", encoding="utf-8")

    n = seed_missing_builtins(tmp_path)

    assert n == _EXPECTED_COUNT - 1
    assert (custom / "boll_breakout.py").read_text(encoding="utf-8") == "# 用户自定义版本\n"
    assert (custom / "my_own.py").read_text(encoding="utf-8") == "# 用户自己的策略\n"
    assert len(list(custom.glob("*.py"))) == _EXPECTED_COUNT + 1


def test_seed_no_resurrect_after_user_deletes(tmp_path):
    _make_upgrade_dir(tmp_path)
    seed_missing_builtins(tmp_path)
    custom = tmp_path / "strategies" / "custom"
    for f in custom.glob("*.py"):
        f.unlink()

    assert seed_missing_builtins(tmp_path) == 0
    assert len(list(custom.glob("*.py"))) == 0


def test_seeded_strategies_load_in_engine(tmp_path):
    from app.strategy.engine import StrategyEngine

    _make_upgrade_dir(tmp_path)
    seed_missing_builtins(tmp_path)
    engine = StrategyEngine(strategy_dirs=[tmp_path / "strategies" / "custom"])

    assert not engine.load_errors(), engine.load_errors()
    assert len(engine.list_strategies()) == 25
