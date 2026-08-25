"""backend/tests 全局隔离: 防止真实挂载的数据根泄漏进测试。"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_canonical_history_root(tmp_path_factory, monkeypatch):
    """默认把外部 canonical-history 根指向空目录。

    repository 的默认根是 /Volumes/WD1 真实挂载; 此前外部 generation 扫描
    因 hive 布局读取失败而隐式回退本地, hive 修复后外部可读, 未显式设置该
    env 的测试会把真实 17M 行 generation 合并进 tmp 夹具。需要外部历史的
    测试自行 monkeypatch.setenv 覆盖。
    """
    monkeypatch.setenv(
        "TICKFLOW_CANONICAL_HISTORY_ROOT",
        str(tmp_path_factory.mktemp("no-canonical-history")),
    )
