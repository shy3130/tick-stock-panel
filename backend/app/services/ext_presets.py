"""内置扩展数据预设 — 概念/行业。

设计原则:
  - 出厂即写【完整可用的拉取配置】(URL / 鉴权 / 两级请求配方), 不是空壳。
    这样「数据」页展开表 → 拉取 就能看到一份真实可跑的配方, 照抄即可接入
    自有源; 也免去"点获取按钮才拉"的隐藏路径。
  - 扩展数据通用逻辑零改动 (ExtConfig / fetch_and_ingest / API / 前端均不动):
    概念/行业所需的「目录 → 逐条目」形态由 PullConfig.fanout 声明式表达,
    通用引擎 (ext_fanout) 执行, 无按 id 的特例分支。
  - 「已存在则跳过」: 绝不覆盖用户已有配置与数据, 老用户零影响。
  - 出厂值变更时定向迁移: 仅当现有配置的 pull 精确命中历代出厂值时整体替换,
    用户自定义过的一律不碰 (见 _RETIRED_FACTORY_PULLS)。
  - 拉取失败只记 warning, 不阻断启动 (保持「没数据也能跑」)。

数据来源 (概念/行业共用 fuyao 同花顺指数接口):
  - 板块清单: /api/a-share-index/catalog/ths-index-list (tag=cn_concept / industry)
  - 板块成分: /api/a-share-index/constituents/ths-stock-list (单板块一次)
  接口方向为「板块→股票」, fanout 反转为「股票→板块名清单」。
  鉴权复用 fuyao 行情插件同一凭据 (auth.key_ref = fuyao_api_key), 用户只需
  在一处填 Key。全量约 390+320 次成分股调用, 8 并发可承受。

接入点: app.main.lifespan → ensure_builtin_presets(store.data_dir)
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.services.ext_data import (
    ExtConfig,
    ExtConfigStore,
    ExtField,
    PullConfig,
)

logger = logging.getLogger(__name__)

# fuyao 同花顺指数接口 (鉴权 X-api-key, 信封 {code, message, data:{item:[...]}})
_FUYAO_BASE = "https://fuyao.aicubes.cn"
_FUYAO_CATALOG_PATH = "/api/a-share-index/catalog/ths-index-list"
_FUYAO_CONSTITUENTS_PATH = "/api/a-share-index/constituents/ths-stock-list"
# 成分股逐板块拉取: 全量 710 次调用, 8 并发 + 单板块失败重试 1 次 (实测无 429)
_FUYAO_CONCURRENCY = 8
_FUYAO_TIMEOUT_S = 20.0
_FUYAO_BOARD_RETRIES = 1
# 复用 fuyao 行情插件的凭据 (secrets.json: fuyao_api_key / 环境变量 FUYAO_API_KEY)
_FUYAO_KEY_REF = "fuyao_api_key"

_PRESET_IDS = ("ext_gn_ths", "ext_hy_ths")


# ---------------------------------------------------------------------------
# 预设定义: 字段结构 + 完整拉取配方
# ---------------------------------------------------------------------------

def _fuyao_auth() -> dict:
    """fuyao 鉴权: X-api-key 头, 凭据复用行情插件的 fuyao_api_key。"""
    return {"type": "header", "header": "X-api-key", "key_ref": _FUYAO_KEY_REF}


def _concept_preset() -> ExtConfig:
    """扩展概念 (ext_gn_ths)。

    两级拉取: 板块目录 (tag=cn_concept) → 逐板块成分股, 反转为
    「股票 → 所属概念」, 多个概念以分号拼接。
    输出 schema: 股票代码 / 股票简称 / 所属概念 / symbol / code
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
        description="同花顺概念分类 (fuyao 同花顺指数接口; 拉取配方见「拉取」页, 可照抄接自有源)",
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=f"{_FUYAO_BASE}{_FUYAO_CATALOG_PATH}?tag=cn_concept",
            method="GET",
            response_path="data.item",
            auth=_fuyao_auth(),
            timeout_seconds=int(_FUYAO_TIMEOUT_S),
            schedule_minutes=1440,
            enabled=True,
            fanout={
                "url": f"{_FUYAO_BASE}{_FUYAO_CONSTITUENTS_PATH}",
                "param": "thscode",
                "id_field": "thscode",
                "label_field": "name",
                "items_path": "data.item",
                "member_id_field": "thscode",
                "member_label_field": "name",
                "symbol_col": "股票代码",
                "name_col": "股票简称",
                "value_col": "所属概念",
                "separator": ";",
                "concurrency": _FUYAO_CONCURRENCY,
                "retries": _FUYAO_BOARD_RETRIES,
            },
        ),
    )


def _industry_preset() -> ExtConfig:
    """扩展行业 (ext_hy_ths)。

    fuyao 为扁平板块清单, 无一/二/三级层级路径, 单级粒度。
    输出 schema: 股票代码 / 股票简称 / 所属同花顺行业 / symbol / code
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
        description="同花顺行业分类 (fuyao 同花顺指数接口, 扁平板块无层级; 拉取配方见「拉取」页)",
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=f"{_FUYAO_BASE}{_FUYAO_CATALOG_PATH}?tag=industry",
            method="GET",
            response_path="data.item",
            auth=_fuyao_auth(),
            timeout_seconds=int(_FUYAO_TIMEOUT_S),
            schedule_minutes=1440,
            enabled=True,
            fanout={
                "url": f"{_FUYAO_BASE}{_FUYAO_CONSTITUENTS_PATH}",
                "param": "thscode",
                "id_field": "thscode",
                "label_field": "name",
                "items_path": "data.item",
                "member_id_field": "thscode",
                "member_label_field": "name",
                "symbol_col": "股票代码",
                "name_col": "股票简称",
                "value_col": "所属同花顺行业",
                "separator": "-",
                "concurrency": _FUYAO_CONCURRENCY,
                "retries": _FUYAO_BOARD_RETRIES,
            },
        ),
    )


def _presets() -> list[ExtConfig]:
    return [_concept_preset(), _industry_preset()]


def get_preset(config_id: str) -> ExtConfig | None:
    """按 id 取预设定义 (供 API 层校验 id 合法性)。"""
    for c in _presets():
        if c.id == config_id:
            return c
    return None


# 历代出厂 pull 配方指纹 — 仅比对 url + fanout 是否存在, 命中即视为未改过的出厂值。
# 换源/改配方后, 老用户的出厂配置需要整体替换为新配方; 用户自己填过 URL 的不动。
_RETIRED_FACTORY_PULLS: dict[str, tuple[dict, ...]] = {
    "ext_gn_ths": (
        # 换源前的自维护种子源
        {"url": "https://shy313.com/api/plugins/market_flow/exports/ths-concepts"},
        # 过渡期的 fuyao 目录标记 (无 fanout, 拉不到成分股)
        {"url": f"{_FUYAO_BASE}{_FUYAO_CATALOG_PATH}?tag=cn_concept"},
    ),
    "ext_hy_ths": (
        {"url": "https://shy313.com/api/plugins/market_flow/exports/ths-industries"},
        {"url": f"{_FUYAO_BASE}{_FUYAO_CATALOG_PATH}?tag=industry"},
    ),
}
# 出厂 description 前缀 (历代文案) — 命中则同步为当前文案, 用户自定义描述不动
_FACTORY_DESC_PREFIXES = (
    "同花顺概念分类",
    "同花顺行业分类",
)


def _is_retired_factory_pull(config_id: str, pull: PullConfig | None) -> bool:
    """现有 pull 是否为历代出厂值 (不含当前配方), 需要补齐/替换为当前配方。

    True 的三种情形:
      - pull 为 None / 空 URL: 出厂空壳 (#199 之后、本次改动之前的形态)
      - URL 命中历代出厂源, 且未带 fanout (换源前的种子源 / 只有目录的过渡配方)
    用户自定义过 URL 或已带 fanout 的配置一律 False (绝不覆盖)。
    """
    if pull is None:
        return True
    url = (pull.url or "").strip()
    if not url:
        return True   # 空 URL 的空壳配置: 由出厂空壳演进而来, 需补齐
    if pull.fanout:
        return False  # 已带两级配方 (当前形态或用户自建), 不碰
    return any(url == retired["url"] for retired in _RETIRED_FACTORY_PULLS.get(config_id, ()))


async def ensure_builtin_presets(data_dir: Path) -> None:
    """启动时: 为缺失的预设创建配置, 并把历代出厂空壳/旧配方补齐为当前配方。

    安全保证:
      - 已存在且是用户自定义配置 → 跳过, 绝不覆盖用户数据
      - 仅当 pull 为空 / 命中历代出厂配方指纹时, 用当前完整配方替换
      - description 仅当命中出厂前缀时同步, 用户自定义描述不动
      - 只写 config.json (不拉数据), 失败只记 warning 不阻断启动

    数据获取: 由拉取调度器按 schedule_minutes 执行, 或用户在数据页点「拉取 → 执行」。
    """
    store = ExtConfigStore(data_dir)
    for config in _presets():
        try:
            existing = store.get(config.id)
            if existing is None:
                store.upsert(config)
                logger.info("内置扩展表 %s 配置已就绪 (含出厂拉取配方)", config.id)
                continue

            changed = False
            if _is_retired_factory_pull(config.id, existing.pull):
                existing.pull = config.pull
                changed = True
            if not (existing.description or "").strip() or \
                    (existing.description or "").startswith(_FACTORY_DESC_PREFIXES):
                if existing.description != config.description:
                    existing.description = config.description
                    changed = True

            if changed:
                store.upsert(existing)
                logger.info("内置扩展表 %s 出厂配方已更新为当前版本", config.id)
        except Exception as e:  # noqa: BLE001 — 启动契约: 预设写入失败不阻断
            logger.warning("内置扩展表 %s 配置写入失败 (不影响启动): %s", config.id, e)
