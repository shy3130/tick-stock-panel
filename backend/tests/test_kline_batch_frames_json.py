"""批量端点 Polars 直出 JSON 的字节级等价契约。

_batch_frames_json 取代 to_dicts()+json.dumps (对象图与序列化耗时一起消失,
见 kline.py 该函数 docstring 的实测数字)。直出的任何格式漂移 —— 键序、浮点
表示、日期格式、NaN/inf 语义、extra 键拼接 —— 都会破坏前端与已冻结扩展
bundle 的解析 (前端 since 增量还按 datetime 字符串字典序比较), 用字节级
断言锁定与旧 dict 路径完全一致。
"""

from __future__ import annotations

import gzip
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl

from app.api.kline import _batch_frames_json, _gzip_raw, _json_safe


def _minute_frame(sym: str) -> pl.DataFrame:
    """真实分时列集合; 含典型价格浮点、inf/NaN/None 三种非有限与缺失形态。"""
    start = datetime(2026, 10, 23, 9, 31)
    return pl.DataFrame({
        "symbol": [sym] * 5,
        "datetime": [start + timedelta(minutes=i) for i in range(5)],
        "open": [10.5, 3.33, 0.01, 9999.99, 1.5],
        "high": [10.6, 3.34, 0.02, 10000.0, float("inf")],
        "low": [10.4, 3.32, 0.005, 9990.0, float("nan")],
        "close": [10.55, 3.335, 0.015, 9995.55, 2.5],
        "volume": [123456, 0, 999999999, 1, 42],
        "amount": [1.5e8, 0.0, 2.5e6, 9999.5, None],
    })


def test_frames_json_byte_identical_to_dict_path():
    """直出与 to_dicts()+dumps (与 _gzip_payload 同参 + _json_safe 净化) 逐字节一致。"""
    parts = {
        "600000.SH": _minute_frame("600000.SH"),
        "000001.SZ": _minute_frame("000001.SZ"),
    }
    new = _batch_frames_json(parts, "datetime", "%Y-%m-%dT%H:%M:%S")

    def _iso(o):
        return o.isoformat() if hasattr(o, "isoformat") else str(o)

    old = json.dumps(
        _json_safe({"data": {s: sub.to_dicts() for s, sub in parts.items()}}),
        ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=_iso,
    ).encode()
    assert new == old


def test_frames_json_extra_keys_spliced_into_root():
    """extra 键并入根对象 (而非拼成兄弟对象), 键序与旧 dict 路径一致。"""
    raw = _batch_frames_json(
        {"600000.SH": _minute_frame("600000.SH")},
        "datetime", "%Y-%m-%dT%H:%M:%S",
        {"full_minute_local": True, "incremental": True, "truncated": ["600000.SH"]},
    )
    payload = json.loads(raw)
    assert payload["full_minute_local"] is True
    assert payload["incremental"] is True
    assert payload["truncated"] == ["600000.SH"]
    assert list(payload) == ["data", "full_minute_local", "incremental", "truncated"]


def test_frames_json_daily_date_format():
    """日K的 date 列输出 ISO 日期串, 与 date.isoformat 语义一致。"""
    df = pl.DataFrame({
        "symbol": ["000001.SZ"] * 2,
        "date": [date(2026, 10, 22), date(2026, 10, 23)],
        "open": [1.0, 2.0], "high": [1.1, 2.1], "low": [0.9, 1.9],
        "close": [1.05, 2.05], "volume": [100, 200],
    })
    payload = json.loads(_batch_frames_json({"000001.SZ": df}, "date", "%Y-%m-%d"))
    assert [r["date"] for r in payload["data"]["000001.SZ"]] == ["2026-10-22", "2026-10-23"]


def test_gzip_raw_negotiation_and_threshold(monkeypatch):
    """_gzip_raw 与 _gzip_payload 同规则: 偏好 / 客户端协商 / 1024 阈值。"""
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_minute_batch_compress", lambda: True)
    monkeypatch.setattr(preferences, "get_daily_batch_compress", lambda: False)

    big = b'{"data":{"x":"' + b"y" * 2048 + b'"}}'
    req_gz = SimpleNamespace(headers={"accept-encoding": "gzip"})
    req_plain = SimpleNamespace(headers={})

    resp = _gzip_raw(req_gz, big, pref_key="minute_batch_compress")
    assert resp.headers["content-encoding"] == "gzip"
    assert gzip.decompress(resp.body) == big

    # 客户端不接受 gzip → 原样 (尊重协商)
    resp = _gzip_raw(req_plain, big, pref_key="minute_batch_compress")
    assert "content-encoding" not in resp.headers
    assert resp.body == big

    # 偏好关 → 原样
    resp = _gzip_raw(req_gz, big, pref_key="daily_batch_compress")
    assert "content-encoding" not in resp.headers

    # 未超 1024 阈值 → 原样
    resp = _gzip_raw(req_gz, b'{"data":{}}', pref_key="minute_batch_compress")
    assert "content-encoding" not in resp.headers
    assert resp.body == b'{"data":{}}'
