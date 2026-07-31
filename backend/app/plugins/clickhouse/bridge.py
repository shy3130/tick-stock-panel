"""通过 ClickHouse HTTP API 执行只读查询。"""

from __future__ import annotations

import json
import os
from urllib import error, parse, request


def _url() -> str:
    return os.getenv("CLICKHOUSE_URL", "").strip().rstrip("/")


def _database() -> str:
    value = os.getenv("CLICKHOUSE_DATABASE", "longbridge").strip()
    if not value.replace("_", "a").isalnum() or value[:1].isdigit():
        raise ValueError("CLICKHOUSE_DATABASE 不是合法标识符")
    return value


def database_identifier() -> str:
    """返回经过标识符校验的数据库名称。"""

    return _database()


def query_json_each_row(
    sql: str,
    *,
    timeout_seconds: float | None = None,
) -> list[dict]:
    """执行 SQL 并返回 JSONEachRow。异常信息不包含凭证。"""

    endpoint = _url()
    if not endpoint:
        raise RuntimeError("未配置 CLICKHOUSE_URL")
    configured_timeout = float(os.getenv("CLICKHOUSE_READ_TIMEOUT_SECONDS", "30"))
    timeout = (
        configured_timeout
        if timeout_seconds is None
        else max(0.001, min(configured_timeout, timeout_seconds))
    )
    database = parse.quote(_database(), safe="")
    body = f"{sql.rstrip().rstrip(';')} FORMAT JSONEachRow".encode()
    req = request.Request(f"{endpoint}/?database={database}", data=body, method="POST")
    user = os.getenv("CLICKHOUSE_USER", "").strip()
    password = os.getenv("CLICKHOUSE_PASSWORD", "")
    if user:
        req.add_header("X-ClickHouse-User", user)
    if password:
        req.add_header("X-ClickHouse-Key", password)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            text = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"ClickHouse 查询失败: HTTP {exc.code}: {detail}") from exc
    except (error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"ClickHouse 连接失败: {type(exc).__name__}") from exc
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def availability() -> tuple[bool, str]:
    if not _url():
        return False, "未配置 CLICKHOUSE_URL"
    try:
        rows = query_json_each_row("SELECT 1 AS ok")
    except Exception as exc:
        return False, str(exc)
    return (bool(rows and rows[0].get("ok") == 1), "ok")
