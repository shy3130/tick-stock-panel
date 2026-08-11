"""研究代码使用的稳定项目路径。

所有研究脚本通过这里访问数据与产物目录，避免脚本移动后继续依赖
``Path(__file__).parent`` 的脆弱相对层级。
"""

from __future__ import annotations

from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent

DATA_DIR = PROJECT_ROOT / "data"
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"
CURRENT_ARTIFACTS_DIR = ARTIFACTS_DIR / "current"
ARCHIVE_ARTIFACTS_DIR = ARTIFACTS_DIR / "archive"
FACTOR_ARTIFACTS_DIR = ARCHIVE_ARTIFACTS_DIR / "factors"
REGIME_ARTIFACTS_DIR = ARCHIVE_ARTIFACTS_DIR / "regime"
OPTIMIZATION_ARTIFACTS_DIR = ARCHIVE_ARTIFACTS_DIR / "optimization"
VALIDATION_ARTIFACTS_DIR = ARCHIVE_ARTIFACTS_DIR / "validation"
LOGS_DIR = ARTIFACTS_DIR / "logs"


def ensure_artifact_dirs() -> None:
    """创建允许由研究脚本写入的产物目录。"""

    for path in (
        CURRENT_ARTIFACTS_DIR,
        FACTOR_ARTIFACTS_DIR,
        REGIME_ARTIFACTS_DIR,
        OPTIMIZATION_ARTIFACTS_DIR,
        VALIDATION_ARTIFACTS_DIR,
        LOGS_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
