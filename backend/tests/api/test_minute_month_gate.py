from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.kline import _ensure_minute_capable, extend_minute_history
from app.capabilities import Cap, CapabilityLimits, CapabilitySet


class _Request:
    def __init__(self, capset: CapabilitySet) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(repo=object(), capabilities=capset))

    async def json(self) -> dict:
        return {"value": 1, "unit": "month"}


def test_day_needs_only_minute_batch():
    capset = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits(batch=200)})

    _ensure_minute_capable(capset, "day")


def test_month_blocked_without_month_cap():
    capset = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits(batch=200)})

    with pytest.raises(HTTPException) as exc:
        _ensure_minute_capable(capset, "month")

    assert exc.value.status_code == 403
    assert "按月" in exc.value.detail


def test_month_allowed_with_month_cap():
    capset = CapabilitySet({
        Cap.KLINE_MINUTE_BATCH: CapabilityLimits(batch=200),
        Cap.KLINE_MINUTE_MONTH: CapabilityLimits(batch=200),
    })

    _ensure_minute_capable(capset, "month")


def test_no_minute_batch_blocks_all():
    capset = CapabilitySet({})

    with pytest.raises(HTTPException) as exc:
        _ensure_minute_capable(capset, "day")

    assert exc.value.status_code == 403
    assert "批量分钟K" in exc.value.detail


@pytest.mark.asyncio
async def test_month_extend_requires_month_capability():
    capset = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits(batch=200)})
    with pytest.raises(HTTPException) as exc:
        await extend_minute_history(_Request(capset))
    assert exc.value.status_code == 403
    assert "当前数据源不支持按月扩展分钟K历史" in exc.value.detail
