import polars as pl

from app.services import symbol_search


class Repo:
    def __init__(self, stock=None, index=None, etf=None):
        self.stock = stock if stock is not None else pl.DataFrame()
        self.index = index if index is not None else pl.DataFrame()
        self.etf = etf if etf is not None else pl.DataFrame()

    def get_instruments(self):
        return self.stock

    def get_index_instruments(self):
        return self.index

    def get_etf_instruments(self):
        return self.etf


def test_local_code_exact_ranks_first(monkeypatch):
    monkeypatch.setattr(symbol_search, "suggest_symbols", lambda q, limit: [])
    repo = Repo(stock=pl.DataFrame([
        {"symbol": "600519.SH", "code": "600519", "name": "贵州茅台"},
        {"symbol": "600500.SH", "code": "600500", "name": "中化"},
    ]))

    rows = symbol_search.search_symbols(repo, "600519", 10)

    assert rows[0]["symbol"] == "600519.SH"
    assert rows[0]["source"] == "local"


def test_name_match_and_suggest_only_when_needed(monkeypatch):
    calls = []
    monkeypatch.setattr(symbol_search, "suggest_symbols", lambda q, limit: calls.append((q, limit)) or [{"symbol": "00700.HK", "code": "00700", "name": "腾讯", "asset_type": "hk", "source": "eastmoney_suggest", "matched_by": "suggest"}])
    repo = Repo(stock=pl.DataFrame([{"symbol": "600519.SH", "code": "600519", "name": "贵州茅台"}]))

    rows = symbol_search.search_symbols(repo, "茅台", 2)

    assert [r["symbol"] for r in rows] == ["600519.SH", "00700.HK"]
    assert calls == [("茅台", 1)]


def test_search_by_pinyin_full(monkeypatch):
    monkeypatch.setattr(symbol_search, "suggest_symbols", lambda q, limit: [])
    repo = Repo(stock=pl.DataFrame([
        {"symbol": "600519.SH", "code": "600519", "name": "贵州茅台"},
    ]))

    rows = symbol_search.search_symbols(repo, "guizhoumaotai", 10)

    assert rows[0]["symbol"] == "600519.SH"
    assert rows[0]["matched_by"] == "pinyin"


def test_search_by_pinyin_initials(monkeypatch):
    monkeypatch.setattr(symbol_search, "suggest_symbols", lambda q, limit: [])
    repo = Repo(stock=pl.DataFrame([
        {"symbol": "600519.SH", "code": "600519", "name": "贵州茅台"},
    ]))

    rows = symbol_search.search_symbols(repo, "gzmt", 10)

    assert rows[0]["symbol"] == "600519.SH"
    assert rows[0]["matched_by"] == "initials"


def test_fullwidth_name_normalized_for_pinyin(monkeypatch):
    monkeypatch.setattr(symbol_search, "suggest_symbols", lambda q, limit: [])
    repo = Repo(stock=pl.DataFrame([
        {"symbol": "000002.SZ", "code": "000002", "name": "万 科Ａ"},
    ]))

    by_full = symbol_search.search_symbols(repo, "wanke", 10)
    by_initial = symbol_search.search_symbols(repo, "wk", 10)

    assert by_full[0]["symbol"] == "000002.SZ"
    assert by_full[0]["matched_by"] == "pinyin"
    assert by_initial[0]["symbol"] == "000002.SZ"
    assert by_initial[0]["matched_by"] == "initials"


def test_suggest_normalizes_markets(monkeypatch):
    monkeypatch.setattr(symbol_search.eastmoney_client, "get_json", lambda *args, **kwargs: {"QuotationCodeTable": {"Data": [{"Code": "159915", "Name": "创业板ETF"}, {"Code": "00700", "Name": "腾讯控股"}, {"Code": "12", "Name": "bad"}]}})

    rows = symbol_search.suggest_symbols("x")

    assert rows[0]["symbol"] == "159915.SZ"
    assert rows[0]["asset_type"] == "etf"
    assert rows[1]["symbol"] == "00700.HK"
