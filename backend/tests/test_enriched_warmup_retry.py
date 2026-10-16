"""enriched warmup 世代漂移有界重建 — GH #480 契约测试。

后台 warmup 的 compute 窗口长达数十秒, 启动期与 quote 实时刷新的
commit 撞车后快照被 fail-closed 丢弃; 丢弃后必须以新世代重建而非把
缓存长期留冷。仅 warmup 线程重试, 同步/懒加载路径保持单次 (不阻塞 HTTP)。
"""
from __future__ import annotations

from app.enriched_generation import EnrichedGenerationUnavailableError
from app.tickflow.repository import (
    _ENRICHED_WARMUP_MAX_ATTEMPTS,
    _ENRICHED_WARMUP_PUBLISH_WAIT_SECONDS,
    DataStore,
    KlineRepository,
)


def _repo(tmp_path) -> KlineRepository:
    return KlineRepository(DataStore(tmp_path))


def test_warmup_rebuilds_after_generation_discard(tmp_path, monkeypatch):
    """丢弃后重建: 第 1 轮换代丢弃, 第 2 轮成功, 缓存建成。"""
    repo = _repo(tmp_path)
    calls: list[int] = []

    def fake_refresh() -> bool:
        calls.append(1)
        if len(calls) == 1:
            return True  # 换代丢弃, 可重试
        repo._enriched_history_cache = object()  # 第 2 轮建成
        return False

    monkeypatch.setattr(repo, "_refresh_enriched", fake_refresh)
    monkeypatch.setattr("app.tickflow.repository.enriched_publication_incomplete", lambda *a: False)

    repo._warmup_enriched()

    assert len(calls) == 2
    assert repo._enriched_history_cache is not None


def test_warmup_single_success_no_retry(tmp_path, monkeypatch):
    """一次成功不重试。"""
    repo = _repo(tmp_path)
    calls: list[int] = []

    def fake_refresh() -> bool:
        calls.append(1)
        return False

    monkeypatch.setattr(repo, "_refresh_enriched", fake_refresh)

    repo._warmup_enriched()

    assert len(calls) == 1


def test_warmup_gives_up_after_max_attempts(tmp_path, monkeypatch):
    """持续换代: 有界上限后放弃, 保持冷缓存降级 (非无限重读)。"""
    repo = _repo(tmp_path)
    calls: list[int] = []
    sleeps: list[float] = []

    def fake_refresh() -> bool:
        calls.append(1)
        return True  # 每次都换代丢弃

    monkeypatch.setattr(repo, "_refresh_enriched", fake_refresh)
    monkeypatch.setattr("app.tickflow.repository.enriched_publication_incomplete", lambda *a: True)
    monkeypatch.setattr("app.tickflow.repository.time.sleep", lambda s: sleeps.append(s))

    repo._warmup_enriched()

    assert len(calls) == _ENRICHED_WARMUP_MAX_ATTEMPTS
    assert repo._enriched_history_cache is None
    # 第 2 轮起发布未完成才等待: 等待次数 = 轮数 - 1
    assert sleeps == [_ENRICHED_WARMUP_PUBLISH_WAIT_SECONDS] * (_ENRICHED_WARMUP_MAX_ATTEMPTS - 1)


def test_refresh_enriched_itself_is_single_shot(tmp_path, monkeypatch):
    """_refresh_enriched 本身单次: 换代返回 True, 成功/其他失败返回 False。

    同步/懒加载调用方不读返回值, 单次语义不变, 不阻塞 HTTP。
    """
    repo = _repo(tmp_path)

    # 成功建成 → False
    monkeypatch.setattr(repo, "_refresh_enriched_impl", lambda: None)
    assert repo._refresh_enriched() is False

    # 换代丢弃 → True (可重试)
    def raise_generation(*a, **k):
        raise EnrichedGenerationUnavailableError("changed")

    monkeypatch.setattr(repo, "_refresh_enriched_impl", raise_generation)
    assert repo._refresh_enriched() is True

    # 非换代失败 → False (不 spin, 保持原有降级)
    def raise_other(*a, **k):
        raise RuntimeError("disk io")

    monkeypatch.setattr(repo, "_refresh_enriched_impl", raise_other)
    assert repo._refresh_enriched() is False
