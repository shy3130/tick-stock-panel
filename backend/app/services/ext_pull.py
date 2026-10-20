"""扩展数据定时拉取引擎 — 从外部 API 拉取数据写入 Parquet。"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta, timezone
from functools import reduce
from pathlib import Path
from typing import Any

import httpx

from app.market_time import CN_TZ, cn_now, cn_today
from app.services.ext_data import (
    ExtConfig,
    ExtConfigStore,
    PullConfig,
    rows_to_parquet,
)

logger = logging.getLogger(__name__)

_pull_client: ContextVar[httpx.AsyncClient | None] = ContextVar("ext_pull_client", default=None)


@asynccontextmanager
async def _pull_client_scope():
    client = _pull_client.get()
    if client is not None:
        yield client
        return
    async with httpx.AsyncClient() as client:
        token = _pull_client.set(client)
        try:
            yield client
        finally:
            _pull_client.reset(token)


def outbound_headers(user_headers: dict[str, str] | None = None) -> dict[str, str]:
    """扩展数据出站请求的默认标识头。

    默认携带 User-Agent: tsp/<版本> 与 X-TSP-Client: tick-stock-panel,
    供服务端 (如 tickflow-hub) 识别本项目的请求。用户在拉取配置里显式
    设置的同名头优先 (大小写不敏感), 不被标识头覆盖。
    """
    from app import __version__

    defaults = {
        "User-Agent": f"tsp/{__version__}",
        "X-TSP-Client": "tick-stock-panel",
    }
    override = {k.lower() for k in (user_headers or {})}
    return {
        **{k: v for k, v in defaults.items() if k.lower() not in override},
        **(user_headers or {}),
    }


def _in_time_window(start: str | None, end: str | None) -> bool:
    """检查当前北京时间是否在每日时间窗口内。

    start/end 为 "HH:MM" 格式。两者都为 None 时不限制(返回 True)。
    支持跨午夜窗口(如 22:00-02:00)。

    用北京时间而不是本地时间: 这个窗口是照着 A 股交易时段设的, 而
    market_time 模块开篇就写明「服务器/容器本地时区不可靠 (python:slim
    镜像默认 UTC)」。UTC 容器里 9:30-15:00 的窗口实际落在北京 17:30-23:00,
    每天都在收盘之后。
    """
    if not start or not end:
        return True
    now = cn_now().strftime("%H:%M")
    if start <= end:
        return start <= now < end
    # 跨午夜: 如 22:00-02:00
    return now >= start or now < end


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------

def _extract_rows(data: Any, path: str) -> list[dict]:
    """按 dot-path 从 JSON 响应中提取行数组。

    例: path="data.list" → response["data"]["list"]
    如果 path 为空，直接将 data 视为数组。
    """
    if not path:
        if isinstance(data, list):
            return data
        raise ValueError("response_path 为空但响应不是数组")

    keys = path.split(".")
    current = data
    for key in keys:
        if isinstance(current, dict):
            if key not in current:
                raise ValueError(f"响应中不存在路径 '{path}'，缺失键 '{key}'")
            current = current[key]
        elif isinstance(current, list):
            try:
                current = current[int(key)]
            except (ValueError, IndexError) as e:
                raise ValueError(f"响应路径 '{path}' 解析失败: {e}") from e
        else:
            raise ValueError(f"响应路径 '{path}' 中间值不是 dict/list: {type(current)}")

    if not isinstance(current, list):
        raise ValueError(f"路径 '{path}' 指向的不是数组，而是 {type(current)}")

    return current


def _apply_field_map(rows: list[dict], field_map: dict[str, str]) -> list[dict]:
    """将外部字段名映射为内部配置字段名。field_map: {外部名: 内部名}。"""
    if not field_map:
        return rows
    mapped = []
    for row in rows:
        new_row: dict = {}
        for k, v in row.items():
            mapped_key = field_map.get(k, k)
            new_row[mapped_key] = v
        mapped.append(new_row)
    return mapped


# ---------------------------------------------------------------------------
# 拉取执行
# ---------------------------------------------------------------------------

def _format_date_value(day: date, date_format: str) -> str:
    """按配置格式把交易日序列化为接口参数值。

    ts_s/ts_ms = 该交易日北京时间 00:00:00 的时间戳 (约定, 见 PULL_DATE_FORMATS);
    未知格式回退 iso (PullConfig 构造时已归一, 这里再兜底)。
    """
    if date_format == "compact":
        return day.strftime("%Y%m%d")
    if date_format == "ts_s":
        return str(int(datetime(day.year, day.month, day.day, tzinfo=CN_TZ).timestamp()))
    if date_format == "ts_ms":
        return str(int(datetime(day.year, day.month, day.day, tzinfo=CN_TZ).timestamp() * 1000))
    return day.isoformat()


def _with_date_param(url: str, date_param: str | None, day: date, date_format: str = "iso") -> str:
    """接口按日查询参数: ?{date_param}={按格式序列化的日期} (已有 query 用 &)。"""
    if not date_param:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{date_param}={_format_date_value(day, date_format)}"


# Hub 管理后台的会话 Cookie 名。它是有有效期的浏览器会话(默认 24h),
# 换机器/换部署即失效, 不能当作定时拉取的长期凭据。
_HUB_SESSION_COOKIES = frozenset({"tickflow_hub_access"})


def _hub_session_cookie_name(header: str, value: str) -> str | None:
    """返回值里是否携带 Hub 管理后台会话 Cookie; 是则返回 Cookie 名。"""
    if str(header or "").strip().lower() != "cookie":
        return None
    for item in str(value or "").split(";"):
        name = item.partition("=")[0].strip().lower()
        if name in _HUB_SESSION_COOKIES:
            return name
    return None


def session_cookie_rejection(auth: dict | None, key: str = "") -> str | None:
    """拉取鉴权是否为「管理后台会话」; 是则返回面向用户的拒绝原因。

    服务间定时同步必须是长期凭据。管理后台会话最长 24 小时, 过期后所有拉取
    都会 401; 换机器或重新部署也无从复用。保存配置、保存 Key 与实际发请求
    三处都调用本函数, 让这条纪律在 UI 与后台都无法绕过。

    key 为空时无法判断 (Key 可能稍后再填), 返回 None。
    """
    if not key:
        return None
    header = str((auth or {}).get("header") or "Authorization")
    session_cookie = _hub_session_cookie_name(header, key)
    if not session_cookie:
        return None
    return (
        f"拉取鉴权不能使用管理后台会话 Cookie({session_cookie}): 它有有效期, "
        "过期后定时拉取会全部失败, 换机器或重新部署也无法沿用。"
        "请改用服务方签发的 API Key(如 X-API-Key / Bearer), "
        "并在服务方后台把对应数据接口开放给该 Key。"
    )


def _apply_auth(config_id: str, auth: dict | None, url: str, headers: dict[str, str]) -> str:
    """把 secrets_store 里的 API Key 注入出站请求。

    鉴权三型与自定义行情源 AuthConfig 同口径: bearer → {header: "Bearer <key>"},
    header → {header: <key>}, query → ?{param}=<key>。Key 只存 secrets.json,
    不落 config.json; 配置了鉴权但未设置 Key 时 fail-closed 直接报错,
    避免不带凭据请求被服务端记成无效调用。返回 (可能追加了参数的) url。

    auth.key_ref: 复用已有凭据而非本表专属 Key。值为 secrets.json 的字段名
    (如 "fuyao_api_key"), 环境变量兜底取该名的大写形式。同一数据源被行情插件
    与扩展表共用时, 用户只需在一处填 Key。
    """
    from urllib.parse import quote

    from app import secrets_store

    from app.services.ext_data import get_ext_api_key

    auth_type = str((auth or {}).get("type") or "none").lower()
    if auth_type == "none":
        return url
    key_ref = str((auth or {}).get("key_ref") or "").strip()
    if key_ref:
        key = secrets_store.get_env_backed_secret(key_ref, key_ref.upper())
        if not key:
            raise ValueError(
                f"已配置鉴权但未找到凭据 {key_ref} — 请在 设置 → 数据源 配置对应 API Key"
            )
    else:
        key = get_ext_api_key(config_id)
        if not key:
            raise ValueError(f"已配置 {auth_type} 鉴权但未设置 API Key, 请在拉取设置中填写")
    header_name = str((auth or {}).get("header") or "Authorization")
    rejected = session_cookie_rejection(auth, key)
    if rejected:
        raise ValueError(rejected)
    if auth_type == "bearer":
        headers[header_name] = f"Bearer {key}"
    elif auth_type == "header":
        headers[header_name] = key
    elif auth_type == "query":
        name = str(auth.get("param") or "token")
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{name}={quote(key, safe='')}"
    else:
        raise ValueError(f"未知鉴权类型: {auth_type!r} (可选 none/bearer/header/query)")
    return url


def _assert_rows_date(rows: list[dict], day: date) -> None:
    """金融契约: 响应行的 date 字段 (若提供) 必须与请求日期一致。

    服务端忽略日期参数返回当日数据时会静默把当日值写进历史分区,
    造成整个时序口径错乱 —— 此处 fail-closed 拒绝 (实测确实有忽略
    ?date= 的接口)。date 字段缺省的接口不做校验。
    """
    want = day.isoformat()
    for r in rows[:20]:
        if not isinstance(r, dict):
            continue
        raw = r.get("date")
        if raw is None:
            continue
        if str(raw)[:10] != want:
            raise ValueError(
                f"接口返回的日期 {str(raw)[:10]!r} 与请求日期 {want} 不一致 "
                "(接口可能不支持日期参数), 已拒绝写入该分区"
            )


async def _request_json(
    pull: PullConfig, config_id: str, day: date | None = None,
    extra_params: dict[str, str] | None = None,
    *,
    url_override: str | None = None,
) -> Any:
    """发起一次拉取请求并返回解析后的 JSON。

    正式拉取 (带日期参数) 与设置页"测试" (不带) 共用同一实现,
    保证 UA 标识头与 API Key 鉴权注入只有一套口径。
    extra_params: 追加的 query 参数 (分页页码/每页条数), 值经 URL 编码。
    url_override: 直接指定请求地址 (两级拉取的第二级请求用), 不再套用
      date_param 与 pull.url。
    """
    if url_override is not None:
        url = url_override
    else:
        url = _with_date_param(pull.url, pull.date_param, day, pull.date_format) if day else pull.url
    if extra_params:
        url = _append_query_params(url, extra_params)
    # 每配置超时 (PullConfig.timeout_seconds, 默认 30 与历史行为一致);
    # getattr 兜底测试用的简化 pull 对象
    timeout = getattr(pull, "timeout_seconds", None)
    timeout = timeout if isinstance(timeout, (int, float)) and timeout > 0 else 30
    async with _pull_client_scope() as client:
        headers = outbound_headers(pull.headers)
        url = _apply_auth(config_id, pull.auth, url, headers)
        kwargs: dict[str, Any] = {"headers": headers, "timeout": timeout}

        if pull.method.upper() == "POST" and pull.body:
            kwargs["content"] = pull.body
            if "content-type" not in {k.lower() for k in headers}:
                kwargs["headers"]["Content-Type"] = "application/json"

        resp = await client.request(pull.method.upper(), url, **kwargs)
        status_code = getattr(resp, "status_code", None)
        if status_code in (401, 403):
            # 鉴权失败几乎都是凭据类型/权限不对, 给出可执行提示而不是裸 HTTP 错误。
            detail = (getattr(resp, "text", "") or "").strip()[:160]
            raise ValueError(
                f"鉴权被拒绝 (HTTP {status_code}): {detail or '无响应体'} — "
                "请确认使用服务方签发的 API Key, 且该 Key 已被授权访问此接口"
            )
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception as e:
            raise ValueError(f"响应不是有效 JSON: {e}") from e


def _append_query_params(url: str, params: dict[str, str]) -> str:
    """把追加参数并入 URL query (值经编码, 保留既有参数与 fragment)。"""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    merged = dict(parse_qsl(parts.query, keep_blank_values=True))
    merged.update({str(k): str(v) for k, v in params.items()})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(merged), parts.fragment))


def _parse_rows_payload(config: ExtConfig, pull: PullConfig, data: Any) -> list[dict]:
    """单次响应 → 行列表 (提取/字段映射)。与写入链共用, 不含日期校验。

    两级拉取 (pull.fanout) 不走这里 —— 它的行由多个响应聚合而来, 见
    fetch_rows_for_date 的分支。
    """
    rows = _extract_rows(data, pull.response_path)
    # 字段映射
    return _apply_field_map(rows, pull.field_map)


async def fetch_rows_for_date(config: ExtConfig, target_date: date) -> list[dict]:
    async with _pull_client_scope():
        return await _fetch_rows_for_date(config, target_date)


async def _fetch_rows_for_date(config: ExtConfig, target_date: date) -> list[dict]:
    """按日期请求外部 API 并解析为行 (不写盘)。空数据返回 []。

    与 fetch_and_ingest 共用同一解析链 (response_path/字段映射/关联字段校验),
    历史回补与当日拉取不产生第二套口径。
    配置了 pull.page_param 时按页循环拉取 (仅 GET), 空页/短页/达 max_pages 停。
    配置了 pull.fanout 时走两级拉取 (目录 → 逐条目明细), 见 ext_fanout。
    """
    pull = config.pull
    if not pull or not pull.url:
        raise ValueError("拉取未配置或 URL 为空")

    if pull.fanout:
        from app.services.ext_fanout import fetch_fanout_rows

        rows = await fetch_fanout_rows(config, target_date)
    elif pull.page_param and pull.method.upper() == "GET":
        collected: list[dict] = []
        page = pull.page_start
        for _ in range(pull.max_pages):
            extra: dict[str, str] = {pull.page_param: str(page)}
            if pull.page_size_param and pull.page_size > 0:
                extra[pull.page_size_param] = str(pull.page_size)
            data = await _request_json(pull, config.id, day=target_date, extra_params=extra)
            page_rows = _parse_rows_payload(config, pull, data)
            if not page_rows:
                break
            collected.extend(page_rows)
            # 短页 (不足一页) 说明已是最后一页, 免去再一次空请求
            if pull.page_size > 0 and len(page_rows) < pull.page_size:
                break
            page += 1
        else:
            logger.warning(
                "ext pull %s: 分页拉取达到 max_pages=%d 上限, 结果可能不完整",
                config.id, pull.max_pages,
            )
        rows = collected
    else:
        data = await _request_json(pull, config.id, day=target_date)
        rows = _parse_rows_payload(config, pull, data)

    # 校验可关联标的的字段：直接 symbol/code，或配置里声明的映射源列。
    # 市场级表 (market_level, 行=全市场一条, 无标的列) 跳过该校验。
    if config.market_level:
        _assert_rows_date(rows, target_date)
        return rows
    row_keys = set(rows[0]) if rows else set()
    mapped_cols = {
        m.get("col")
        for m in (config.symbol_map or {}, config.code_map or {})
        if m.get("type") == "mapped" and m.get("col")
    }
    if rows and not ({"symbol", "code"} & row_keys or mapped_cols & row_keys):
        raise ValueError("数据行中缺少 symbol/code 字段，请配置字段映射或标的映射")

    _assert_rows_date(rows, target_date)
    return rows


async def fetch_and_ingest(
    config: ExtConfig,
    data_dir,
    target_date: date | None = None,
    *,
    keep_strategy_cache: bool = False,
) -> tuple[int, str]:
    """执行一次拉取: 请求外部 API → 解析响应 → 写入 Parquet。

    target_date 默认当日; 历史回补传入目标日期 (写入对应分区)。
    keep_strategy_cache=True 由定时拉取循环传入: 例行刷新不清策略结果缓存。
    Returns:
        (rows_written, date_str)
    """
    # 同上: 落盘分区按北京日期, 否则 UTC 容器在北京时间 08:00 之前写的是前一天。
    day = target_date or cn_today()
    rows = await fetch_rows_for_date(config, day)
    if not rows:
        raise ValueError("提取到的行数为 0")
    n = rows_to_parquet(
        rows, config, data_dir, snapshot_date=day,
        keep_strategy_cache=keep_strategy_cache,
    )
    return n, day.isoformat()


MAX_BACKFILL_DAYS = 120  # 单次回补上限: 同步端点, 控制请求时长
_BACKFILL_DAY_INTERVAL_S = 0.3   # 相邻请求间隔 (对数据源限速)
_BACKFILL_429_WAIT_S = 30.0      # 429 限流退避时长 (服务端按分钟配额)
_BACKFILL_MAX_CONSECUTIVE_429 = 3  # 连续 429 天数达到阈值 → 中止本次回补
_RATE_LIMIT_ABORT_REASON = "限流中止 (429), 稍后重跑回补可自动续补剩余日期"


def _status_code(e: BaseException) -> int | None:
    """从 httpx.HTTPStatusError 提取状态码; 非该类异常返回 None。"""
    resp = getattr(e, "response", None)
    return getattr(resp, "status_code", None)


def _day_partition(data_dir, config_id: str, day: date) -> Path:
    return Path(data_dir) / "ext_data" / config_id / "timeseries" / f"date={day.isoformat()}" / "part.parquet"


async def backfill_history(
    config: ExtConfig,
    data_dir,
    start: date,
    end: date,
) -> dict:
    async with _pull_client_scope():
        return await _backfill_history(config, data_dir, start, end)


async def _backfill_history(
    config: ExtConfig,
    data_dir,
    start: date,
    end: date,
) -> dict:
    """按本地交易日逐日回补 timeseries 历史分区 (幂等, 已有分区跳过)。

    前提: 接口支持按日期查询 (pull.date_param 已配置)。交易日取本地日K
    分区日期 —— 非交易日无人气数据, 也避免无谓请求。单日失败不中断,
    汇总进 failed 清单返回; 该日无数据 (空响应或 404) 计入 empty 跳过。
    """
    if config.mode != "timeseries":
        raise ValueError("仅 timeseries 模式支持历史回补 (snapshot 无历史概念)")
    pull = config.pull
    if not pull or not pull.url:
        raise ValueError("拉取未配置或 URL 为空")
    if not pull.date_param:
        raise ValueError("接口未配置日期参数 (date_param) —— 需接口支持 ?日期参数= 历史查询")
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    if (end - start).days + 1 > MAX_BACKFILL_DAYS:
        raise ValueError(f"单次回补上限 {MAX_BACKFILL_DAYS} 天, 请分段执行")

    from app.services.dragon_tiger import _local_trading_days

    days = [d for d in _local_trading_days(data_dir) if start <= d <= end]
    if not days:
        raise ValueError("范围内无本地交易日 (需先同步日K以确定交易日历)")

    fetched = skipped = empty = 0
    rows_written = 0
    failed: list[dict] = []
    consecutive_429 = 0  # 连续限流天数 (重试成功即清零); 达到阈值中止本次回补
    for i, d in enumerate(days):
        part = _day_partition(data_dir, config.id, d)
        if part.exists():
            skipped += 1
            continue
        try:
            rows = await fetch_rows_for_date(config, d)
            consecutive_429 = 0
            if not rows:
                empty += 1  # 该日无数据 (服务端未归档), 不是错误
            else:
                rows_written += rows_to_parquet(rows, config, data_dir, snapshot_date=d)
                fetched += 1
        except httpx.HTTPStatusError as e:
            if _status_code(e) == 404:
                # 接口契约 (tickflow-hub /exports、/fuyao-rank): 该日无快照
                # 返回 404 —— 视为该日无数据跳过, 不计入失败
                empty += 1
            elif _status_code(e) != 429:
                failed.append({"date": d.isoformat(), "reason": str(e)[:200]})
            else:
                # 服务端按分钟配额限流: 退避后原地重试一次; 连续多日 429
                # 说明配额窗口已耗尽, 中止剩余天数 (幂等, 重跑即可续补)。
                consecutive_429 += 1
                if consecutive_429 >= _BACKFILL_MAX_CONSECUTIVE_429:
                    remaining = [dd for dd in days[i:] if not _day_partition(data_dir, config.id, dd).exists()]
                    failed.extend({"date": dd.isoformat(), "reason": _RATE_LIMIT_ABORT_REASON}
                                  for dd in remaining)
                    break
                await asyncio.sleep(_BACKFILL_429_WAIT_S)
                try:
                    rows = await fetch_rows_for_date(config, d)
                    consecutive_429 = 0
                    if not rows:
                        empty += 1
                    else:
                        rows_written += rows_to_parquet(rows, config, data_dir, snapshot_date=d)
                        fetched += 1
                except Exception as e2:
                    if _status_code(e2) == 404:  # 退避重试后无该日快照 → 同样视为无数据
                        empty += 1
                    else:
                        failed.append({"date": d.isoformat(), "reason": str(e2)[:200]})
        except Exception as e:
            failed.append({"date": d.isoformat(), "reason": str(e)[:200]})
        if i + 1 < len(days):
            await asyncio.sleep(_BACKFILL_DAY_INTERVAL_S)  # 限速, 对数据源礼貌
    return {
        "total_days": len(days),
        "fetched": fetched,
        "skipped_existing": skipped,
        "empty": empty,
        "failed": failed,
        "rows_written": rows_written,
    }



# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------

# 每日固定时刻模式的等待分片: 长睡到明天会让「中途改配置/停用」最长等一整天
# 才生效, 分片轮询让变更在一分钟内被外层重读
_DAILY_POLL_SECONDS = 60


def _today_target(now: datetime, hhmm: str) -> datetime:
    """北京时间 now 当天的固定时刻 (保持 now 的时区信息)。"""
    return now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:5]), second=0, microsecond=0)


def _ran_at_or_after(pull: PullConfig, target: datetime) -> bool:
    """上次执行 (last_run, UTC ISO) 是否已不早于 target — 重启后不重复拉当天。

    解析失败按「未拉过」处理: 拉取本身幂等 (同日分区去重覆盖), 宁可重拉一次
    也不要因为状态字段脏了而整天静默跳过。
    """
    raw = pull.last_run
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(CN_TZ) >= target


def _is_scheduled_day(now: datetime, pull: PullConfig) -> bool:
    """今天 (北京时间) 是否为固定时刻模式的计划日: 周期 + 交易日过滤。

    weekly: schedule_day 1=周一..7=周日, 越界 (手改 config.json) 视为 1;
    monthly: schedule_day 1..31, 短月无该日则当月自然不命中 (该月跳过);
    trading_only: 跳过周六日 — 法定节假日无法离线判定, 仍会按期尝试,
    拉取幂等且失败仅记 last_status, 不影响后续调度。
    """
    period = pull.schedule_period or "daily"
    if period == "weekly":
        day = pull.schedule_day if 1 <= (pull.schedule_day or 1) <= 7 else 1
        if now.isoweekday() != day:
            return False
    elif period == "monthly":
        day = pull.schedule_day if 1 <= (pull.schedule_day or 1) <= 31 else 1
        if now.day != day:
            return False
    return not (pull.schedule_trading_only and now.isoweekday() > 5)


def _next_scheduled_target(now: datetime, hhmm: str, pull: PullConfig) -> datetime:
    """从 now 起下一个计划日的固定时刻 (供 next_run 预告与睡眠)。

    今天是计划日且时刻未到 → 今天; 否则从明天起逐日推进。防御上限 400 天:
    最苛刻组合 (每月 31 号 + 仅交易日) 最坏跨近一年, 超限回退明天 (不可达分支)。
    """
    tgt = _today_target(now, hhmm)
    if _is_scheduled_day(now, pull) and now < tgt:
        return tgt
    for i in range(1, 400):
        candidate = _today_target(now + timedelta(days=i), hhmm)
        if _is_scheduled_day(candidate, pull):
            return candidate
    return tgt + timedelta(days=1)


class PullScheduler:
    """后台调度器：为每个启用了 pull 的 ExtConfig 维护定时任务。

    线程安全说明:
      refresh()/stop() 可能从主事件循环 (lifespan startup) 或同步路由的
      worker 线程 (configure_pull 是 def 而非 async def, FastAPI 丢进线程池)
      调用。worker 线程里没有 running loop, 直接 asyncio.create_task 会抛
      "no running event loop"。因此对 task 的增删一律通过
      call_soon_threadsafe 提交到主循环执行 —— 同一套代码两种调用场景都安全。
    """
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self, data_dir) -> None:
        """启动调度（在 lifespan startup 调用，主事件循环内）。"""
        self._running = True
        self._data_dir = data_dir
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        logger.info("PullScheduler started")

    def _submit(self, fn, *args) -> None:
        """把一个 callable 提交到主事件循环执行 (线程安全)。

        startup 在主循环内调用时 fn 立即排队; worker 线程调用时跨线程排队。
        两者都通过 call_soon_threadsafe, 保证 _tasks 字典的读写只在主循环里发生。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeError(
                "PullScheduler: 事件循环不可用 (start() 未在事件循环中调用?)"
            )
        loop.call_soon_threadsafe(fn, *args)

    def stop(self) -> None:
        """停止所有任务 (从 shutdown 调用)。"""
        self._running = False
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
        logger.info("PullScheduler stopped")

    def refresh(self, data_dir) -> None:
        """重新加载配置，更新调度任务（增/删/改）。线程安全。"""
        self._data_dir = data_dir
        store = ExtConfigStore(data_dir)
        configs = store.load_all()

        active_ids: set[str] = set()
        enabled_configs: list[ExtConfig] = []

        for config in configs:
            if not config.pull or not config.pull.enabled or not config.pull.url:
                continue
            active_ids.add(config.id)
            enabled_configs.append(config)

        # 对 _tasks 的一切读判断 (含增删 diff) 都放进主循环闭包里执行:
        # refresh 可能从工作线程调用, 若在调用方线程读 _tasks 再把决策
        # 提交回主循环, 两步之间主循环可能已改动字典 (TOCTOU, #203)。
        # 此处只携带与 _tasks 无关的 config 数据跨线程。
        def _apply() -> None:
            for config in enabled_configs:
                if config.id not in self._tasks:
                    self._tasks[config.id] = self._loop.create_task(
                        self._run_loop(config)
                    )
                    logger.info(
                        "PullScheduler: scheduled %s (every %d min)",
                        config.id, config.pull.schedule_minutes,
                    )
            for cid in [c for c in self._tasks if c not in active_ids]:
                task = self._tasks.pop(cid)
                task.cancel()
                logger.info("PullScheduler: removed %s", cid)

        self._submit(_apply)

    async def _execute_pull(self, fresh: ExtConfig, store: ExtConfigStore) -> None:
        """执行一次拉取并把成功/失败状态写回配置 (间隔制与固定时刻制共用)。"""
        try:
            # 例行定时刷新: 不清策略结果缓存 (见 invalidate_ext_caches),
            # 否则策略页每轮拉取后整页空白, 直到下次全量重算完成。
            n, d = await fetch_and_ingest(
                fresh, self._data_dir, keep_strategy_cache=True
            )
            fresh.pull.last_run = datetime.now(timezone.utc).isoformat()
            fresh.pull.last_status = "success"
            fresh.pull.last_message = f"{n} rows @ {d}"
            fresh.pull.last_rows = n
            store.upsert(fresh, keep_strategy_cache=True)
            logger.info("PullScheduler: %s success, %d rows", fresh.id, n)
        except Exception as e:
            fresh2 = store.get(fresh.id)
            if fresh2 and fresh2.pull:
                fresh2.pull.last_run = datetime.now(timezone.utc).isoformat()
                fresh2.pull.last_status = "error"
                fresh2.pull.last_message = str(e)[:200]
                store.upsert(fresh2, keep_strategy_cache=True)
            logger.warning("PullScheduler: %s error: %s", fresh.id, e)

    async def _run_daily_at(self, config_id: str, store: ExtConfigStore, hhmm: str) -> None:
        """固定时刻 (北京时间) 周期性拉取: 每天 / 每周 / 每月 / 仅交易日。

        间隔制 (schedule_minutes) 锁相在启用时刻 —— 表达不了「每天 09:30 拉一次」;
        这里按墙钟对齐: 今天是计划日且当前时刻已过当日目标、上次执行早于它
        (重启安全) 就拉, 否则分片睡到下一个计划日的目标时刻。启用即对齐: 若
        启用时已过当日时刻、当日是计划日且未拉过, 立即补拉一次 (与间隔制
        「启用即拉取」同语义, 拉取本身幂等)。周期/交易日中途修改在一分钟内
        被下轮 fresh 重读生效。本模式忽略时间窗口与调度间隔 (固定时刻本身
        就是完整日程)。
        """
        while self._running:
            fresh = store.get(config_id)
            if (
                not fresh or not fresh.pull or not fresh.pull.enabled
                or fresh.pull.schedule_time != hhmm
            ):
                return  # 停用 / 改时刻 / 切回间隔制 → 外层循环重读
            now = cn_now()
            target = _today_target(now, hhmm)
            if (
                _is_scheduled_day(now, fresh.pull)
                and now >= target and not _ran_at_or_after(fresh.pull, target)
            ):
                await self._execute_pull(fresh, store)
                continue
            nxt = _next_scheduled_target(now, hhmm, fresh.pull)
            # 预告下次运行时间 (UTC ISO, 与间隔制同口径), 供前端展示
            nxt_iso = nxt.astimezone(UTC).isoformat()
            latest = store.get(config_id)
            if latest and latest.pull and latest.pull.next_run != nxt_iso:
                latest.pull.next_run = nxt_iso
                store.upsert(latest, keep_strategy_cache=True)
            await asyncio.sleep(
                min(_DAILY_POLL_SECONDS, max((nxt - now).total_seconds(), 1.0))
            )

    async def _sleep_interval_reloading(
        self, config_id: str, store: ExtConfigStore, interval: float,
    ) -> None:
        """间隔制睡眠, 但每 60s 重读配置。

        用户把间隔制改成固定时刻 (或改间隔/停用) 时, 旧实现要等整段间隔睡完
        才生效 —— 24h 间隔意味着新时刻要推迟一天。这里分片睡眠, 让配置改动
        在一分钟内生效。
        """
        remaining = float(interval)
        while remaining > 0 and self._running:
            chunk = min(_DAILY_POLL_SECONDS, remaining)
            await asyncio.sleep(chunk)
            remaining -= chunk
            fresh = store.get(config_id)
            if not fresh or not fresh.pull or not fresh.pull.enabled:
                return
            if fresh.pull.schedule_time or max(fresh.pull.schedule_minutes * 60, 60) != interval:
                return  # 调度形态或间隔变了 → 回外层重读并重新安排

    async def _run_loop(self, config: ExtConfig) -> None:
        """单个配置的定时拉取循环。

        策略: 启用后立即执行一次, 之后按 interval 循环。
        每次循环重读最新配置 (fresh), interval 取自 fresh.pull.schedule_minutes,
        这样用户中途修改间隔也能立即生效 (无需重启)。
        配置了 schedule_time (每日固定时刻) 时走 _run_daily_at 墙钟对齐。
        """
        try:
            while self._running:
                # 每轮重读最新配置 — 用户可能修改了 url / interval / enabled
                store = ExtConfigStore(self._data_dir)
                fresh = store.get(config.id)
                if not fresh or not fresh.pull or not fresh.pull.enabled:
                    break
                pull = fresh.pull

                if pull.schedule_time:
                    await self._run_daily_at(config.id, store, pull.schedule_time)
                    continue

                # 时间窗口检查: 不在窗口内则跳过本次拉取
                if not _in_time_window(pull.time_window_start, pull.time_window_end):
                    fresh.pull.last_run = datetime.now(timezone.utc).isoformat()
                    fresh.pull.last_status = "skipped"
                    fresh.pull.last_message = "不在拉取时间窗口内"
                    store.upsert(fresh, keep_strategy_cache=True)
                    logger.info("PullScheduler: %s skipped (outside time window)", config.id)
                    interval = max(pull.schedule_minutes * 60, 60)
                    await self._sleep_interval_reloading(config.id, store, interval)
                    continue

                # 先执行一次 (启用即拉取, 让用户立刻看到生效)
                await self._execute_pull(fresh, store)

                # 间隔取自最新配置 (每次重新读取, 修复改间隔不生效)
                interval = max(pull.schedule_minutes * 60, 60)  # 至少 60s
                # 预告下次运行时间, 供前端展示
                next_dt = datetime.now(UTC).timestamp() + interval
                latest = store.get(config.id)
                if latest and latest.pull:
                    latest.pull.next_run = datetime.fromtimestamp(
                        next_dt, tz=UTC
                    ).isoformat()
                    store.upsert(latest, keep_strategy_cache=True)

                await self._sleep_interval_reloading(config.id, store, interval)
                if not self._running:
                    break
        except asyncio.CancelledError:
            pass


# 全局单例
pull_scheduler = PullScheduler()
