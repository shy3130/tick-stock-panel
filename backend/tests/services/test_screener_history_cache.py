"""ScreenerService 进程级历史窗口缓存的回归测试。

覆盖: 命中不重算、LRU(2 项)淘汰最旧、累计字节 ≤ 256MiB、单帧超大绕过
(返回但不缓存)、TTL 过期失效、data root / cache_generation 隔离、
并发 get/put 不破坏 OrderedDict。通过 monkeypatch _frame_estimated_size
控制字节,避免分配数百 MiB。
"""
from __future__ import annotations

import threading
import time
from datetime import date

import polars as pl

import app.services.screener as screener


def _clear_cache() -> None:
    screener._history_cache_clear()


def _mkdf(tag: str = "x", n: int = 3) -> pl.DataFrame:
    return pl.DataFrame(
        {"symbol": [tag] * n, "date": [date(2026, 1, 1)] * n, "v": list(range(n))}
    )


def _key(root: str = "/data", gen: int = 0, d: date = date(2026, 1, 1), lb: int = 30):
    return (root, gen, d, lb)


def test_repeated_request_hits_cache_without_recompute():
    _clear_cache()
    df = _mkdf("hit")
    now = time.monotonic()
    screener._history_cache_put(_key(), now, df)
    got = screener._history_cache_get(_key(), now + 1.0)
    assert got is not None
    assert got.equals(df)
    # 命中同一帧对象(未重算)
    assert got is screener._history_cache[_key()][1]
    _clear_cache()


def test_third_entry_evicts_oldest_lru(monkeypatch):
    _clear_cache()
    now = time.monotonic()
    monkeypatch.setattr(screener, "_frame_estimated_size", lambda df: 1)
    for i in range(3):
        screener._history_cache_put(
            _key(root=f"/d{i}", d=date(2026, 1, 1 + i)), now + i, _mkdf(str(i))
        )
    # 上限 2 项,最早项 d0 被淘汰
    assert screener._history_cache_get(_key(root="/d0"), now + 10) is None
    assert screener._history_cache_get(
        _key(root="/d1", d=date(2026, 1, 2)), now + 10,
    ) is not None
    assert screener._history_cache_get(
        _key(root="/d2", d=date(2026, 1, 3)), now + 10,
    ) is not None
    assert len(screener._history_cache) == 2
    _clear_cache()


def test_cumulative_bytes_never_exceed_limit(monkeypatch):
    _clear_cache()
    cap = screener._HISTORY_CACHE_MAX_BYTES
    # 每帧 0.6 * cap,两帧即超 cap -> 第二帧写入应淘汰第一帧
    monkeypatch.setattr(screener, "_frame_estimated_size", lambda df: int(cap * 0.6))
    now = time.monotonic()
    screener._history_cache_put(_key(root="/a"), now, _mkdf("a"))
    screener._history_cache_put(_key(root="/b"), now + 1, _mkdf("b"))
    total = sum(sz for _, _, sz in screener._history_cache.values())
    assert total <= cap
    # a 被淘汰以让 b 留下
    assert screener._history_cache_get(_key(root="/a"), now + 2) is None
    assert screener._history_cache_get(_key(root="/b"), now + 2) is not None
    _clear_cache()


def test_oversized_frame_returned_but_not_cached(monkeypatch):
    _clear_cache()
    cap = screener._HISTORY_CACHE_MAX_BYTES
    monkeypatch.setattr(screener, "_frame_estimated_size", lambda df: cap + 1)
    now = time.monotonic()
    screener._history_cache_put(_key(), now, _mkdf("big"))
    # 超大不入缓存
    assert screener._history_cache_get(_key(), now + 1) is None
    assert len(screener._history_cache) == 0
    _clear_cache()


def test_ttl_expiry_evicts_entry():
    _clear_cache()
    now = time.monotonic()
    screener._history_cache_put(_key(), now, _mkdf())
    # 未过期
    assert screener._history_cache_get(_key(), now + screener._HISTORY_CACHE_TTL - 1) is not None
    # 过期
    assert screener._history_cache_get(_key(), now + screener._HISTORY_CACHE_TTL + 1) is None
    _clear_cache()


def test_cache_key_isolates_data_root_and_generation():
    _clear_cache()
    now = time.monotonic()
    screener._history_cache_put(_key(root="/data", gen=0), now, _mkdf("a"))
    # 不同 data root
    assert screener._history_cache_get(_key(root="/other", gen=0), now + 1) is None
    # 不同 generation(管道刷新后)
    assert screener._history_cache_get(_key(root="/data", gen=1), now + 1) is None
    # 相同 key 命中
    assert screener._history_cache_get(_key(root="/data", gen=0), now + 1) is not None
    _clear_cache()


def test_empty_frame_is_not_cached():
    _clear_cache()
    now = time.monotonic()
    screener._history_cache_put(_key(), now, pl.DataFrame())
    assert screener._history_cache_get(_key(), now + 1) is None
    assert len(screener._history_cache) == 0
    _clear_cache()


def test_replace_existing_key_moves_to_end(monkeypatch):
    _clear_cache()
    monkeypatch.setattr(screener, "_frame_estimated_size", lambda df: 1)
    now = time.monotonic()
    k = _key()
    screener._history_cache_put(k, now, _mkdf("v1"))
    screener._history_cache_put(k, now + 1, _mkdf("v2"))
    # 同一 key 只剩一份,值为最新
    assert len(screener._history_cache) == 1
    got = screener._history_cache_get(k, now + 2)
    assert got is not None
    assert got["symbol"][0] == "v2"
    _clear_cache()


def test_concurrent_get_put_does_not_corrupt(monkeypatch):
    _clear_cache()
    monkeypatch.setattr(screener, "_frame_estimated_size", lambda df: 1)
    stop = threading.Event()
    errors: list[BaseException] = []

    def worker(tag: str) -> None:
        try:
            i = 0
            while not stop.is_set() and i < 200:
                k = _key(root=f"/{tag}", d=date(2026, 1, 1 + (i % 5)))
                now = time.monotonic()
                screener._history_cache_get(k, now)
                screener._history_cache_put(k, now, _mkdf(tag))
                i += 1
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(str(t),)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    assert not errors, errors
    # 收敛后不超过条数上限
    assert len(screener._history_cache) <= screener._HISTORY_CACHE_MAX_ENTRIES
    _clear_cache()


def test_clear_history_cache_drops_all():
    _clear_cache()
    now = time.monotonic()
    screener._history_cache_put(_key(root="/a"), now, _mkdf())
    screener._history_cache_put(_key(root="/b"), now, _mkdf())
    screener.ScreenerService.clear_history_cache()
    assert len(screener._history_cache) == 0


def test_close_screener_sql_connection_is_idempotent(monkeypatch):
    class Connection:
        close_calls = 0

        def close(self):
            self.close_calls += 1

    conn = Connection()
    monkeypatch.setattr(screener, "_screener_sql_conn", conn)

    screener.close_screener_sql_connection()
    screener.close_screener_sql_connection()

    assert conn.close_calls == 1
    assert screener._screener_sql_conn is None
