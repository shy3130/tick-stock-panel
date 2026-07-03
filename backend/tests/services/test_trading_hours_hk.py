"""Realtime/depth trading-hour gates cover HK afternoon and A-share buffers."""
from datetime import datetime

from app.services.depth_service import DepthService
from app.services.quote_service import QuoteService


def _fixed_datetime(when):
    class _D(datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    return _D


def test_trading_hours_cover_hk_afternoon(monkeypatch):
    when = datetime(2026, 7, 1, 15, 30)
    monkeypatch.setattr("app.services.quote_service.datetime", _fixed_datetime(when))
    monkeypatch.setattr("app.services.depth_service.datetime", _fixed_datetime(when))

    assert QuoteService._is_trading_hours() is True
    assert DepthService._is_trading_hours() is True


def test_trading_hours_preserve_a_share_opening_buffer(monkeypatch):
    when = datetime(2026, 7, 1, 9, 15)
    monkeypatch.setattr("app.services.quote_service.datetime", _fixed_datetime(when))
    monkeypatch.setattr("app.services.depth_service.datetime", _fixed_datetime(when))

    assert QuoteService._is_trading_hours() is True
    assert DepthService._is_trading_hours() is True
