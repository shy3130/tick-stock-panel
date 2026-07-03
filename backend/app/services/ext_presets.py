"""内置扩展数据预设 — 概念/行业首次启动自动拉取。

设计原则:
  - 扩展数据通用逻辑零改动 (ExtConfig / fetch_and_ingest / API / 前端均不动)
  - 仅在本模块做「接口结构 → 本地 schema」的转换
  - 「已存在则跳过」: 绝不覆盖用户已有数据, 老用户零影响
  - 拉取失败只记 warning, 不阻断启动 (保持「没数据也能跑」)

种子数据来源: https://files.688798.xyz/ths/{concepts,industries}.json
作者更新数据只需改接口上的 JSON, 用户下次拉取自动同步, 无需发版。

接入点: app.main.lifespan → ensure_builtin_presets(store.data_dir)
"""
from __future__ import annotations

import logging
from pathlib import Path
from datetime import date

from app.services.ext_data import (
    ExtConfig,
    ExtConfigStore,
    ExtField,
    PullConfig,
    rows_to_parquet,
)

logger = logging.getLogger(__name__)

# 种子数据源 (作者维护, 改这里即对所有用户生效)
_THS_BASE = "https://files.688798.xyz/ths"
_EM_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"


# ---------------------------------------------------------------------------
# 预设定义: 字段结构 + 拉取配方
# ---------------------------------------------------------------------------

def _concept_preset() -> ExtConfig:
    """扩展概念 (ext_gn_ths)。

    接口结构: [{symbol, name, concepts: [概念1, 概念2, ...]}]
    本地 schema: 股票代码 / 股票简称 / 所属概念(分号拼接) / symbol / code
    """
    return ExtConfig(
        id="ext_gn_ths",
        label="扩展概念",
        mode="snapshot",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("code", "string", "代码"),
            ExtField("股票代码", "string", "股票代码"),
            ExtField("股票简称", "string", "股票简称"),
            ExtField("所属概念", "string", "所属概念"),
        ],
        description="同花顺概念分类 (首次启动自动拉取, 可在扩展数据页手动更新)",
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=f"{_THS_BASE}/concepts.json",
            method="GET",
            schedule_minutes=1440,
            enabled=False,
        ),
    )


def _industry_preset() -> ExtConfig:
    """扩展行业 (ext_hy_ths)。

    接口结构: [{symbol, name, industries: [一级行业, 二级行业, 三级行业]}]
    本地 schema: 股票代码 / 股票简称 / 所属同花顺行业(横杠拼接) / symbol / code
    """
    return ExtConfig(
        id="ext_hy_ths",
        label="扩展行业",
        mode="snapshot",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("code", "string", "代码"),
            ExtField("股票代码", "string", "股票代码"),
            ExtField("股票简称", "string", "股票简称"),
            ExtField("所属同花顺行业", "string", "所属同花顺行业"),
        ],
        description="同花顺行业分类 (首次启动自动拉取, 可在扩展数据页手动更新)",
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=f"{_THS_BASE}/industries.json",
            method="GET",
            schedule_minutes=1440,
            enabled=False,
        ),
    )


def _dragon_tiger_preset() -> ExtConfig:
    return ExtConfig(
        id="ext_lhb_em",
        label="龙虎榜",
        mode="snapshot",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("code", "string", "代码"),
            ExtField("trade_date", "string", "交易日期"),
            ExtField("name", "string", "名称"),
            ExtField("close", "float", "收盘价"),
            ExtField("change_pct", "float", "涨跌幅%"),
            ExtField("net_buy", "float", "龙虎榜净买额"),
            ExtField("buy_amount", "float", "买入金额"),
            ExtField("sell_amount", "float", "卖出金额"),
            ExtField("turnover", "float", "成交额"),
            ExtField("reason", "string", "上榜原因"),
        ],
        description="东方财富龙虎榜日榜 (手动获取当天数据)",
        symbol_map={"type": "mapped", "col": "symbol"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=_EM_DATACENTER,
            method="GET",
            schedule_minutes=1440,
            enabled=False,
        ),
    )


def _presets() -> list[ExtConfig]:
    return [_concept_preset(), _industry_preset(), _dragon_tiger_preset()]


# ---------------------------------------------------------------------------
# 接口结构 → 本地 schema 转换 (仅预设使用)
# ---------------------------------------------------------------------------

def _symbol_to_code(symbol: str) -> str:
    """symbol (000001.SZ) → code (000001)。"""
    return symbol.split(".", 1)[0] if "." in symbol else symbol


def _flatten_concept_rows(raw_rows: list[dict]) -> list[dict]:
    """概念: concepts 数组 → 分号拼接成「所属概念」字符串。

    [{symbol, name, concepts:[...]}] → [{股票代码, 股票简称, 所属概念, symbol, code}]
    注: code 由 symbol 派生 (000001.SZ → 000001), 因 rows_to_parquet 不执行 code_map。
    """
    out: list[dict] = []
    for r in raw_rows:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        concepts = r.get("concepts") or []
        out.append({
            "股票代码": sym,
            "股票简称": r.get("name") or "",
            "所属概念": ";".join(str(c) for c in concepts if c),
            "symbol": sym,
            "code": _symbol_to_code(sym),
        })
    return out


def _flatten_industry_rows(raw_rows: list[dict]) -> list[dict]:
    """行业: industries 数组 → 横杠拼接成「所属同花顺行业」字符串。

    [{symbol, name, industries:[...]}] → [{股票代码, 股票简称, 所属同花顺行业, symbol, code}]
    """
    out: list[dict] = []
    for r in raw_rows:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        inds = r.get("industries") or []
        out.append({
            "股票代码": sym,
            "股票简称": r.get("name") or "",
            "所属同花顺行业": "-".join(str(i) for i in inds if i),
            "symbol": sym,
            "code": _symbol_to_code(sym),
        })
    return out


def _a_symbol(code: str) -> str:
    code = str(code or "").strip()
    if code.startswith(("8", "4", "92")):
        return f"{code}.BJ"
    if code.startswith(("60", "68", "90", "11", "13")):
        return f"{code}.SH"
    return f"{code}.SZ"


def _flatten_dragon_tiger_rows(raw_rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in raw_rows:
        code = str(r.get("SECURITY_CODE") or "").strip()
        if not code:
            continue
        out.append({
            "symbol": _a_symbol(code),
            "code": code,
            "trade_date": str(r.get("TRADE_DATE") or "")[:10],
            "name": r.get("SECURITY_NAME_ABBR") or "",
            "close": r.get("CLOSE_PRICE"),
            "change_pct": r.get("CHANGE_RATE"),
            "net_buy": r.get("BILLBOARD_NET_AMT"),
            "buy_amount": r.get("BILLBOARD_BUY_AMT"),
            "sell_amount": r.get("BILLBOARD_SELL_AMT"),
            "turnover": r.get("ACCUM_AMOUNT"),
            "reason": r.get("EXPLANATION") or "",
        })
    return out


# ---------------------------------------------------------------------------
# 拉取执行 (复用 httpx, 不依赖 fetch_and_ingest 的 PullConfig 路径)
# ---------------------------------------------------------------------------

async def _fetch_json(url: str) -> list[dict]:
    """请求 JSON 接口, 返回行数组。超时 30s, 失败抛异常由调用方兜底。"""
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"接口返回不是数组: {type(data)}")
    return data


async def _seed_one(config: ExtConfig, flatten, data_dir: Path) -> int:
    """拉取 + 转换 + 写入单个预设。返回写入行数。"""
    from datetime import date

    raw = await _fetch_json(config.pull.url)
    rows = flatten(raw)
    if not rows:
        raise ValueError(f"接口返回 0 行: {config.pull.url}")
    n = rows_to_parquet(rows, config, data_dir, snapshot_date=date.today())
    return n


async def _seed_dragon_tiger(config: ExtConfig, data_dir: Path) -> int:
    from app.services import eastmoney_client

    today = date.today().isoformat()
    payload = eastmoney_client.get_json(_EM_DATACENTER, params={
        "reportName": "RPT_DAILYBILLBOARD_DETAILS",
        "columns": "ALL",
        "filter": f"(TRADE_DATE='{today}')",
        "sortColumns": "BILLBOARD_NET_AMT",
        "sortTypes": "-1",
        "pageNumber": "1",
        "pageSize": "500",
        "source": "WEB",
        "client": "WEB",
    })
    result = payload.get("result") if isinstance(payload, dict) else None
    raw_rows = result.get("data") if isinstance(result, dict) else []
    rows = _flatten_dragon_tiger_rows(raw_rows if isinstance(raw_rows, list) else [])
    if not rows:
        raise ValueError(f"东方财富龙虎榜 {today} 返回 0 行")
    return rows_to_parquet(rows, config, data_dir, snapshot_date=date.today())


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def get_preset(config_id: str) -> ExtConfig | None:
    """按 id 取预设定义 (供 API 层校验 id 合法性)。"""
    for c in _presets():
        if c.id == config_id:
            return c
    return None


async def ensure_builtin_presets(data_dir: Path) -> None:
    """启动时: 为缺失的预设创建 config.json (含 pull 配置), 但【不拉取数据】。

    设计: 数据获取改为用户在概念/行业页手动点「获取数据」触发, 避免启动时
    网络请求阻塞, 也避免「自动拉取」与「用户自主控制」的预期冲突。

    安全保证:
      - 已存在则完全跳过 (绝不覆盖用户数据)
      - 只写 config.json, 失败只记 warning 不阻断启动
    """
    store = ExtConfigStore(data_dir)

    for config in _presets():
        existing = store.get(config.id)
        if existing is not None:
            # 用户已有此表 (老用户 / 自己重建过) → 一律不动
            continue
        try:
            store.upsert(config)
            logger.info("内置扩展表 %s 配置已就绪 (待用户手动获取数据)", config.id)
        except Exception as e:  # noqa: BLE001
            logger.warning("内置扩展表 %s 配置写入失败 (不影响启动): %s", config.id, e)


async def fetch_preset(config_id: str, data_dir: Path) -> int:
    """手动触发某个预设的数据拉取 (供 API 调用)。

    Raises:
        ValueError: config_id 不是内置预设
        Exception: 网络请求/解析/写入失败 (由 API 层转 HTTP 错误)
    """
    config = get_preset(config_id)
    if config is None:
        raise ValueError(f"未知的内置预设: {config_id}")

    # 确保 config.json 存在 (用户可能从未启动过 ensure_builtin_presets)
    store = ExtConfigStore(data_dir)
    if store.get(config_id) is None:
        store.upsert(config)

    if config_id == "ext_lhb_em":
        n = await _seed_dragon_tiger(config, data_dir)
    else:
        flatten = _flatten_concept_rows if config_id == "ext_gn_ths" else _flatten_industry_rows
        n = await _seed_one(config, flatten, data_dir)
    logger.info("内置扩展表 %s 手动拉取成功: %d 行", config_id, n)
    return n
