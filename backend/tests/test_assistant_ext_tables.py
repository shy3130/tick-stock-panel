"""AI 助手扩展数据表问答工具: list_ext_tables / query_ext_table 往返。

覆盖:
- list_ext_tables 列出配置与数据新鲜度(时序表最新分区日期), 空目录给提示。
- query_ext_table 读行: 默认最新分区 / 等值过滤 / 排序 / limit 封顶 / 日期范围。
- 错误契约: 未知表、非法过滤字段、非法日期均按 ok=False 回填, 不抛异常。
- QUICK_SUGGESTS 全量携带 group 字段(空会话分组卡片依赖)。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from app.custom.assistant import tools as assistant_tools
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, rows_to_parquet


def _make_timeseries_table(data_dir: Path, days: list[str]) -> None:
    cfg = ExtConfig(
        id="demo_pool",
        label="演示池",
        mode="timeseries",
        fields=[
            ExtField("symbol", "string", "代码"),
            ExtField("score", "float", "评分"),
        ],
        description="测试用扩展表",
    )
    ExtConfigStore(data_dir).upsert(cfg)
    for day in days:
        rows_to_parquet(
            [
                {"symbol": "600519.SH", "score": 88.5},
                {"symbol": "000001.SZ", "score": 72.0},
            ],
            cfg,
            data_dir,
            snapshot_date=date.fromisoformat(day),
        )


def _make_snapshot_table(data_dir: Path) -> None:
    cfg = ExtConfig(
        id="snap_tags",
        label="标签快照",
        mode="snapshot",
        fields=[
            ExtField("symbol", "string", "代码"),
            ExtField("tag", "string", "标签"),
        ],
    )
    ExtConfigStore(data_dir).upsert(cfg)
    rows_to_parquet(
        [{"symbol": "300750.SZ", "tag": "电池"}],
        cfg,
        data_dir,
        snapshot_date=date(2026, 9, 26),
    )


def test_list_ext_tables_roundtrip(tmp_path: Path) -> None:
    _make_timeseries_table(tmp_path, ["2026-09-24", "2026-09-25"])
    ctx = assistant_tools.ToolContext.build(data_dir=tmp_path)

    result = assistant_tools._list_ext_tables({}, ctx)

    assert result["count"] == 1
    table = result["tables"][0]
    assert table["id"] == "demo_pool" and table["mode"] == "timeseries"
    assert table["has_data"] is True
    assert table["latest_date"] == "2026-09-25"
    assert table["earliest_date"] == "2026-09-24"
    assert {f["name"] for f in table["fields"]} == {"symbol", "score"}


def test_list_ext_tables_empty_gives_note(tmp_path: Path) -> None:
    ctx = assistant_tools.ToolContext.build(data_dir=tmp_path)
    result = assistant_tools._list_ext_tables({}, ctx)
    assert result["tables"] == [] and "还没有配置" in result["note"]


async def test_query_ext_table_rows_filter_sort_range(tmp_path: Path) -> None:
    _make_timeseries_table(tmp_path, ["2026-09-24", "2026-09-25"])
    ctx = assistant_tools.ToolContext.build(data_dir=tmp_path)

    # 默认: 最新分区
    payload = await assistant_tools.execute_assistant_tool(
        "query_ext_table", {"table": "demo_pool"}, ctx,
    )
    assert payload["ok"] is True
    result = payload["result"]
    assert result["date"] == "2026-09-25" and result["total"] == 2
    assert {row["symbol"] for row in result["rows"]} == {"600519.SH", "000001.SZ"}

    # 等值过滤 + 降序排序 + limit
    payload = await assistant_tools.execute_assistant_tool(
        "query_ext_table",
        {"table": "demo_pool", "filter": "symbol:000001.SZ", "sort": "score:desc", "limit": 1},
        ctx,
    )
    assert payload["ok"] is True
    assert [row["symbol"] for row in payload["result"]["rows"]] == ["000001.SZ"]

    # 日期范围: 两个分区合并, 行级 date 列补齐
    payload = await assistant_tools.execute_assistant_tool(
        "query_ext_table",
        {"table": "demo_pool", "start_date": "2026-09-24", "end_date": "2026-09-25"},
        ctx,
    )
    assert payload["ok"] is True
    result = payload["result"]
    assert result["date"] == "2026-09-24..2026-09-25" and result["total"] == 4
    assert "date" in result["columns"]


async def test_query_ext_table_snapshot_mode(tmp_path: Path) -> None:
    _make_snapshot_table(tmp_path)
    ctx = assistant_tools.ToolContext.build(data_dir=tmp_path)

    payload = await assistant_tools.execute_assistant_tool(
        "query_ext_table", {"table": "snap_tags"}, ctx,
    )
    assert payload["ok"] is True
    assert payload["result"]["rows"][0]["tag"] == "电池"

    # 快照表不支持日期范围 → 契约回错
    bad = await assistant_tools.execute_assistant_tool(
        "query_ext_table", {"table": "snap_tags", "start_date": "2026-09-01"}, ctx,
    )
    assert bad["ok"] is False and "快照" in bad["error"]


async def test_query_ext_table_error_contract(tmp_path: Path) -> None:
    _make_timeseries_table(tmp_path, ["2026-09-25"])
    ctx = assistant_tools.ToolContext.build(data_dir=tmp_path)

    missing = await assistant_tools.execute_assistant_tool(
        "query_ext_table", {"table": "nope"}, ctx,
    )
    assert missing["ok"] is False and "不存在" in missing["error"]

    no_table = await assistant_tools.execute_assistant_tool("query_ext_table", {}, ctx)
    assert no_table["ok"] is False and "table" in no_table["error"]

    bad_filter = await assistant_tools.execute_assistant_tool(
        "query_ext_table", {"table": "demo_pool", "filter": "nope:1"}, ctx,
    )
    assert bad_filter["ok"] is False and "不存在" in bad_filter["error"]

    bad_date = await assistant_tools.execute_assistant_tool(
        "query_ext_table", {"table": "demo_pool", "date": "not-a-date"}, ctx,
    )
    assert bad_date["ok"] is False and "日期格式错误" in bad_date["error"]


def test_summarize_ext_table_results() -> None:
    assert "扩展表" in assistant_tools.summarize_tool_result(
        "list_ext_tables", {"ok": True, "result": {"tables": [{}, {}], "count": 2}},
    )
    assert "暂无" in assistant_tools.summarize_tool_result(
        "list_ext_tables", {"ok": True, "result": {"tables": [], "count": 0}},
    )
    assert "2/4" in assistant_tools.summarize_tool_result(
        "query_ext_table",
        {"ok": True, "result": {"label": "演示池", "returned": 2, "total": 4}},
    )


def test_quick_suggests_grouped() -> None:
    for item in assistant_tools.QUICK_SUGGESTS:
        assert {"id", "label", "prompt", "group"} <= set(item)
    ids = {item["id"] for item in assistant_tools.QUICK_SUGGESTS}
    assert {"ext-tables", "data-coverage", "market-overview"} <= ids
    # 对比类建议: 策略横向对比 + 个股横向对比
    assert {"strategy-compare", "stock-compare"} <= ids
    groups = {item["group"] for item in assistant_tools.QUICK_SUGGESTS}
    assert len(groups) >= 4  # 行情与大盘 / 我的与个股 / 策略与信号 / 数据与扩展
    assert len(ids) == len(assistant_tools.QUICK_SUGGESTS)  # id 无重复
