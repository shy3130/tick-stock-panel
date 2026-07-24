from app.api import dow_strategy


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_dow_strategy_run_proxy_starts_and_reads_realtime_job(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(("POST", url, kwargs))
        return _Response({"runId": "scan-hk-1", "status": "queued"})

    def fake_get(url, **kwargs):
        calls.append(("GET", url, kwargs))
        return _Response({"runId": "scan-hk-1", "status": "running", "completed": 8, "total": 2600})

    monkeypatch.setattr(dow_strategy.httpx, "post", fake_post)
    monkeypatch.setattr(dow_strategy.httpx, "get", fake_get)

    started = dow_strategy.start_run({"market": "hk"})
    status = dow_strategy.run_status("scan-hk-1")

    assert started["runId"] == "scan-hk-1"
    assert status["completed"] == 8
    assert calls[0][0] == "POST"
    assert calls[0][1].endswith("/api/dow-strategy/runs")
    assert calls[0][2]["json"] == {"market": "hk"}
    assert calls[1][1].endswith("/api/dow-strategy/runs/scan-hk-1")
