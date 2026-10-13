"""两级拉取 (fanout) — 「目录 → 逐条目明细」接口的声明式配方。

适用面: 数据源只提供「类别清单」+「单类别成员」两个端点, 没有一次返回
「成员 → 所属类别」全量映射的接口 (同花顺概念/行业、多数板块类 API)。

通用拉取是单请求 + 字段名映射, 表达不了这种形态; 与其按 config_id 硬编码
特例, 不如把「第二级怎么发、结果怎么拼」写进 PullConfig.fanout —— 配置本身
即配方, 在「数据」页展开扩展表 → 拉取 就能看到全貌, 也便于照抄接入自有源
(见 docs/custom-data-source.md)。

fanout 配方字段:
  url                第二级请求地址 (与 pull.url 同走标识头 + auth 鉴权)
  param              第二级请求的查询参数名, 取值为第一级条目的 id_field
  id_field           第一级条目中作为第二级参数的字段
  label_field        第一级条目中作展示名的字段 (可选, 缺省用 id_field; 仅用于告警)
  items_path         第二级响应里成员数组的 dot-path (留空 = 响应本身即数组)
  member_id_field    成员对象里取标的代码的字段
  member_label_field 成员对象里取标的名称的字段 (可选)
  symbol_col         输出列名: 标的代码
  name_col           输出列名: 标的名称
  value_col          输出列名: 拼接后的类别
  separator          多类别拼接符 (概念用 ";", 行业层级用 "-")
  concurrency        并发上限 (可选, 默认 8)
  retries            单条目失败重试次数 (可选, 默认 1; 用尽则跳过该条目)

产出: 行方向由「类别 → 成员」反转为「成员 → 类别清单」, 与单请求路径的
行结构一致, 下游 rows_to_parquet / 分析页零改动。
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from app.services.ext_data import ExtConfig

logger = logging.getLogger(__name__)

_DEFAULT_CONCURRENCY = 8
_DEFAULT_RETRIES = 1
_REQUIRED_KEYS = (
    "url", "param", "id_field", "items_path", "member_id_field",
    "symbol_col", "name_col", "value_col",
)


def clean_label(value: Any) -> str:
    """类别名归一: 剔除 None / NaN / 字符串 "nan" 等占位值。"""
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return ""
    text = str(value).strip()
    return "" if text.casefold() in {"nan", "none", "null"} else text


def _raise_on_envelope_error(data: Any) -> None:
    """常见信封 {code, message, data}: code 非 0 时抛出带服务端文案的错误。"""
    if isinstance(data, dict):
        code = data.get("code")
        if code not in (0, None, "0"):
            raise ValueError(f"接口返回错误 code={code}: {data.get('message', '')}")


async def fetch_fanout_rows(config: ExtConfig, target_date=None) -> list[dict]:
    """执行两级拉取并反转为「成员 → 类别清单」行。"""
    from app.services.ext_pull import _append_query_params, _extract_rows, _request_json

    pull = config.pull
    if pull is None:
        raise ValueError("拉取未配置")
    fanout = pull.fanout or {}
    missing = [k for k in _REQUIRED_KEYS if not fanout.get(k)]
    if missing:
        raise ValueError(f"fanout 配方缺少必填项: {', '.join(missing)}")

    # ---- 第一级: 类别清单 ----
    catalog = await _request_json(pull, config.id, target_date)
    _raise_on_envelope_error(catalog)
    items = _extract_rows(catalog, pull.response_path)

    id_field = fanout["id_field"]
    label_field = fanout.get("label_field") or id_field
    entries = [
        it for it in items
        if isinstance(it, dict) and str(it.get(id_field) or "").strip()
    ]
    if not entries:
        raise ValueError(f"第一级返回 0 个可用条目 (id_field={id_field})")

    # ---- 第二级: 逐条目成员 (并发 + 单条目失败容忍) ----
    concurrency = int(fanout.get("concurrency") or _DEFAULT_CONCURRENCY)
    raw_retries = fanout.get("retries")
    retries = int(_DEFAULT_RETRIES if raw_retries is None else raw_retries)
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _members(entry: dict) -> list[dict] | None:
        member_url = _append_query_params(
            fanout["url"], {fanout["param"]: str(entry[id_field]).strip()}
        )
        async with sem:
            for attempt in range(retries + 1):
                try:
                    data = await _request_json(
                        pull, config.id, None, url_override=member_url
                    )
                    _raise_on_envelope_error(data)
                    return _extract_rows(data, fanout.get("items_path") or "")
                except Exception:  # noqa: BLE001 — 单条目失败: 重试后跳过
                    if attempt == retries:
                        return None
                    await asyncio.sleep(0.5)
        return None

    members_list = await asyncio.gather(*(_members(e) for e in entries))

    # ---- 反转聚合: 成员 → 类别清单 (顺序跟随目录顺序, 保证拼接结果稳定) ----
    m_id = fanout["member_id_field"]
    m_name = fanout.get("member_label_field")
    agg: dict[str, dict] = {}
    failed: list[str] = []
    for entry, members in zip(entries, members_list, strict=True):
        if members is None:
            failed.append(str(entry.get(label_field) or entry.get(id_field) or ""))
            continue
        label = clean_label(entry.get(label_field))
        for m in members:
            if not isinstance(m, dict):
                continue
            sym = str(m.get(m_id) or "").strip()
            if not sym:
                continue
            row = agg.get(sym)
            if row is None:
                row = {
                    fanout["symbol_col"]: sym,
                    fanout["name_col"]: (m.get(m_name) or "") if m_name else "",
                    fanout["value_col"]: [],
                }
                agg[sym] = row
            if label and label not in row[fanout["value_col"]]:
                row[fanout["value_col"]].append(label)

    if failed:
        logger.warning(
            "fanout 第二级失败 %d/%d (已跳过): %s",
            len(failed), len(entries), "、".join(failed[:5]),
        )
    if not agg:
        raise ValueError("fanout 全部条目第二级拉取失败")

    separator = fanout.get("separator") or ";"
    return [
        {**row, fanout["value_col"]: separator.join(row[fanout["value_col"]])}
        for row in agg.values()
    ]
