"""AI 策略发布闸最小 demo — 验证「草稿 → 显式发布」门 与 复盘推送模式。

运行方式(在 backend/ 目录下, 已安装依赖):
    python scripts/demo_ai_publish_gate.py

不启动服务, 直接调用内部函数(与单测同款 SimpleNamespace 请求桩), 全程落在临时目录。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from app.api.strategy import (
    StrategyCodeSaveRequest,
    _save_strategy_code,
    publish_ai_strategy,
)
from app.services import preferences
from app.strategy.engine import StrategyEngine

_CODE = '''"""demo 策略"""
import polars as pl

META = {
    "id": "ai_demo",
    "name": "demo",
    "description": "demo",
    "tags": [],
    "params": [],
    "scoring": {},
}

ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 20

def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.lit(True)
'''


def _request(data_dir: Path, engine: StrategyEngine):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, strategy_engine=engine)))


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        engine = StrategyEngine(strategy_dirs=[data_dir / "strategies" / "custom",
                                               data_dir / "strategies" / "ai"])
        request = _request(data_dir, engine)

        print("== 1. 保存 AI 策略(默认草稿) ==")
        result = _save_strategy_code(StrategyCodeSaveRequest(
            strategy_id="ai_demo", target_source="ai", mode="create",
            code=_CODE, name="demo",
        ), request)
        public = [m["id"] for m in engine.list_strategies() if not m.get("research_only")]
        print(f"   research_only={result['research_only']} (期望 True)")
        print(f"   公开列表={public} (期望不含 ai_demo)")

        print("== 2. 显式 publish ==")
        print("   ", publish_ai_strategy("ai_demo", request))
        print(f"   research_only={engine.get('ai_demo').meta['research_only']} (期望 False)")

        print("== 3. 重复 publish 应被拒 ==")
        try:
            publish_ai_strategy("ai_demo", request)
        except Exception as exc:  # noqa: BLE001
            print(f"   被拒: {exc}")

        print("== 4. 自定义策略 publish 应被拒 ==")
        _save_strategy_code(StrategyCodeSaveRequest(
            strategy_id="custom_demo", target_source="custom", mode="create",
            code=_CODE.replace("ai_demo", "custom_demo"), name="custom",
        ), request)
        try:
            publish_ai_strategy("custom_demo", request)
        except Exception as exc:  # noqa: BLE001
            print(f"   被拒: {exc}")

    # 复盘推送模式(隔离到临时文件, 不污染真实偏好)
    prefs_path = Path(tempfile.mkdtemp()) / "preferences.json"
    preferences._path = lambda: prefs_path  # noqa: SLF001 - demo 隔离
    preferences._invalidate_cache()
    print("== 5. 复盘推送模式 review_push_mode ==")
    print(f"   默认 = {preferences.get_review_push_mode()} (期望 manual)")
    print(f"   设为 auto = {preferences.set_review_push_mode('auto')}")
    print(f"   非法值回退 = {preferences.set_review_push_mode('bogus')} (期望 manual)")

    print("\n全部通过")


if __name__ == "__main__":
    main()
