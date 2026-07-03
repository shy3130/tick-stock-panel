from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import research


def request(tmp_path):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, scheduler=None)))


def test_schedule_crud_and_run_now(tmp_path):
    req = request(tmp_path)
    item = research.create_schedule(research.ScheduleIn(name="周报", template="strategy_pool_weekly", cron="0 18 * * 5"), req)

    assert research.list_schedules(req)["items"][0]["id"] == item["id"]
    out = research.run_schedule_now(item["id"], req)
    assert out["schedule"]["last_status"] == "success"
    assert research.delete_schedule(item["id"], req) == {"ok": True}


def test_schedule_invalid_template_maps_400(tmp_path):
    with pytest.raises(HTTPException) as exc:
        research.create_schedule(research.ScheduleIn(name="x", template="bad", cron="0 1 * * *"), request(tmp_path))

    assert exc.value.status_code == 400


def test_schedule_missing_maps_404(tmp_path):
    with pytest.raises(HTTPException) as exc:
        research.patch_schedule("missing", research.SchedulePatch(name="x"), request(tmp_path))

    assert exc.value.status_code == 404
