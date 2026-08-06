from __future__ import annotations

from types import SimpleNamespace

from app.services.trading import red_flag_webhook, store


def _trade(tmp_path):
    trade = {
        "tradeId": "t1", "symbol": "600519.SH", "status": "持仓中",
        "position": {"qty": 10, "costPrice": 100, "invested": 1000},
    }
    store.write_trade(tmp_path, trade)


def test_no_webhook_url_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADING_RED_FLAG_WEBHOOK_URL", raising=False)
    assert red_flag_webhook.push_new_flags(tmp_path, "t1", [{"type": "loss_add", "ts": "t"}]) == 0


def test_pushes_new_flag_once(tmp_path, monkeypatch):
    _trade(tmp_path)
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(red_flag_webhook.httpx, "post", post)
    flags = [{"type": "loss_add", "ts": "2026-08-04 10:00", "price": 90, "costPrice": 100}]
    assert red_flag_webhook.push_new_flags(tmp_path, "t1", flags, "http://hook") == 1
    assert red_flag_webhook.push_new_flags(tmp_path, "t1", flags, "http://hook") == 0
    assert len(calls) == 1
    assert calls[0][1]["json"]["symbol"] == "600519.SH"
    assert calls[0][1]["timeout"] == 3.0
    assert calls[0][1]["trust_env"] is False


def test_distinct_flags_are_sent(tmp_path, monkeypatch):
    _trade(tmp_path)
    monkeypatch.setattr(
        red_flag_webhook.httpx,
        "post",
        lambda *args, **kwargs: SimpleNamespace(raise_for_status=lambda: None),
    )
    flags = [
        {"type": "loss_add", "ts": "t1", "price": 90},
        {"type": "gate_bypassed", "ts": "t2", "kind": "fill"},
    ]
    assert red_flag_webhook.push_new_flags(tmp_path, "t1", flags, "http://hook") == 2


def test_failure_does_not_mark_sent_or_raise(tmp_path, monkeypatch):
    _trade(tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(red_flag_webhook.httpx, "post", fail)
    flag = [{"type": "audit_missing", "ts": "t1", "kind": "fill"}]
    assert red_flag_webhook.push_new_flags(tmp_path, "t1", flag, "http://hook") == 0
    assert red_flag_webhook._sent_keys(tmp_path) == set()
