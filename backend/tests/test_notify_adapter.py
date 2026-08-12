from __future__ import annotations

from types import SimpleNamespace

from app.services import notify_adapter


def test_osascript_notification_passes_untrusted_text_as_argv(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(notify_adapter.subprocess, "run", fake_run)
    payload = '\\"); do shell script "touch /tmp/injected"; --'

    assert notify_adapter._notify_osascript("title", payload) is True
    args = captured["args"]
    assert payload not in args[2]
    assert args[-2:] == ["title", payload]
    assert captured["kwargs"]["timeout"] == 5
