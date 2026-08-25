"""Dynamic extension-column SQL safety regression tests."""
from __future__ import annotations

import json
from types import SimpleNamespace

import duckdb
import pytest

from app.api import ext_data as ext_data_api
from app.api import kline, screener, watchlist
from app.db_safe import is_valid_ext_ident, quote_ident
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("close", '"close"'),
        ('指标"名称', '"指标""名称"'),
        ('close" UNION SELECT secret FROM credentials --', '"close"" UNION SELECT secret FROM credentials --"'),
    ],
)
def test_quote_ident_keeps_untrusted_field_as_one_identifier(name: str, expected: str):
    assert quote_ident(name) == expected


def test_quoted_identifier_does_not_execute_copy_payload(tmp_path):
    target = tmp_path / "injected.csv"
    payload = f'close" COPY (SELECT 1) TO \'{target}\' --'

    connection = duckdb.connect(":memory:")
    connection.execute("CREATE TABLE ext_safe (symbol VARCHAR, close DOUBLE)")
    connection.execute("INSERT INTO ext_safe VALUES ('000001.SZ', 12.3)")

    with pytest.raises(duckdb.BinderException):
        connection.execute(
            f"SELECT symbol, {quote_ident(payload)} FROM ext_safe"
        ).fetchall()

    assert not target.exists()


def test_config_identifier_allowlist_rejects_unsafe_names():
    assert is_valid_ext_ident("ext_123") is True
    assert is_valid_ext_ident('bad" UNION SELECT 1 --') is False
    assert is_valid_ext_ident("ext-name") is False


def test_kline_ext_columns_skip_unsafe_config_id(tmp_path):
    response = {"stock_info": {}}
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    assert kline._attach_ext(
        response,
        repo,
        "000001.SZ",
        'bad" UNION SELECT 1 --.close',
    ) is response


def test_kline_ext_columns_skip_nul_field_name(tmp_path):
    response = {"stock_info": {}}
    repo = SimpleNamespace(
        store=SimpleNamespace(
            data_dir=tmp_path,
            db=SimpleNamespace(query=lambda _sql: pytest.fail("unsafe field reached DuckDB")),
        )
    )

    assert kline._attach_ext(response, repo, "000001.SZ", "good_1.close\x00copy") is response


@pytest.mark.parametrize(
    "parse_ext_columns",
    [watchlist._parse_ext_columns, screener._parse_ext_columns],
)
def test_ext_column_parsers_reject_unsafe_config_ids(parse_ext_columns):
    assert parse_ext_columns('good_1.中文.字段,bad" UNION SELECT 1 --.close') == [
        ("good_1", "中文.字段"),
    ]


@pytest.mark.parametrize(
    "parse_ext_columns",
    [watchlist._parse_ext_columns, screener._parse_ext_columns],
)
def test_ext_column_parsers_reject_nul_field_names(parse_ext_columns):
    assert parse_ext_columns("good_1.close\x00copy") == []



def _config(config_id: str) -> ExtConfig:
    return ExtConfig(
        id=config_id,
        label="安全测试",
        mode="snapshot",
        fields=[ExtField("close", "float")],
    )


def test_ext_config_store_rejects_path_traversal_ids(tmp_path):
    data_dir = tmp_path / "data"
    store = ExtConfigStore(data_dir)

    with pytest.raises(ValueError, match="只能包含"):
        store.upsert(_config("../outside"))

    assert store.get("../outside") is None
    assert store.delete("../outside") is False
    assert not (data_dir / "outside").exists()


def test_ext_config_store_ignores_poisoned_config_metadata(tmp_path):
    data_dir = tmp_path / "data"
    config_dir = data_dir / "ext_data" / "safe"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"id": 'safe" UNION SELECT 1 --', "label": "坏配置"}),
        encoding="utf-8",
    )

    store = ExtConfigStore(data_dir)
    assert store.load_all() == []
    assert store.get("safe") is None


def test_refresh_views_skips_poisoned_config_id(tmp_path):
    data_dir = tmp_path / "data"
    config_dir = data_dir / "ext_data" / "safe"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"id": 'safe" COPY (SELECT 1) TO \'/tmp/evil\' --'}),
        encoding="utf-8",
    )
    (config_dir / "part.parquet").touch()

    executed: list[str] = []
    db = SimpleNamespace(execute=lambda sql: executed.append(sql))
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=SimpleNamespace(store=SimpleNamespace(data_dir=data_dir, db=db))
            )
        )
    )

    ext_data_api._refresh_views(request)

    assert executed == []