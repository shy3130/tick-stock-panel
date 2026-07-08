"""验证 FQuantProvider 按 FQUANT_FSTORE_MODE 选择正确的 fstore 客户端。"""
from __future__ import annotations

import os

import pytest

from app.data_providers.fquant.fstore_client import FStoreClient
from app.data_providers.fquant.fstore_duckdb_client import FStoreDuckDBClient
from app.data_providers.fquant_provider import FQuantProvider


def test_default_mode_is_postgres(monkeypatch):
    monkeypatch.delenv("FQUANT_FSTORE_MODE", raising=False)
    provider = FQuantProvider()
    assert isinstance(provider._fstore, FStoreClient)


def test_duckdb_mode_via_env(monkeypatch):
    monkeypatch.setenv("FQUANT_FSTORE_MODE", "duckdb")
    provider = FQuantProvider()
    assert isinstance(provider._fstore, FStoreDuckDBClient)


def test_unknown_mode_falls_back_to_postgres(monkeypatch):
    monkeypatch.setenv("FQUANT_FSTORE_MODE", "not_a_real_mode")
    provider = FQuantProvider()
    assert isinstance(provider._fstore, FStoreClient)
