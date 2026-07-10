"""监控规则 — 统一的 MonitorRule 模型,覆盖策略/个股信号/个股价格/市场异动四类。

职责:
  - 从 data/user_data/monitor_rules/*.json 加载规则定义
  - 校验规则字段合法性
  - 提供 CRUD (load_all / save_one / delete_one)

不知道: 行情评估引擎、API、告警落盘。纯函数 + 文件存储。

设计 (镜像 custom_signals.py 的写法):
  - 一对象一文件 + glob 全扫 + 全量重写
  - 字段白名单复用 custom_signals.ALLOWED_FIELDS (阈值条件) + 信号列清单 (布尔条件)
  - id 正则与 custom_signals 一致,保证可纳入同一索引体系
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from app.strategy.custom_signals import ALLOWED_FIELDS

logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────
ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
RULE_TYPES = {"strategy", "signal", "price", "market", "ladder"}
SCOPES = {"symbols", "all", "sector"}
LOGICS = {"and", "or"}
DIRECTIONS = {"entry", "exit", "both"}
SEVERITIES = {"info", "warn", "critical"}
OPS = {">", ">=", "<", "<=", "==", "!="}
# ladder 规则: 封单监控的指标 (量=手, 额=元)
LADDER_METRICS = {"sealed_vol", "sealed_amount"}
# ladder 规则: 方向 (up=涨停炸板预警, down=跌停翘板预警)
LADDER_DIRECTIONS = {"up", "down"}

# 布尔信号列前缀 (op=truth 时 field 取这些)
_SIGNAL_PREFIXES = ("signal_", "csg_")


# ── 持久化 (镜像 custom_signals.py) ─────────────────────
def _dir(data_dir: Path) -> Path:
    d = data_dir / "user_data" / "monitor_rules"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(data_dir: Path, rule_id: str) -> Path:
    return _dir(data_dir) / f"{rule_id}.json"


def load_all(data_dir: Path) -> list[dict]:
    """读取全部监控规则。损坏的文件被跳过。"""
    d = _dir(data_dir)
    out: list[dict] = []
    for f in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning("monitor rule load failed %s: %s", f.name, e)
    return out


def load_one(data_dir: Path, rule_id: str) -> dict | None:
    p = _path(data_dir, rule_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("monitor rule load failed %s: %s", rule_id, e)
        return None


def save_one(data_dir: Path, rule: dict) -> None:
    p = _path(data_dir, rule["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rule, ensure_ascii=False, indent=2), encoding="utf-8")


def delete_one(data_dir: Path, rule_id: str) -> bool:
    p = _path(data_dir, rule_id)
    if p.exists():
        p.unlink()
        return True
    return False


# ── 校验 ────────────────────────────────────────────────
def _is_signal_field(field: str) -> bool:
    """判断 field 是否为布尔信号列 (signal_ / csg_ 前缀)。"""
    return any(field.startswith(p) for p in _SIGNAL_PREFIXES)


def validate(rule: dict) -> None:
    """校验一条监控规则,非法则抛 ValueError (含中文信息)。"""
    rid = rule.get("id", "")
    if not isinstance(rid, str) or not ID_RE.match(rid):
        raise ValueError(f"规则 id 非法 (仅小写字母数字下划线, 1-40字符): {rid!r}")
    if not isinstance(rule.get("name"), str) or not rule["name"].strip():
        raise ValueError("规则 name 不能为空")
    if rule.get("type") not in RULE_TYPES:
        raise ValueError(f"type 必须是 {RULE_TYPES} 之一")

    # 策略类型: 需要 strategy_id + direction,conditions 可空
    if rule.get("type") == "strategy":
        if not rule.get("strategy_id"):
            raise ValueError("策略类型规则必须指定 strategy_id")
        if rule.get("direction", "entry") not in DIRECTIONS:
            raise ValueError(f"direction 必须是 {DIRECTIONS} 之一")
    elif rule.get("type") == "ladder":
        # 连板梯队封单监控: 需 metric + threshold + direction(up/down), 不用 conditions
        if rule.get("metric", "sealed_vol") not in LADDER_METRICS:
            raise ValueError(f"metric 必须是 {LADDER_METRICS} 之一")
        if rule.get("direction", "up") not in LADDER_DIRECTIONS:
            raise ValueError(f"direction 必须是 {LADDER_DIRECTIONS} 之一 (up=涨停炸板, down=跌停翘板)")
        thr = rule.get("threshold")
        if not isinstance(thr, (int, float)) or thr < 0:
            raise ValueError("threshold 必须是非负数字 (封单 ≤ 此值时报警)")
    else:
        # 信号/价格/市场类型: 需要 conditions
        conds = rule.get("conditions")
        if not isinstance(conds, list) or len(conds) == 0:
            raise ValueError("conditions 不能为空")
        if len(conds) > 8:
            raise ValueError("conditions 最多 8 条")
        if rule.get("logic", "and") not in LOGICS:
            raise ValueError(f"logic 必须是 {LOGICS} 之一")
        for i, c in enumerate(conds):
            if not isinstance(c, dict):
                raise ValueError(f"第 {i+1} 个条件格式错误")
            field = c.get("field", "")
            op = c.get("op", "")
            if op == "truth":
                # 布尔信号: field 必须是 signal_/csg_ 前缀
                if not _is_signal_field(field):
                    raise ValueError(f"第 {i+1} 个条件: op=truth 时 field 必须是信号列 (signal_/csg_ 前缀): {field!r}")
            elif op in OPS:
                # 阈值比较: field 必须在白名单, 需要 value
                if field not in ALLOWED_FIELDS:
                    raise ValueError(f"第 {i+1} 个条件: 阈值字段 {field!r} 不在白名单")
                if not isinstance(c.get("value"), (int, float)):
                    raise ValueError(f"第 {i+1} 个条件: value 必须是数字")
            else:
                raise ValueError(f"第 {i+1} 个条件: op {op!r} 非法 (应为 truth 或 {OPS})")

    # scope 校验
    if rule.get("scope", "symbols") not in SCOPES:
        raise ValueError(f"scope 必须是 {SCOPES} 之一")
    if rule.get("scope") == "symbols":
        syms = rule.get("symbols")
        if not isinstance(syms, list) or len(syms) == 0:
            raise ValueError("scope=symbols 时 symbols 不能为空")
    # sector 作用域的板块 JOIN 尚未实现: _apply_scope 目前会退化为「全市场」,
    # 一条本意针对某板块的规则会对全市场每只命中都触发(告警风暴)。在板块 JOIN
    # 落地前, 拒绝创建 sector 规则(fail-closed), 避免用户建出会刷屏的规则。
    if rule.get("scope") == "sector":
        raise ValueError("scope=sector 暂未支持(板块 JOIN 未实现),请改用 scope=symbols 指定标的或 scope=all")

    # 其余枚举
    if rule.get("severity", "info") not in SEVERITIES:
        raise ValueError(f"severity 必须是 {SEVERITIES} 之一")
    cd = rule.get("cooldown_seconds", 3600)
    if not isinstance(cd, int) or cd < 0:
        raise ValueError("cooldown_seconds 必须是非负整数")


def normalize(rule: dict) -> dict:
    """补全默认字段,返回规范化后的规则 (不校验)。"""
    r = dict(rule)
    r.setdefault("enabled", True)
    r.setdefault("asset_type", "stock")
    r.setdefault("scope", "symbols")
    r.setdefault("symbols", [])
    r.setdefault("sector", None)
    r.setdefault("strategy_id", None)
    # direction 默认值: ladder 用 "up", 其余用 "entry"
    r.setdefault("direction", "up" if r.get("type") == "ladder" else "entry")
    r.setdefault("conditions", [])
    # ladder 专属默认字段
    r.setdefault("metric", "sealed_vol")
    r.setdefault("threshold", 0)
    r.setdefault("logic", "and")
    r.setdefault("cooldown_seconds", 3600)
    r.setdefault("severity", "info")
    r.setdefault("message", "")
    r.setdefault("webhook_url", "")
    r.setdefault("webhook_enabled", False)
    # webhook_channels: 命中时推送的外部渠道 (合法值 'feishu' | 'wecom')。
    # 向后兼容: 老规则只有 webhook_enabled 布尔 (当时勾选即飞书+企业微信双推),
    # 这里把 webhook_enabled=True 但未带 webhook_channels 的老规则迁移为 ['feishu','wecom'],
    # 还原其当时的实际行为, 用户无感知。
    if r.get("webhook_channels") is None:
        r["webhook_channels"] = ["feishu", "wecom"] if r.get("webhook_enabled") else []
    else:
        # 防御性过滤, 只保留合法渠道
        r["webhook_channels"] = [c for c in r["webhook_channels"] if c in ("feishu", "wecom")]
    r.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    return r


# 策略监控自动迁移的规则 id 前缀 (固定, 保证幂等)
STRATEGY_RULE_PREFIX = "mr_strategy_"


def strategy_rule_id(strategy_id: str) -> str:
    """策略监控规则 id = mr_strategy_{strategy_id}。"""
    return f"{STRATEGY_RULE_PREFIX}{strategy_id}"


def migrate_strategy_monitors(data_dir: Path, strategy_ids: list[str], strategy_names: dict[str, str]) -> list[dict]:
    """把 preferences.strategy_monitor_ids 里的策略,同步生成/更新 type=strategy 规则。

    幂等: 已存在的策略规则会被更新 (方向/名称),不会重复创建。
    已从 strategy_ids 移除的策略, 其规则会被停用 (enabled=False) 而非删除 (保留历史触发记录的关联)。

    Args:
        data_dir: 数据目录
        strategy_ids: 当前监控池中的策略 id 列表
        strategy_names: {strategy_id: 策略名} 用于规则显示名
    Returns:
        本次生成/更新的规则列表
    """
    desired = set(strategy_ids)
    existing = load_all(data_dir)
    # 已存在的策略规则 {strategy_id: rule}
    existing_strategy_rules: dict[str, dict] = {}
    for r in existing:
        rid = r.get("id", "")
        if rid.startswith(STRATEGY_RULE_PREFIX):
            sid = rid[len(STRATEGY_RULE_PREFIX):]
            if sid:
                existing_strategy_rules[sid] = r

    touched: list[dict] = []
    # 1. 为当前监控池的策略 upsert 规则
    for sid in desired:
        rule_id = strategy_rule_id(sid)
        name = strategy_names.get(sid, sid)
        rule = existing_strategy_rules.get(sid)
        if rule is None:
            rule = normalize({
                "id": rule_id,
                "name": f"策略监控 · {name}",
                "type": "strategy",
                "scope": "all",
                "strategy_id": sid,
                "direction": "entry",
                "conditions": [],
                "cooldown_seconds": 3600,
                "enabled": True,
            })
        else:
            rule = dict(rule)
            rule["enabled"] = True
            rule["strategy_id"] = sid
            rule["name"] = f"策略监控 · {name}"
            rule.setdefault("scope", "all")
            rule.setdefault("direction", "entry")
        save_one(data_dir, rule)
        touched.append(rule)

    # 2. 不在监控池的策略 → 停用其规则 (不删除)
    for sid, rule in existing_strategy_rules.items():
        if sid not in desired and rule.get("enabled") is not False:
            rule = dict(rule)
            rule["enabled"] = False
            save_one(data_dir, rule)

    return touched
