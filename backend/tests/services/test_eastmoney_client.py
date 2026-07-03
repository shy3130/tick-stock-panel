import pytest

from app.services import eastmoney_client as em


def test_disallowed_host_rejected():
    with pytest.raises(ValueError, match="host not allowed"):
        em.get_json("https://evil.example.com/api")


def test_allowed_hosts_pass_validation():
    for url in (
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        "https://reportapi.eastmoney.com/report/list",
        "https://search-api-web.eastmoney.com/search/jsonp",
        "https://searchapi.eastmoney.com/api/suggest/get",
    ):
        assert em._check_host(url)


def test_datacenter_paged_merges_pages(monkeypatch):
    pages = {
        1: {"result": {"pages": 2, "data": [{"i": 1}]}},
        2: {"result": {"pages": 2, "data": [{"i": 2}]}},
    }

    def fake_get_json(url, params=None):
        return pages[int(params["pageNumber"])]

    monkeypatch.setattr(em, "get_json", fake_get_json)
    rows = em.get_datacenter_paged("https://datacenter-web.eastmoney.com/api/data/v1/get", {"reportName": "X"})
    assert rows == [{"i": 1}, {"i": 2}]
