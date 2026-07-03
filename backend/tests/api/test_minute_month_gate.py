from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.kline import extend_minute_history
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


class _Request:
    def __init__(self, capset: CapabilitySet) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(repo=object(), capabilities=capset))

    async def json(self) -> dict:
        return {"value": 1, "unit": "month"}


@pytest.mark.asyncio
async def test_month_extend_requires_month_capability():
    capset = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits(batch=200)})
    with pytest.raises(HTTPException) as exc:
        await extend_minute_history(_Request(capset))
    assert exc.value.status_code == 403
    assert "当前数据源不支持按月扩展分钟K历史" in exc.value.detail
