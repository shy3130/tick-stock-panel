"""stop()/disable() 的持久化语义 (issue 反馈: 重启容器后实时行情被持久化成关闭)。

stop() 被 lifespan shutdown 调用 (容器停止/重启都会触发), 不得写 preferences;
只有 disable() (用户主动关闭) 才持久化 realtime_quotes_enabled=False,
否则 boot_check() 下次启动读到 False, 实时行情永远不自动恢复。
"""

from __future__ import annotations

from app.services import preferences
from app.services.quote_service import QuoteService


def test_stop_does_not_persist_disabled_preference(monkeypatch):
    saved: list[dict] = []
    monkeypatch.setattr(preferences, "save", lambda updates: saved.append(updates))
    service = QuoteService()

    service.stop()

    assert saved == []


def test_disable_persists_disabled_preference(monkeypatch):
    saved: list[dict] = []
    monkeypatch.setattr(preferences, "save", lambda updates: saved.append(updates))
    service = QuoteService()

    service.disable()

    assert saved == [{"realtime_quotes_enabled": False}]
