"""用户自定义仓位策略 — data/user_data/position_policies/*.py 的发现/校验/存储。

文件由设置页「仓位策略」生成 (表单 META + compute 函数包装成完整 .py),
也可手工编辑 — 与自定义策略同一套信任模型: AST 安全校验 (复用
AIStrategyGenerator._validate_safety, 白名单追加本契约模块) 拦截已知
危险模式, 进程内执行 (T3 子进程沙箱的触发条件维持不变)。

生命周期契约 (secondary-development §4.3):
- 单文件损坏/不安全/缺 compute → 该条禁用并记入 errors, 不影响其余 (插件隔离);
- 目录 mtime 签名缓存惰性扫描 — 写盘/删除后下一次访问自动刷新, 无需 reload 钩子;
- 保存 = 安全校验 + 结构解析 + 合成冒烟 (position_sizing.smoke_compute),
  三关全过才落盘 (fail-closed)。
"""
from __future__ import annotations

import ast
import json
import logging
import re
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.backtest.position_sizing import ComputeFn, smoke_compute
from app.services.fs_utils import atomic_write_text
from app.strategy.ai_generator import AIStrategyGenerator

logger = logging.getLogger(__name__)

POLICY_DIR = "user_data/position_policies"
ID_RE = re.compile(r"^up_[a-z0-9_]{1,40}$")
_EXTRA_ALLOWED_IMPORTS = frozenset({"app.backtest.position_sizing"})
MAX_CODE_CHARS = 8000
MAX_PARAMS = 8


@dataclass(frozen=True)
class PolicyDefinition:
    id: str
    label: str
    description: str
    params: tuple[dict, ...]  # 与策略 META params 同构
    compute: ComputeFn
    source_file: str  # 相对 data_dir 的路径, 冒烟/列表展示用


# ── 文件渲染与解析 (表单 ⇄ .py 往返) ────────────────────────

def render_policy_file(meta: dict, compute_src: str) -> str:
    """表单 META + compute 源码 → 完整 .py 文件内容。"""
    header = (
        f'"""仓位策略: {meta.get("label", "")} — 由设置页生成, 也可手工编辑。\n'
        "\n"
        "契约: def compute(ctx) -> PositionSizingResult; ctx 字段与输出校验见\n"
        "app/backtest/position_sizing.py。保存/导入前经过 AST 安全校验与合成冒烟。\n"
        '"""\n'
        "from app.backtest.position_sizing import PositionSizingResult\n"
        "\n"
        f"META = {json.dumps(meta, ensure_ascii=False, indent=2)}\n"
        "\n"
    )
    return header + compute_src.rstrip() + "\n"


def parse_policy_file(code: str) -> tuple[dict, str]:
    """完整 .py → (META, 用户代码区); 结构不合法抛 ValueError (中文)。

    用户代码区 = 移除自动生成的三件套 (模块 docstring / 注入的契约 import /
    META 赋值) 后的原文行 — 用户自己的 import、辅助函数、格式全部原样保留,
    保证 render→parse 严格往返 (编辑器往返一致性的基础)。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise ValueError(f"Python 语法错误: {e.msg}") from e
    meta: dict | None = None
    excluded_ranges: list[tuple[int, int]] = []
    body = list(tree.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        excluded_ranges.append((body[0].lineno, body[0].end_lineno or body[0].lineno))
        body = body[1:]
    has_compute = False
    for node in body:
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "app.backtest.position_sizing"
        ):
            excluded_ranges.append((node.lineno, node.end_lineno or node.lineno))
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "META" for t in node.targets
        ):
            try:
                meta = ast.literal_eval(node.value)
            except (ValueError, SyntaxError) as e:
                raise ValueError(f"META 必须是字面量字典: {e}") from e
            excluded_ranges.append((node.lineno, node.end_lineno or node.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "compute":
            has_compute = True
    if not isinstance(meta, dict):
        raise ValueError("缺少模块级 META 字典")
    if not isinstance(meta.get("label"), str) or not str(meta.get("label", "")).strip():
        raise ValueError("META.label 不能为空")
    if not has_compute:
        raise ValueError("缺少模块级 def compute(ctx) 函数")
    lines = code.splitlines()
    drop = set()
    for start, end in excluded_ranges:
        drop.update(range(start, end + 1))
    user_src = "\n".join(
        line for i, line in enumerate(lines, start=1) if i not in drop
    ).strip("\n")
    return meta, user_src


def _normalize_meta(meta: dict, *, require_id: bool = True) -> dict:
    normalized = {
        "id": str(meta.get("id", "")),
        "label": str(meta.get("label", "")).strip(),
        "description": str(meta.get("description", "")),
        "params": list(meta.get("params") or []),
    }
    if require_id and not ID_RE.match(normalized["id"]):
        raise ValueError(f"id 必须匹配 {ID_RE.pattern} (小写字母数字下划线, up_ 前缀)")
    if not normalized["label"]:
        raise ValueError("label 不能为空")
    if len(normalized["params"]) > MAX_PARAMS:
        raise ValueError(f"参数最多 {MAX_PARAMS} 个")
    for p in normalized["params"]:
        if not isinstance(p, dict) or not isinstance(p.get("id"), str) or not p["id"]:
            raise ValueError("每个参数需要非空 id")
        if p.get("type", "float") not in ("float", "int", "bool", "select", "date", "string"):
            raise ValueError(f"参数 {p['id']} 类型非法: {p.get('type')!r}")
    return normalized


def _structural_issues(code: str) -> list[str]:
    issues: list[str] = []
    try:
        meta, _ = parse_policy_file(code)
        _normalize_meta(meta)
    except ValueError as e:
        issues.append(str(e))
    return issues


def _safety_issues(code: str) -> list[str]:
    try:
        AIStrategyGenerator._validate_safety(
            code, extra_allowed_import_modules=_EXTRA_ALLOWED_IMPORTS
        )
        return []
    except (ValueError, SyntaxError) as e:
        return [f"安全校验失败: {e}"]


def validate_policy_code(code: str) -> dict:
    """三关校验 (安全 → 结构 → 冒烟); 返回 {ok, issues, smoke}。"""
    issues: list[str] = []
    if len(code) > MAX_CODE_CHARS:
        issues.append(f"代码超过 {MAX_CODE_CHARS} 字符上限")
        return {"ok": False, "issues": issues, "smoke": None}
    issues.extend(_safety_issues(code))
    issues.extend(_structural_issues(code))
    smoke: dict | None = None
    if not issues:
        loaded = _exec_policy(code, source_hint="<validate>")
        if isinstance(loaded, Exception):
            issues.append(f"加载失败: {loaded}")
        else:
            smoke = smoke_compute(loaded.compute)
            if not smoke["ok"]:
                issues.extend(smoke["issues"])
                smoke = None
    return {"ok": not issues, "issues": issues, "smoke": smoke}


def _exec_policy(code: str, *, source_hint: str) -> PolicyDefinition | Exception:
    """编译执行策略文件 → PolicyDefinition; 任何失败返回异常本体 (不抛)。"""
    try:
        meta, _ = parse_policy_file(code)
        meta = _normalize_meta(meta)
        module_name = f"position_policy_{meta['id']}_{secrets.token_hex(4)}"
        ns: dict[str, Any] = {"__name__": module_name}
        exec(compile(code, source_hint, "exec"), ns)
        compute = ns.get("compute")
        if not callable(compute):
            raise ValueError("compute 不是可调用对象")
        return PolicyDefinition(
            id=meta["id"],
            label=meta["label"],
            description=meta["description"],
            params=tuple(meta["params"]),
            compute=compute,
            source_file=source_hint,
        )
    except Exception as e:  # noqa: BLE001 — 调用方按 fail-closed 处理
        return e


# ── 目录注册表 (mtime 签名缓存, 惰性扫描) ──────────────────────

@dataclass
class _Snapshot:
    signature: tuple
    policies: dict[str, PolicyDefinition]
    errors: dict[str, str]


_LOCK = threading.Lock()
_CACHE: dict[str, _Snapshot] = {}


def _dir_signature(d: Path) -> tuple:
    entries = []
    if d.is_dir():
        for f in sorted(d.glob("*.py")):
            st = f.stat()
            entries.append((f.name, st.st_mtime_ns, st.st_size))
    return tuple(entries)


def _snapshot(data_dir: Path) -> _Snapshot:
    d = Path(data_dir) / POLICY_DIR
    sig = _dir_signature(d)
    key = str(d)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached.signature == sig:
            return cached
    policies: dict[str, PolicyDefinition] = {}
    errors: dict[str, str] = {}
    if d.is_dir():
        for f in sorted(d.glob("*.py")):
            try:
                code = f.read_text(encoding="utf-8")
            except OSError as e:
                errors[f.name] = f"读取失败: {e}"
                continue
            issues = _safety_issues(code) or _structural_issues(code)
            if issues:
                errors[f.name] = "; ".join(issues)
                continue
            loaded = _exec_policy(code, source_hint=str(f))
            if isinstance(loaded, Exception):
                errors[f.name] = f"加载失败: {loaded}"
                continue
            if loaded.id in policies:
                errors[f.name] = f"重复策略 id {loaded.id!r}"
                continue
            policies[loaded.id] = loaded
    snap = _Snapshot(signature=sig, policies=policies, errors=errors)
    with _LOCK:
        _CACHE[key] = snap
    return snap


def list_policies(data_dir: Path) -> tuple[list[dict], dict[str, str]]:
    """全部可用策略 (视图 dict) + 坏文件错误表 {文件名: 原因}。"""
    snap = _snapshot(data_dir)
    views = [
        {
            "id": p.id,
            "label": p.label,
            "description": p.description,
            "params": list(p.params),
            "source_file": p.source_file,
        }
        for p in snap.policies.values()
    ]
    return views, dict(snap.errors)


def get_policy(data_dir: Path, policy_id: str) -> PolicyDefinition | None:
    snap = _snapshot(data_dir)
    return snap.policies.get(str(policy_id))


def invalidate_cache() -> None:
    with _LOCK:
        _CACHE.clear()


# ── 保存 / 删除 (API 层调用, 三关全过才写盘) ────────────────────

def save_policy(data_dir: Path, meta: dict, compute_src: str) -> dict:
    """渲染 → 三关校验 → 原子写盘; 返回 {ok, id, issues}。"""
    normalized = _normalize_meta(meta, require_id=False)
    if not ID_RE.match(normalized["id"]):
        normalized["id"] = f"up_{secrets.token_hex(4)}"
    code = render_policy_file(normalized, compute_src)
    result = validate_policy_code(code)
    if not result["ok"]:
        return {"ok": False, "id": normalized["id"], "issues": result["issues"]}
    d = Path(data_dir) / POLICY_DIR
    d.mkdir(parents=True, exist_ok=True)
    atomic_write_text(d / f"{normalized['id']}.py", code)
    invalidate_cache()
    return {"ok": True, "id": normalized["id"], "issues": []}


def delete_policy(data_dir: Path, policy_id: str) -> bool:
    p = Path(data_dir) / POLICY_DIR / f"{policy_id}.py"
    if p.exists():
        p.unlink()
        invalidate_cache()
        return True
    return False


# ── 新建模板 (「从模板新建」的预填内容) ────────────────────────

TEMPLATE_BLANK = {
    "key": "blank",
    "label": "空白模板",
    "description": "只有等权骨架, 从零写自己的规则。",
    "meta": {"id": "", "label": "我的仓位策略", "description": "", "params": []},
    "code": (
        "def compute(ctx):\n"
        "    # ctx.selected: 当日候选 [(symbol, score), ...] 按信号强度降序\n"
        "    # ctx.recent_returns: {symbol: 最近60日收益率} (仅当日选中标的)\n"
        "    # ctx.closed_trades: 已平仓交易快照 (pnl_pct/duration/exit_reason)\n"
        "    # ctx.portfolio: cash/equity/drawdown(≤0)/open_positions\n"
        "    # ctx.params: 上面表单定义的参数\n"
        "    n = len(ctx.selected)\n"
        "    weights = [1.0 / n] * n\n"
        "    return PositionSizingResult(weights=weights, budget_scale=1.0)\n"
    ),
}

TEMPLATE_VOL_TARGET = {
    "key": "vol_target",
    "label": "波动率目标",
    "description": "波动大的标的少买 — 权重按近期波动率的倒数分配。",
    "meta": {
        "id": "",
        "label": "波动率目标仓位",
        "description": "按波动率倒数分配候选权重",
        "params": [
            {"id": "window", "label": "波动率窗口(日)", "type": "int", "default": 20, "min": 5, "max": 60, "step": 1},
        ],
    },
    "code": (
        "import numpy as np\n"
        "\n"
        "\n"
        "def compute(ctx):\n"
        "    window = int(ctx.params.get(\"window\", 20))\n"
        "    inv = []\n"
        "    for cand in ctx.selected:\n"
        "        rets = list(ctx.recent_returns.get(cand.symbol, ()))[-window:]\n"
        "        vol = float(np.std(rets)) if len(rets) >= 5 else 0.02\n"
        "        inv.append(1.0 / max(vol, 1e-6))\n"
        "    total = sum(inv)\n"
        "    weights = [v / total for v in inv]\n"
        "    return PositionSizingResult(weights=weights, budget_scale=1.0)\n"
    ),
}

TEMPLATE_DRAWDOWN = {
    "key": "drawdown",
    "label": "回撤自适应",
    "description": "组合回撤越深, 整体买入预算越小 (反鞅), 个股间等权。",
    "meta": {
        "id": "",
        "label": "回撤自适应仓位",
        "description": "按组合回撤缩放总预算",
        "params": [
            {"id": "max_drawdown", "label": "最大容忍回撤(%)", "type": "float", "default": 20.0, "min": 5.0, "max": 50.0, "step": 1.0},
            {"id": "floor_scale", "label": "最低预算比例", "type": "float", "default": 0.3, "min": 0.0, "max": 1.0, "step": 0.05},
        ],
    },
    "code": (
        "def compute(ctx):\n"
        "    max_dd = float(ctx.params.get(\"max_drawdown\", 20.0)) / 100.0\n"
        "    floor = float(ctx.params.get(\"floor_scale\", 0.3))\n"
        "    drawdown = abs(min(ctx.portfolio.drawdown, 0.0))\n"
        "    scale = max(floor, 1.0 - drawdown / max(max_dd, 1e-6))\n"
        "    n = len(ctx.selected)\n"
        "    weights = [1.0 / n] * n\n"
        "    return PositionSizingResult(weights=weights, budget_scale=scale)\n"
    ),
}

TEMPLATES = (TEMPLATE_BLANK, TEMPLATE_VOL_TARGET, TEMPLATE_DRAWDOWN)
