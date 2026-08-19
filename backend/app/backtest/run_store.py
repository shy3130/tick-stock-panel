"""BacktestRun — 不可变回测实验记录的持久化、查询与旧 run_card 迁移。

存储契约 (schema_version=1):
- 目录: <data_dir>/research/backtest_runs/{run_id}.json
- 核心字段不可变; 仅 favorite/label 可通过 patch 修改 (API 层 extra=forbid)
- 单文件 20 MiB 上限, 超限直接拒绝且不留半文件
- 原子写: 同目录临时文件 + os.replace, 崩溃只可能留下 .tmp 孤儿
- run_id 严格白名单, 防路径穿越; 所有非有限数值经 json_safe 转 null
- 旧 research/run_cards/*.json 只读迁移 (warnings 加 legacy_run_card),
  不修改不删除原文件; PATCH 会把迁移结果固化到 backtest_runs/ 再改
"""
from __future__ import annotations

from collections import Counter

import csv
import io
import json
import os
import re
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.json_safe import finite_float_or_none, json_safe

SCHEMA_VERSION = 1
MAX_RUN_FILE_BYTES = 20 * 1024 * 1024  # 20 MiB
RUN_KINDS: tuple[str, ...] = ("strategy", "factor", "composite")
# 首字符限字母数字, 其后允许 _ -, 长度 1~64; 天然排除 . / \ .. % 等路径成分。
RUN_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{0,63}$")

_RUN_LIST_CACHE_LOCK = threading.Lock()
_RUN_LIST_CACHE: dict[str, tuple[tuple[int | None, int | None], tuple["BacktestRun", ...]]] = {}

_RUN_WRITE_LOCK = threading.RLock()


def _directory_mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None

LEGACY_RUN_CARD_WARNING = (
    "legacy_run_card: 旧 run_card 只读迁移，缺少净值曲线与交易明细"
)

# 列表/比较用的头部指标 (存在才输出)。
HEADLINE_METRICS: tuple[str, ...] = (
    "total_return",
    "annual_return",
    "sharpe",
    "max_drawdown",
    "win_rate",
    "profit_factor",
    "benchmark_return",
    "excess",
)
FACTOR_HEADLINE_METRICS: tuple[str, ...] = ("ic_mean", "ir")

# 可变字段白名单: patch 仅允许改这两个。
MUTABLE_FIELDS = ("favorite", "label")


class RunIdError(ValueError):
    """run_id 不在白名单内 (路径穿越/非法字符)。"""


class RunTooLargeError(ValueError):
    """序列化后超过单文件 20 MiB 上限。"""


class LegacyRunCardReadOnly(RuntimeError):
    """旧 run_card 只读, 不允许该操作 (例如删除)。"""


def check_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise RunIdError(f"invalid run_id: {run_id!r}")
    return run_id


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class RunSubject(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = ""
    name: str = ""
    hash: str = ""


class BacktestRun(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    run_id: str
    kind: Literal["strategy", "factor", "composite"]
    created_at: str = ""
    status: str = "completed"
    subject: RunSubject = Field(default_factory=RunSubject)
    config: dict[str, Any] = Field(default_factory=dict)
    data_snapshot: dict[str, Any] = Field(default_factory=dict)
    benchmark: dict[str, Any] | None = None
    cost_model: dict[str, Any] = Field(default_factory=dict)
    metric_context: dict[str, Any] = Field(default_factory=dict)
    random_seed: int | None = None
    engine_version: str = ""
    stats: dict[str, Any] = Field(default_factory=dict)
    equity_curve: list[dict[str, Any]] = Field(default_factory=list)
    drawdown_curve: list[dict[str, Any]] = Field(default_factory=list)
    benchmark_curve: list[dict[str, Any]] = Field(default_factory=list)
    trades: list[dict[str, Any]] = Field(default_factory=list)
    per_symbol_stats: list[dict[str, Any]] = Field(default_factory=list)
    factor_result: dict[str, Any] | None = None
    attribution: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    # ── 唯二可变字段 ──
    favorite: bool = False
    label: str = ""
    source_run_id: str | None = None


def summarize(run: BacktestRun) -> dict[str, Any]:
    """列表用的轻量摘要, 不携带曲线/交易明细。"""
    cfg = run.config or {}
    subject = run.subject
    headline = {k: run.stats[k] for k in HEADLINE_METRICS if k in run.stats}
    if run.factor_result:
        for key in FACTOR_HEADLINE_METRICS:
            if key in run.factor_result:
                headline[key] = run.factor_result[key]
    symbols = cfg.get("symbols")
    return {
        "run_id": run.run_id,
        "kind": run.kind,
        "status": run.status,
        "created_at": run.created_at,
        "subject": {"id": subject.id, "name": subject.name, "hash": subject.hash},
        "start": cfg.get("start"),
        "end": cfg.get("end"),
        "symbols_count": len(symbols) if isinstance(symbols, list) else None,
        "favorite": run.favorite,
        "label": run.label,
        "source_run_id": run.source_run_id,
        "stats": headline,
        "n_trades": len(run.trades),
        "n_points": len(run.equity_curve),
        "has_factor_result": run.factor_result is not None,
        "has_csv_export": bool(
            run.trades
            or (
                run.factor_result
                and isinstance(run.factor_result.get("group_stats"), list)
                and run.factor_result["group_stats"]
            )
        ),
        "warnings_count": len(run.warnings),
    }


def _scalar_metrics(run: BacktestRun) -> dict[str, float]:
    """stats (含 factor 头部指标) 中的数值标量, 供比较矩阵使用。"""
    out: dict[str, float] = {}
    for source in (run.stats, run.factor_result or {}):
        for key, value in source.items():
            number = finite_float_or_none(value)
            if number is not None and key not in out:
                out[key] = number
    return out


def compare_runs(runs: list[BacktestRun]) -> dict[str, Any]:
    """2~4 个 Run 的指标矩阵、曲线、可比性警告与配置/交易差异。

    差异口径: 第一个 run 为基线 (baseline), 其余每个 run 相对基线输出
    config_diff (递归配置差异) 与 trade_summary (共同/新增/消失交易)。
    """
    runs = list(runs)
    metric_names = sorted({k for r in runs for k in _scalar_metrics(r)})
    metric_matrix = {
        name: {r.run_id: _scalar_metrics(r).get(name) for r in runs}
        for name in metric_names
    }
    curves = [
        {
            "run_id": r.run_id,
            "kind": r.kind,
            "equity_curve": r.equity_curve,
            "benchmark_curve": r.benchmark_curve,
        }
        for r in runs
    ]
    return {
        "runs": [summarize(r) for r in runs],
        "metric_matrix": metric_matrix,
        "curves": curves,
        "warnings": _comparability_warnings(runs),
        "config_diff": _config_diff_section(runs),
        "trade_summary": _trade_summary_section(runs),
    }


# ── 配置差异 (相对 baseline 的递归 diff) ────────────────────

# 单 candidate 配置差异条目 / 每类交易样本的条数上限: 保护大响应, 计数始终完整。
MAX_CONFIG_DIFF_ENTRIES = 200
MAX_TRADE_DIFF_SAMPLES = 20
# 单条差异中 list 值的预览元素上限, 防一条超大 symbols 撑爆响应。
_MAX_LIST_VALUE_ITEMS = 50


def _json_value(value: Any) -> Any:
    """diff 输出值: json_safe + 超长 list 截断为有界预览。"""
    safe = json_safe(value)
    if isinstance(safe, list) and len(safe) > _MAX_LIST_VALUE_ITEMS:
        return safe[:_MAX_LIST_VALUE_ITEMS] + [f"…(+{len(safe) - _MAX_LIST_VALUE_ITEMS})"]
    return safe


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, bool, int, float))


def _values_equal(before: Any, after: Any) -> bool:
    """值相等: 数值按有限浮点语义比较 (5 == 5.0), bool 不与 int 折叠 (True ≠ 1)。"""
    if isinstance(before, bool) or isinstance(after, bool):
        return type(before) is type(after) and before == after
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return finite_float_or_none(before) == finite_float_or_none(after)
    if type(before) is not type(after):
        return False
    try:
        return bool(before == after)
    except Exception:
        return False


def _stable_key(value: Any) -> str:
    return json.dumps(json_safe(value), sort_keys=True, ensure_ascii=False, default=str)


def _scalar_key(value: Any) -> tuple[str, Any]:
    """多重集元素键: 数值归一为有限 float, 其余按 (类型, 值)。"""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("number", finite_float_or_none(value))
    return (type(value).__name__, value)


def _scalar_list_diff(path: str, before: list, after: list, emit) -> None:
    """纯标量 list 按多重集比较 (顺序无关): 元素增删逐条输出; 集合相同视为无差异。"""
    before_counts = Counter(_scalar_key(v) for v in before)
    after_counts = Counter(_scalar_key(v) for v in after)
    for key, count in sorted((before_counts - after_counts).items(), key=lambda kv: _stable_key(kv[0][1])):
        emit(path, "removed", key[1], None, repeat=count)
    for key, count in sorted((after_counts - before_counts).items(), key=lambda kv: _stable_key(kv[0][1])):
        emit(path, "added", None, key[1], repeat=count)


def _config_diff_entries(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """递归配置 diff: 稳定 path 排序, 只输出实际差异。

    - dict 按键名排序递归; 单侧缺失 → added / removed
    - 纯标量 list 按多重集比较 (symbols 等顺序无关); 含结构的 list 按下标递归
    - 标量值不同 → changed
    """
    entries: list[dict[str, Any]] = []

    def emit(path: str, op: str, b: Any, a: Any, repeat: int = 1) -> None:
        for _ in range(repeat):
            entries.append({"path": path, "op": op, "before": _json_value(b), "after": _json_value(a)})

    def walk(b: Any, a: Any, path: str) -> None:
        if isinstance(b, dict) and isinstance(a, dict):
            for key in sorted(set(b) | set(a), key=str):
                child = f"{path}.{key}" if path else str(key)
                if key not in b:
                    emit(child, "added", None, a[key])
                elif key not in a:
                    emit(child, "removed", b[key], None)
                else:
                    walk(b[key], a[key], child)
        elif isinstance(b, list) and isinstance(a, list):
            if all(_scalar(v) for v in b) and all(_scalar(v) for v in a):
                _scalar_list_diff(path, b, a, emit)
                return
            shared = min(len(b), len(a))
            for i in range(shared):
                walk(b[i], a[i], f"{path}[{i}]")
            for i in range(shared, len(b)):
                emit(f"{path}[{i}]", "removed", b[i], None)
            for i in range(shared, len(a)):
                emit(f"{path}[{i}]", "added", None, a[i])
        elif not _values_equal(b, a):
            emit(path or "(root)", "changed", b, a)

    walk(before, after, "")
    entries.sort(key=lambda e: (e["path"], e["op"]))
    return entries


def _config_diff_section(runs: list[BacktestRun]) -> dict[str, Any]:
    """每个 run 相对 baseline (第一个 run) 的递归配置差异, 条目数受限但 total 完整。"""
    baseline = runs[0]
    candidates: list[dict[str, Any]] = []
    for run in runs[1:]:
        entries = _config_diff_entries(baseline.config or {}, run.config or {})
        candidates.append(
            {
                "run_id": run.run_id,
                "total": len(entries),
                "truncated": len(entries) > MAX_CONFIG_DIFF_ENTRIES,
                "entries": entries[:MAX_CONFIG_DIFF_ENTRIES],
            }
        )
    return {"baseline_run_id": baseline.run_id, "candidates": candidates}


# ── 交易差异 (相对 baseline 的共同/新增/消失) ────────────────


def _trade_identity(trade: dict[str, Any]) -> tuple[str, str, str]:
    """共同交易口径: (symbol, entry_date, exit_date) 字符串化, 缺失按空串。"""
    return (
        str(trade.get("symbol") or ""),
        str(trade.get("entry_date") or ""),
        str(trade.get("exit_date") or ""),
    )


def _group_trades(trades: list[Any]) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for trade in trades:
        if isinstance(trade, dict):
            groups.setdefault(_trade_identity(trade), []).append(trade)
    return groups


def _trade_value_differs(b: dict[str, Any], c: dict[str, Any]) -> bool:
    """shares/entry_value/exit_value 任一不同即视为数值口径不同 (仍属共同交易)。"""
    for field in ("shares", "entry_value", "exit_value"):
        if not _values_equal(finite_float_or_none(b.get(field)), finite_float_or_none(c.get(field))):
            return True
    return False


def _trade_sample(trade: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(trade.get("symbol") or "") or None,
        "entry_date": str(trade.get("entry_date") or "") or None,
        "exit_date": str(trade.get("exit_date") or "") or None,
        "shares": finite_float_or_none(trade.get("shares")),
        "entry_value": finite_float_or_none(trade.get("entry_value")),
        "exit_value": finite_float_or_none(trade.get("exit_value")),
        "pnl_pct": finite_float_or_none(trade.get("pnl_pct")),
    }


def _trade_summary_section(runs: list[BacktestRun]) -> dict[str, Any]:
    """每个 run 相对 baseline 的交易集合差异: 计数完整, 样本受限。"""
    baseline = runs[0]
    baseline_groups = _group_trades(baseline.trades or [])
    candidates: list[dict[str, Any]] = []
    for run in runs[1:]:
        groups = _group_trades(run.trades or [])
        common = common_value_diff = added = removed = 0
        common_rows: list[dict[str, Any]] = []
        added_rows: list[dict[str, Any]] = []
        removed_rows: list[dict[str, Any]] = []
        for key in sorted(baseline_groups.keys() | groups.keys()):
            b_list = baseline_groups.get(key, [])
            c_list = groups.get(key, [])
            paired = min(len(b_list), len(c_list))
            common += paired
            removed += len(b_list) - paired
            added += len(c_list) - paired
            for i in range(paired):
                b, c = b_list[i], c_list[i]
                differs = _trade_value_differs(b, c)
                if differs:
                    common_value_diff += 1
                common_rows.append(
                    {
                        "symbol": key[0] or None,
                        "entry_date": key[1] or None,
                        "exit_date": key[2] or None,
                        "value_differs": differs,
                        "baseline": {
                            "shares": finite_float_or_none(b.get("shares")),
                            "entry_value": finite_float_or_none(b.get("entry_value")),
                            "exit_value": finite_float_or_none(b.get("exit_value")),
                            "pnl_pct": finite_float_or_none(b.get("pnl_pct")),
                        },
                        "candidate": {
                            "shares": finite_float_or_none(c.get("shares")),
                            "entry_value": finite_float_or_none(c.get("entry_value")),
                            "exit_value": finite_float_or_none(c.get("exit_value")),
                            "pnl_pct": finite_float_or_none(c.get("pnl_pct")),
                        },
                    }
                )
            removed_rows.extend(_trade_sample(t) for t in b_list[paired:])
            added_rows.extend(_trade_sample(t) for t in c_list[paired:])
        # 共同样本中数值口径不同的优先展示, 其余按 (symbol, entry, exit) 稳定排序。
        common_rows.sort(
            key=lambda r: (not r["value_differs"], r["symbol"] or "", r["entry_date"] or "", r["exit_date"] or "")
        )
        added_rows.sort(key=lambda r: (r["symbol"] or "", r["entry_date"] or "", r["exit_date"] or ""))
        removed_rows.sort(key=lambda r: (r["symbol"] or "", r["entry_date"] or "", r["exit_date"] or ""))
        candidates.append(
            {
                "run_id": run.run_id,
                "n_trades": len(run.trades or []),
                "common": common,
                "common_value_diff": common_value_diff,
                "added": added,
                "removed": removed,
                "samples": {
                    "common": common_rows[:MAX_TRADE_DIFF_SAMPLES],
                    "added": added_rows[:MAX_TRADE_DIFF_SAMPLES],
                    "removed": removed_rows[:MAX_TRADE_DIFF_SAMPLES],
                },
            }
        )
    return {
        "baseline_run_id": baseline.run_id,
        "baseline_n_trades": len(baseline.trades or []),
        "candidates": candidates,
    }


def _comparability_warnings(runs: list[BacktestRun]) -> list[str]:
    warnings: list[str] = []

    def _distinct(getter) -> list[Any]:
        seen: list[Any] = []
        for run in runs:
            value = getter(run)
            if value not in seen:
                seen.append(value)
        return seen

    intervals = _distinct(lambda r: (r.config.get("start"), r.config.get("end")))
    if len(intervals) > 1:
        detail = "; ".join(f"{s}~{e}" for s, e in intervals)
        warnings.append(f"compare.interval_mismatch: 回测区间不同 ({detail})")

    universes = _distinct(
        lambda r: (r.data_snapshot.get("universe_definition") or {}).get("hash")
    )
    if len(universes) > 1:
        warnings.append("compare.universe_mismatch: 股票池快照 hash 不同，横截面口径不一致")

    benchmarks = _distinct(lambda r: _benchmark_identity(r.benchmark))
    if len(benchmarks) > 1 and all(benchmarks):
        warnings.append(
            f"compare.benchmark_mismatch: 基准不同 ({', '.join(str(b) for b in benchmarks)})"
        )

    generations = _distinct(lambda r: r.data_snapshot.get("canonical_generation"))
    if len(generations) > 1:
        warnings.append(
            f"compare.canonical_generation_mismatch: canonical generation 不同 ({', '.join(str(g) for g in generations)})"
        )

    versions = _distinct(lambda r: (r.metric_context or {}).get("version"))
    if len(versions) > 1:
        warnings.append(
            f"compare.metric_version_mismatch: 指标口径版本不同 ({', '.join(str(v) for v in versions)})"
        )

    contexts = _distinct(
        lambda r: (
            (r.metric_context or {}).get("return_frequency", "daily"),
            (r.metric_context or {}).get("periods_per_year", 252),
            (r.metric_context or {}).get("std_ddof", 1),
            float((r.metric_context or {}).get("risk_free_rate", 0.0) or 0.0),
        )
    )
    if len(contexts) > 1:
        warnings.append("compare.metric_context_mismatch: 收益频率、年化周期、ddof 或无风险收益口径不同")

    cost_models = _distinct(lambda r: json_safe(r.cost_model or {}))
    if len(cost_models) > 1:
        warnings.append("compare.cost_model_mismatch: 手续费、滑点或成本口径不同")

    engine_versions = _distinct(lambda r: r.engine_version or "")
    if len(engine_versions) > 1:
        warnings.append("compare.engine_version_mismatch: 回测引擎版本不同")

    curve_semantics = _distinct(
        lambda r: (
            r.config.get("mode", "position"),
            (r.stats or {}).get("full_kind", "portfolio"),
        )
    )
    if len(curve_semantics) > 1:
        warnings.append("compare.curve_semantics_mismatch: 账户净值与候选样本复利曲线口径不同，不应直接比较")
    return warnings


def _benchmark_identity(benchmark: dict[str, Any] | None) -> str | None:
    if not benchmark:
        return None
    return str(benchmark.get("symbol") or json_safe(benchmark))


def export_csv(run: BacktestRun) -> tuple[str, bytes]:
    """CSV 导出: 优先 trades; 因子 run 无 trades 时导出 group_stats。

    返回 (明细名, UTF-8 bytes)。无可导出数据时抛 ValueError。
    """
    rows = run.trades
    name = "trades"
    if not rows and run.factor_result:
        rows = run.factor_result.get("group_stats") or []
        name = "group_stats"
    if not rows:
        raise ValueError("run 内没有可导出的 trades/group_stats 明细")

    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)

    def _cell(value: Any) -> Any:
        if value is None:
            return ""
        if isinstance(value, (list, dict)):
            return json.dumps(json_safe(value), ensure_ascii=False)
        return value

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _cell(json_safe(row.get(key))) for key in fieldnames})
    return name, buf.getvalue().encode("utf-8")


class BacktestRunStore:
    """backtest_runs 目录的读写入口; 兼并旧 run_cards 只读迁移。"""

    def __init__(self, data_dir: Path | str) -> None:
        from app.services.research_registry import ResearchStore

        self.run_dir = Path(data_dir) / "research" / "backtest_runs"
        # 旧 run_cards 路径统一由 ResearchStore 持有, 迁移只读不写。
        self._legacy_store = ResearchStore(data_dir)
        self._legacy_card_dir = self._legacy_store.card_dir

    def _list_cache_key(self) -> str:
        return f"{self.run_dir.resolve()}|{self._legacy_card_dir.resolve()}"

    def _list_cache_signature(self) -> tuple[int | None, int | None]:
        return (
            _directory_mtime_ns(self.run_dir),
            _directory_mtime_ns(self._legacy_card_dir),
        )

    def _invalidate_list_cache(self) -> None:
        with _RUN_LIST_CACHE_LOCK:
            _RUN_LIST_CACHE.pop(self._list_cache_key(), None)

    # ── 持久化 ──────────────────────────────────────────────

    def _path(self, run_id: str) -> Path:
        return self.run_dir / f"{check_run_id(run_id)}.json"

    def serialize(self, run: BacktestRun) -> bytes:
        """规范序列化: 非有限数值转 null + 体积上限检查。"""
        check_run_id(run.run_id)
        encoded = json.dumps(
            json_safe(run.model_dump()),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_RUN_FILE_BYTES:
            raise RunTooLargeError(
                f"run {run.run_id} 序列化后 {len(encoded)} 字节，超过 {MAX_RUN_FILE_BYTES} 上限"
            )
        return encoded

    def save(self, run: BacktestRun) -> BacktestRun:
        """创建不可变 Run；同一 run_id 已存在时拒绝覆盖。"""
        return self._write(run, replace=False)

    def _write(self, run: BacktestRun, *, replace: bool) -> BacktestRun:
        data = self.serialize(run)
        with _RUN_WRITE_LOCK:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            target = self._path(run.run_id)
            fd, tmp_name = tempfile.mkstemp(dir=self.run_dir, prefix=f".{run.run_id}.", suffix=".tmp")
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                if replace:
                    os.replace(tmp_path, target)
                else:
                    os.link(tmp_path, target)
                    tmp_path.unlink()
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
            self._invalidate_list_cache()
        return run

    # ── 读取 ────────────────────────────────────────────────

    def exists(self, run_id: str) -> bool:
        return self._path(run_id).exists()

    def get(self, run_id: str) -> BacktestRun:
        path = self._path(run_id)
        if not path.exists():
            # 只读迁移: 旧 run_card 可直接读取
            card = self._legacy_card(run_id)
            if card is not None:
                return card
            raise KeyError(run_id)
        try:
            return BacktestRun.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, TypeError, ValueError) as exc:
            # 损坏/旧 schema 的单个文件不能把读取端点变成 500。
            raise KeyError(run_id) from exc

    def list_runs(
        self,
        *,
        kind: str | None = None,
        favorite: bool | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """轻量摘要列表; 含旧 run_card 迁移项 (同 run_id 以新契约为准)。"""
        runs = self._all_runs()
        if kind is not None:
            runs = [r for r in runs if r.kind == kind]
        if favorite is not None:
            runs = [r for r in runs if r.favorite == favorite]
        q = (query or "").strip().lower()
        if q:
            runs = [r for r in runs if q in _search_text(r)]
        total = len(runs)
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        return {
            "items": [summarize(r) for r in runs[offset : offset + limit]],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def _all_runs(self) -> list[BacktestRun]:
        cache_key = self._list_cache_key()
        signature_before = self._list_cache_signature()
        with _RUN_LIST_CACHE_LOCK:
            cached = _RUN_LIST_CACHE.get(cache_key)
            if cached is not None and cached[0] == signature_before:
                return list(cached[1])

        runs: dict[str, BacktestRun] = self._legacy_runs()
        if self.run_dir.exists():
            for path in sorted(self.run_dir.glob("*.json")):
                run_id = path.stem
                try:
                    check_run_id(run_id)
                    runs[run_id] = BacktestRun.model_validate(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                except (RunIdError, ValueError, json.JSONDecodeError, OSError):
                    continue
        ordered = tuple(sorted(runs.values(), key=lambda r: (r.created_at, r.run_id), reverse=True))
        # 扫描期间发生写入 (save/patch/delete 会先失效缓存, 但可能早于本次
        # 存入) 时, 目录签名已变化: 不缓存旧内容, 下次调用重扫拿到新事实。
        with _RUN_LIST_CACHE_LOCK:
            if self._list_cache_signature() == signature_before:
                _RUN_LIST_CACHE[cache_key] = (signature_before, ordered)
        return list(ordered)

    # ── 可变字段 (favorite/label) ───────────────────────────

    def patch(self, run_id: str, *, favorite: bool | None = None, label: str | None = None) -> BacktestRun:
        check_run_id(run_id)
        with _RUN_WRITE_LOCK:
            existing = self.exists(run_id)
            if existing:
                run = self.get(run_id)
            else:
                card = self._legacy_card(run_id)
                if card is None:
                    raise KeyError(run_id)
                # 迁移固化: 把旧 run_card 落成新契约文件后仅改可变字段, 原文件不动。
                run = card
            if favorite is not None:
                run.favorite = favorite
            if label is not None:
                run.label = label
            return self._write(run, replace=existing)

    def delete(self, run_id: str) -> bool:
        """仅删除明确的 backtest_runs/{run_id}.json; 旧 run_card 永不删除。"""
        path = self._path(run_id)
        with _RUN_WRITE_LOCK:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            else:
                self._invalidate_list_cache()
                return True
            if self._legacy_card(run_id) is not None:
                raise LegacyRunCardReadOnly(
                    f"run {run_id} 来自旧 run_card (只读迁移)，不删除原文件"
                )
            return False

    # ── 旧 run_card 只读迁移 ────────────────────────────────

    def _legacy_card(self, run_id: str) -> BacktestRun | None:
        try:
            check_run_id(run_id)
        except RunIdError:
            return None
        path = self._legacy_card_dir / f"{run_id}.json"
        if not path.exists():
            return None
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(card, dict) or card.get("kind") not in RUN_KINDS:
            return None
        # 文件名已通过白名单，是唯一可寻址的 run_id；不可相信卡内历史字段。
        card["run_id"] = run_id
        try:
            return self._card_to_run(card)
        except (TypeError, ValueError):
            return None

    def _legacy_runs(self) -> dict[str, BacktestRun]:
        """按已验证的文件名枚举旧卡，避免卡内 run_id 伪造或错配。"""
        if not self._legacy_card_dir.exists():
            return {}
        out: dict[str, BacktestRun] = {}
        for path in sorted(self._legacy_card_dir.glob("*.json")):
            run_id = path.stem
            try:
                check_run_id(run_id)
            except RunIdError:
                continue
            run = self._legacy_card(run_id)
            if run is not None:
                out[run_id] = run
        return out

    @staticmethod
    def _card_to_run(card: dict[str, Any]) -> BacktestRun:
        raw_config = card.get("config")
        config = raw_config if isinstance(raw_config, dict) else {}
        raw_stats = card.get("stats")
        stats = raw_stats if isinstance(raw_stats, dict) else {}
        kind = str(card["kind"])
        subject_id = str(
            config.get("strategy_id") or config.get("factor_name") or card.get("run_id") or ""
        )
        metric_context = stats.get("metric_context")
        return BacktestRun(
            run_id=str(card.get("run_id") or ""),
            kind=kind,
            created_at=str(card.get("created_at") or ""),
            status="legacy",
            subject=RunSubject(
                id=subject_id,
                name=subject_id,
                hash=str(card.get("strategy_hash") or card.get("config_hash") or ""),
            ),
            config=config,
            cost_model={
                k: config.get(k)
                for k in ("fees_pct", "slippage_bps", "entry_fill", "exit_fill", "matching")
                if config.get(k) is not None
            },
            metric_context=metric_context if isinstance(metric_context, dict) else {},
            stats=stats,
            warnings=[LEGACY_RUN_CARD_WARNING],
        )


def _search_text(run: BacktestRun) -> str:
    cfg = run.config or {}
    return " ".join(
        (
            run.run_id,
            run.kind,
            run.status,
            run.subject.id,
            run.subject.name,
            run.label,
            str(cfg.get("strategy_id") or ""),
            str(cfg.get("factor_name") or ""),
        )
    ).lower()
