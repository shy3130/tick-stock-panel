"""Tests for the reference-counted, generation-aware connection lease."""
from __future__ import annotations

from app.data_providers.fquant.lease import ConnectionSet


class FakeConn:
    def __init__(self, path: str) -> None:
        self.path = path
        self.closed = False

    def close(self) -> None:
        self.closed = True


def make_set():
    opened: list[FakeConn] = []

    def opener(path: str) -> FakeConn:
        c = FakeConn(path)
        opened.append(c)
        return c

    return ConnectionSet(opener), opened


def test_same_path_reuses_connection():
    cs, opened = make_set()
    with cs.lease("gen1/tdx.duckdb") as a:
        with cs.lease("gen1/tdx.duckdb") as b:
            assert a is b
    assert len(opened) == 1


def test_swap_keeps_inflight_connection_open_until_released():
    cs, opened = make_set()
    # Query A opens gen1 and holds its lease across a generation swap.
    with cs.lease("gen1/tdx.duckdb") as conn_a:
        assert conn_a.closed is False
        # A newer generation is published; query B leases gen2 (the swap).
        with cs.lease("gen2/tdx.duckdb") as conn_b:
            assert conn_b.path == "gen2/tdx.duckdb"
            # gen1 was retired by the swap but MUST stay open: A still holds it.
            assert conn_a.closed is False
        # B released gen2 (still current, not retired) -> stays open.
        assert conn_b.closed is False
    # A released gen1 -> retired + idle -> now closed.
    assert conn_a.closed is True


def test_close_defers_until_refcount_zero():
    cs, opened = make_set()
    with cs.lease("gen1/tdx.duckdb") as conn:
        cs.close()  # close requested while a lease is held
        assert conn.closed is False  # must not close under an active lease
    assert conn.closed is True  # closed once the lease is released


def test_close_closes_idle_connection():
    cs, opened = make_set()
    with cs.lease("gen1/tdx.duckdb") as conn:
        pass
    assert conn.closed is False  # current generation, still cached
    cs.close()
    assert conn.closed is True


def test_acquire_after_close_raises():
    cs, _ = make_set()
    cs.close()
    try:
        with cs.lease("gen1/tdx.duckdb"):
            pass
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError leasing a closed set")
