import json
import threading
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import HTTPException, UploadFile

from app.api import trade_journal
from app.services.trading import fhold_client


class FakeRepo:
    def __init__(self):
        self.symbols = []

    def get_index_daily(self, symbol, start, end, columns=None):
        self.symbols.append(symbol)
        return pl.DataFrame(
            {
                "date": ["2024-02-05", "2024-02-06"],
                "close": [100.0, 101.0],
            }
        )


def request(repo=None):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo or FakeRepo())))


def upload_file(text: str) -> UploadFile:
    return UploadFile(filename="journal.csv", file=BytesIO(text.encode("utf-8")))


CSV = """成交日期,成交时间,代码,名称,交易类别,成交数量,成交价格,发生金额,费用
2024-02-05,14:53:08,601127,赛力斯,买入,200,56.1,-11221.23,1.23
2024-02-06,14:54:53,601127,赛力斯,卖出,200,61.71,12334.48,7.52
"""


FHOLD_TRANSACTIONS = {
    "available": True,
    "accounts": [{"id": 7, "name": "银河证券", "broker": "银河", "isDefault": True}],
    "transactions": [
        {
            "id": 101,
            "account_id": 7,
            "trade_date": "2024-02-05",
            "trade_time": "14:53:08",
            "code": "601127",
            "name": "赛力斯",
            "trade_type": "buy",
            "quantity": 200,
            "price": 56.1,
            "amount": -11221.23,
            "trade_amount": 11220,
            "fee": 1.23,
        },
        {
            "id": 102,
            "account_id": 7,
            "trade_date": "2024-02-06",
            "trade_time": "14:54:53",
            "code": "601127",
            "name": "赛力斯",
            "trade_type": "卖出",
            "quantity": 200,
            "price": 61.71,
            "amount": 12334.48,
            "trade_amount": 12342,
            "fee": 7.52,
        },
    ],
}


def test_get_ledger_returns_normal_empty_state(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_journal.settings, "data_dir", tmp_path)

    assert trade_journal.get_ledger() is None


@pytest.mark.asyncio
async def test_upload_preview_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_journal.settings, "data_dir", tmp_path)
    resp = await trade_journal.upload_journal(request(), file=upload_file(CSV), commit=False)
    assert resp["guessed_mapping"]["成交日期"] == "date"
    assert resp["preview_rows"][0]["代码"] == 601127 or resp["preview_rows"][0]["代码"] == "601127"
    assert trade_journal.store.read_ledger(tmp_path) is None


@pytest.mark.asyncio
async def test_upload_commit_writes_normalized_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_journal.settings, "data_dir", tmp_path)
    resp = await trade_journal.upload_journal(
        request(),
        file=upload_file(CSV),
        commit=True,
        mapping=json.dumps(trade_journal.THS_PRESET["mapping"], ensure_ascii=False),
    )
    assert resp["summary"]["total_trips"] == 1
    assert abs(resp["trips"][0]["total_pnl"] - 1113.25) < 1e-9
    assert resp["warnings"] == ["追涨诊断: 1 只标的无本地日K或历史不足20日未覆盖"]
    assert "交易流水复盘" in resp["methodology_context"]
    stored = trade_journal.store.read_ledger(tmp_path)
    assert stored is not None
    assert "methodology_context" not in stored
    ledger = trade_journal.get_ledger()
    assert ledger["summary"]["total_trips"] == 1
    assert "交易流水复盘" in ledger["methodology_context"]
    assert trade_journal.delete_ledger() == {"deleted": True}


@pytest.mark.asyncio
async def test_upload_commit_skill_context_failure_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_journal.settings, "data_dir", tmp_path)

    def fail_safe_loader(scenario, max_chars=12_000, warnings=None):
        if warnings is not None:
            warnings.append(f"方法论库加载失败: {scenario}")
        return ""

    monkeypatch.setattr("app.services.skill_context.load_skill_context_safe", fail_safe_loader)
    resp = await trade_journal.upload_journal(
        request(),
        file=upload_file(CSV),
        commit=True,
        mapping=json.dumps(trade_journal.THS_PRESET["mapping"], ensure_ascii=False),
    )

    assert "methodology_context" not in resp
    assert "方法论库加载失败: trade_journal" in resp["warnings"]


@pytest.mark.asyncio
async def test_upload_append_deduplicates_same_account(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_journal.settings, "data_dir", tmp_path)
    kwargs = {
        "commit": True,
        "append": True,
        "account_id": "银河",
        "mapping": json.dumps(trade_journal.THS_PRESET["mapping"], ensure_ascii=False),
    }

    first = await trade_journal.upload_journal(request(), file=upload_file(CSV), **kwargs)
    second = await trade_journal.upload_journal(request(), file=upload_file(CSV), **kwargs)

    assert first["summary"]["total_trips"] == 1
    assert second["summary"]["total_trips"] == 1
    assert second["import"]["deduped_fills"] == 2
    assert len(trade_journal.store.read_source(tmp_path)["fills"]) == 2


@pytest.mark.asyncio
async def test_upload_append_keeps_accounts_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_journal.settings, "data_dir", tmp_path)
    mapping = json.dumps(trade_journal.THS_PRESET["mapping"], ensure_ascii=False)
    await trade_journal.upload_journal(
        request(),
        file=upload_file(CSV),
        commit=True,
        append=True,
        account_id="A",
        mapping=mapping,
    )

    resp = await trade_journal.upload_journal(
        request(),
        file=upload_file(CSV),
        commit=True,
        append=True,
        account_id="B",
        mapping=mapping,
    )

    assert resp["summary"]["total_trips"] == 2
    assert {trip["account_id"] for trip in resp["trips"]} == {"A", "B"}
    assert {row["account_id"] for row in resp["benchmark"]["per_trip"]} == {"A", "B"}


@pytest.mark.asyncio
async def test_upload_narrative_uses_aggregate_only(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_journal.settings, "data_dir", tmp_path)
    resp = await trade_journal.upload_journal(
        request(),
        file=upload_file(CSV),
        commit=True,
        narrative=True,
        mapping=json.dumps(trade_journal.THS_PRESET["mapping"], ensure_ascii=False),
    )

    assert "narrative" in resp
    assert "1 个完成回合" in resp["narrative"]
    assert "14:53:08" not in resp["narrative"]


def test_normalize_benchmark_accepts_legacy_suffix():
    assert trade_journal._normalize_benchmark("399006.SZ") == "399006.INDEX"


@pytest.mark.asyncio
async def test_upload_commit_falls_back_unknown_benchmark(tmp_path, monkeypatch):
    repo = FakeRepo()
    monkeypatch.setattr(trade_journal.settings, "data_dir", tmp_path)
    resp = await trade_journal.upload_journal(
        request(repo),
        file=upload_file(CSV),
        commit=True,
        benchmark="BAD.INDEX",
        mapping=json.dumps(trade_journal.THS_PRESET["mapping"], ensure_ascii=False),
    )
    assert resp["benchmark"]["code"] == "000300.INDEX"
    assert repo.symbols == ["000300.INDEX"]


def test_feedback_records_value_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_journal.settings, "data_dir", tmp_path)
    trade_journal.store.write_ledger(tmp_path, {"imported_at": "2026-07-03T00:00:00Z"})

    resp = trade_journal.save_feedback({"rating": "helpful"})

    assert resp == {"ok": True}
    assert trade_journal.store.read_feedback(tmp_path)[0]["rating"] == "helpful"
    assert (
        trade_journal.store.read_feedback(tmp_path)[0]["ledger_imported_at"]
        == "2026-07-03T00:00:00Z"
    )


def test_fhold_preview_is_read_only_and_normalizes_cash_sign(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_journal.settings, "data_dir", tmp_path)
    monkeypatch.setattr(fhold_client, "fetch_transactions", lambda: deepcopy(FHOLD_TRANSACTIONS))

    preview = trade_journal.preview_fhold_journal()

    assert preview["available"] is True
    assert preview["row_count"] == 2
    assert preview["importable_count"] == 2
    assert preview["accounts"] == [{"id": "fhold:7", "name": "银河证券", "fills": 2}]
    assert preview["preview_rows"][0]["amount"] == -11221.23
    assert preview["preview_rows"][1]["amount"] == 12334.48
    assert trade_journal.store.read_source(tmp_path) is None
    assert trade_journal.store.read_ledger(tmp_path) is None


def test_fhold_preview_skips_out_of_range_numeric_values(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_journal.settings, "data_dir", tmp_path)
    state = deepcopy(FHOLD_TRANSACTIONS)
    state["transactions"][0]["price"] = 1e101
    monkeypatch.setattr(fhold_client, "fetch_transactions", lambda: state)

    preview = trade_journal.preview_fhold_journal()

    assert preview["available"] is True
    assert preview["row_count"] == 2
    assert preview["importable_count"] == 1
    assert preview["skipped_count"] == 1
    assert any("成交数量或价格无效" in warning for warning in preview["warnings"])


def test_fhold_numeric_parser_rejects_booleans():
    assert trade_journal._finite_float(True) is None
    assert trade_journal._finite_float(False) is None


def test_concurrent_journal_appends_preserve_both_fills(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_journal.settings, "data_dir", tmp_path)
    first_fill, _ = trade_journal._fhold_transaction_to_fill(FHOLD_TRANSACTIONS["transactions"][0])
    second_fill, _ = trade_journal._fhold_transaction_to_fill(FHOLD_TRANSACTIONS["transactions"][1])
    assert first_fill is not None and second_fill is not None
    barrier = threading.Barrier(2)
    errors = []

    def append_fill(fill):
        try:
            barrier.wait(timeout=1)
            trade_journal._commit_journal(
                request(),
                [fill],
                [],
                append=True,
                import_meta={"source": "test", "account_id": fill.account_id},
                benchmark="000300.INDEX",
                narrative=False,
                warnings=[],
            )
        except BaseException as exc:  # pragma: no cover - asserted below from the caller thread
            errors.append(exc)

    first = threading.Thread(target=append_fill, args=(first_fill,))
    second = threading.Thread(target=append_fill, args=(second_fill,))
    first.start()
    second.start()
    first.join(timeout=3)
    second.join(timeout=3)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    source = trade_journal.store.read_source(tmp_path)
    assert {fill["source_ref"] for fill in source["fills"]} == {
        "fhold:transaction:101",
        "fhold:transaction:102",
    }


def test_fhold_import_appends_idempotently_without_trading_events(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_journal.settings, "data_dir", tmp_path)
    monkeypatch.setattr(fhold_client, "fetch_transactions", lambda: deepcopy(FHOLD_TRANSACTIONS))
    preview = trade_journal.preview_fhold_journal()
    payload = trade_journal.FholdJournalImportRequest(snapshot_sha256=preview["snapshot_sha256"])

    first = trade_journal.import_fhold_journal(request(), payload)
    second = trade_journal.import_fhold_journal(request(), payload)

    assert first["summary"]["total_trips"] == 1
    assert first["import"]["source"] == "fhold"
    assert first["import"]["mode"] == "append"
    assert second["import"]["deduped_fills"] == 2
    source = trade_journal.store.read_source(tmp_path)
    assert len(source["fills"]) == 2
    assert {fill["source_ref"] for fill in source["fills"]} == {
        "fhold:transaction:101",
        "fhold:transaction:102",
    }
    assert list(tmp_path.rglob("trade_events.jsonl")) == []


def test_fhold_import_rejects_changed_snapshot_before_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_journal.settings, "data_dir", tmp_path)
    state = deepcopy(FHOLD_TRANSACTIONS)
    monkeypatch.setattr(fhold_client, "fetch_transactions", lambda: deepcopy(state))
    preview = trade_journal.preview_fhold_journal()
    state["transactions"][0]["price"] = 56.2

    with pytest.raises(HTTPException) as exc_info:
        trade_journal.import_fhold_journal(
            request(),
            trade_journal.FholdJournalImportRequest(snapshot_sha256=preview["snapshot_sha256"]),
        )

    assert exc_info.value.status_code == 409
    assert trade_journal.store.read_source(tmp_path) is None


def test_fhold_import_keeps_existing_source_reference_on_correction(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_journal.settings, "data_dir", tmp_path)
    state = deepcopy(FHOLD_TRANSACTIONS)
    monkeypatch.setattr(fhold_client, "fetch_transactions", lambda: deepcopy(state))
    initial = trade_journal.preview_fhold_journal()
    trade_journal.import_fhold_journal(
        request(),
        trade_journal.FholdJournalImportRequest(snapshot_sha256=initial["snapshot_sha256"]),
    )
    state["transactions"][0]["amount"] = -11300.0
    changed = trade_journal.preview_fhold_journal()

    result = trade_journal.import_fhold_journal(
        request(),
        trade_journal.FholdJournalImportRequest(snapshot_sha256=changed["snapshot_sha256"]),
    )

    source = trade_journal.store.read_source(tmp_path)
    assert result["import"]["conflicting_fills"] == 1
    assert len(source["fills"]) == 2
    assert source["fills"][0]["amount"] == -11221.23
    assert any("发生变更" in warning for warning in result["warnings"])


FHOLD_XLSX_REALITY = {
    "available": True,
    "accounts": [{"id": 9, "name": "测试账户", "broker": "银河", "isDefault": True}],
    "transactions": [
        {
            "id": 201,
            "account_id": 9,
            # 券商 xlsx 导入后 SQLite 原样保留 "YYYY-MM-DD HH:MM:SS" 完整串
            "trade_date": "2026-08-14 15:32:05",
            "trade_time": "15:32:05",
            "code": "600519",
            "name": "贵州茅台",
            "trade_type": "买入",
            "quantity": 100,
            "price": 1292.43,
            "amount": -129243.0,
            "trade_amount": 129243.0,
            "fee": 5.17,
        },
        {
            "id": 202,
            "account_id": 9,
            "trade_date": "2026-08-14 09:41:12",
            "trade_time": "09:41:12",
            # 5 开头沪市 ETF: 前缀规则无法判定市场, 必须经本地 ETF universe 解析
            "code": "510300",
            "name": "沪深300ETF",
            "trade_type": "卖出",
            "quantity": 10000,
            "price": 3.921,
            "amount": 39210.0,
            "trade_amount": 39210.0,
            "fee": 1.96,
        },
        {
            "id": 203,
            "account_id": 9,
            "trade_date": "2026-08-14 10:05:00",
            "trade_time": "10:05:00",
            # 融券购回是保证金语义, 不是普通买卖, 必须排除
            "code": "600519",
            "name": "贵州茅台",
            "trade_type": "融券购回",
            "quantity": 100,
            "price": 1290.0,
            "amount": -129000.0,
            "trade_amount": 129000.0,
            "fee": 5.0,
        },
        {
            "id": 204,
            "account_id": 9,
            "trade_date": "2026-08-14 10:10:00",
            "trade_time": "10:10:00",
            # 11x 可转债不在股票/ETF universe, 正确跳过
            "code": "113050",
            "name": "南银转债",
            "trade_type": "买入",
            "quantity": 10,
            "price": 130.5,
            "amount": -1305.0,
            "trade_amount": 1305.0,
            "fee": 0.5,
        },
    ],
}


def test_fhold_preview_accepts_datetime_dates_and_etf_codes(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_journal.settings, "data_dir", tmp_path)
    monkeypatch.setattr(fhold_client, "fetch_transactions", lambda: deepcopy(FHOLD_XLSX_REALITY))
    monkeypatch.setattr(
        fhold_client,
        "_etf_code_map",
        lambda: {"510300": "510300.SH", "159915": "159915.SZ"},
    )

    preview = trade_journal.preview_fhold_journal()

    assert preview["row_count"] == 4
    assert preview["importable_count"] == 2
    assert preview["skipped_count"] == 2
    reasons = {w for w in preview["warnings"]}
    assert any("买卖方向不支持" in w for w in reasons)
    assert any("证券代码无法映射" in w for w in reasons)
    fills = {row["symbol"]: row for row in preview["preview_rows"]}
    assert "600519.SH" in fills
    assert "510300.SH" in fills
    assert all(fill["date"] == "2026-08-14" for fill in fills.values())
    assert trade_journal.store.read_source(tmp_path) is None


def test_fhold_side_check_reports_semantic_exclusion_first():
    # 融券/转账等公司行为流水即使代码可映射, 也应报告方向不支持而非代码无法映射
    fill, reason = trade_journal._fhold_transaction_to_fill(
        FHOLD_XLSX_REALITY["transactions"][2]
    )
    assert fill is None
    assert reason == "买卖方向不支持"
