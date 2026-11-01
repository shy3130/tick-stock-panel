"""回测输入指纹及有限次时间因果检查。只在正式回测 worker 中启用。

检查固定本次输入、参数和预热起点，比较截断/扰动未来后的历史输出；
它不能证明上游供应商数据本身满足 point-in-time，也不替代样本外验证。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.backtest.matrix import MarketDataMatrix, SignalMatrix, slice_market_data_matrix

_SAMPLES = 3
_EXAMPLES = 5
_RUN_ID = re.compile(r"[0-9a-f]{10,32}\Z")
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024


def json_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, allow_nan=False).encode()
    ).hexdigest()


def _array_digest(array: np.ndarray) -> str:
    digest = hashlib.sha256()
    digest.update(str(array.dtype).encode())
    digest.update(str(array.shape).encode())
    # memmap 和非连续视图分块处理，避免为整个回测矩阵再复制一份。
    for start in range(0, len(array), 256):
        block = np.ascontiguousarray(array[start:start + 256])
        digest.update(memoryview(block).cast("B"))
    return digest.hexdigest()


def fingerprint_matrix(market: MarketDataMatrix) -> dict:
    fields = {
        name: _array_digest(getattr(market, name))
        for name in (
            "timestamps", "session_ids", "open", "high", "low", "close", "volume",
            "tradable", "limit_up_locked", "limit_down_locked",
        )
    }
    fields.update({f"field:{key}": _array_digest(value) for key, value in sorted(market.fields.items())})
    identity = {"timestamps": market.timestamp_labels, "symbols": market.symbols, "names": market.names}
    return {
        "sha256": json_digest({"identity": identity, "fields": fields}),
        "fields": fields,
        "rows": market.shape[0] * market.shape[1],
        "symbols": len(market.symbols),
        "start": market.timestamp_labels[0] if market.timestamp_labels else None,
        "end": market.timestamp_labels[-1] if market.timestamp_labels else None,
    }


def fingerprint_frame(frame: pl.DataFrame) -> dict:
    keys = [name for name in ("symbol", "date", "datetime") if name in frame.columns]
    # 公告/股本表同一标的有多行；补齐排序键，避免物理行序变化被误判为内容变化。
    frame = frame.sort(keys + sorted(set(frame.columns) - set(keys)))
    fields = {}
    for name in sorted(frame.columns):
        digest = hashlib.sha256(str(frame.schema[name]).encode())
        for block in frame.select(name).iter_slices(65536):
            buffer = io.BytesIO()
            block.rechunk().write_ipc(buffer)
            digest.update(buffer.getbuffer())
        fields[name] = digest.hexdigest()
    time_col = next((col for col in ("datetime", "date") if col in frame.columns), None)
    return {
        "sha256": json_digest({"rows": frame.height, "fields": fields}),
        "fields": fields,
        "rows": frame.height,
        "symbols": frame["symbol"].n_unique() if "symbol" in frame.columns else None,
        "start": str(frame[time_col].min()) if time_col and frame.height else None,
        "end": str(frame[time_col].max()) if time_col and frame.height else None,
    }


def _cutoffs(values: list) -> list:
    if not values:
        return []
    indices = np.linspace(0, len(values) - 1, min(_SAMPLES, len(values)), dtype=int)
    return [values[int(i)] for i in indices]


def _scalar(value):
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def _different(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    if left.shape != right.shape:
        raise ValueError("重算输出形状与基线不一致")
    if left.dtype.kind in "iufb" and right.dtype.kind in "iufb":
        return ~np.isclose(left, right, rtol=1e-6, atol=1e-7, equal_nan=True)
    return ~((left == right) | ((left == None) & (right == None)))  # noqa: E711


def _report(checks: list[dict]) -> dict:
    differences = sum(c.get("differences", 0) for c in checks)
    status = (
        "error" if any(c["kind"] == "repeat" and c["status"] == "error" for c in checks)
        else "failed" if differences else "error" if any(c["status"] == "error" for c in checks)
        else "passed" if any(c.get("compared_values", 0) > 0 for c in checks) else "insufficient"
    )
    return {"status": status, "checks": checks, "differences": differences}


def _check(kind: str, cutoff, compare: Callable[[], dict]) -> dict:
    try:
        return {"kind": kind, "cutoff": str(cutoff), "status": "completed", **compare()}
    except Exception as exc:  # noqa: BLE001 — 用户策略异常必须转为可见的检查失败。
        return {
            "kind": kind, "cutoff": str(cutoff), "status": "error",
            "error": f"重算未完成（{type(exc).__name__}）",
            "differences": 0, "compared_values": 0, "examples": [],
        }


def _perturb_matrix(market: MarketDataMatrix, cutoff: int) -> MarketDataMatrix:
    def change(array):
        if array.ndim != 2 or array.dtype.kind not in "fiu":
            return array
        values = array.copy()
        scale = 1.5 + np.arange(len(values) - cutoff - 1)[:, None] * 0.03
        future = values[cutoff + 1:]
        values[cutoff + 1:] = (future * scale + (future == 0)).astype(array.dtype)
        values.flags.writeable = False
        return values

    return replace(
        market,
        **{name: change(getattr(market, name)) for name in ("open", "high", "low", "close", "volume")},
        fields={name: change(value) for name, value in market.fields.items()},
        _valid_bars=None,
    )


def audit_matrix(
    market: MarketDataMatrix,
    baseline: SignalMatrix,
    evaluate: Callable[[MarketDataMatrix], SignalMatrix],
    *, start: str, end: str, cancel_event=None,
) -> dict:
    candidates = [
        i for i, label in enumerate(market.timestamp_labels[:-1])
        if start <= label[:10] <= end
    ]
    checks = []

    def compare(kind, cutoff):
        label = market.timestamp_labels[cutoff]
        if kind == "repeat":
            data = market
        elif kind == "truncate":
            data = slice_market_data_matrix(market, 0, cutoff + 1)
        else:
            data = _perturb_matrix(market, cutoff)
        actual = evaluate(data)
        if actual.shape != data.shape:
            raise ValueError("重算信号形状不匹配")
        rows = np.array([start <= t[:10] <= label[:10] for t in market.timestamp_labels[:cutoff + 1]])
        count, compared, examples = 0, 0, []
        for name in ("entry", "exit", "score", "entry_signal_code", "exit_signal_code"):
            expected = getattr(baseline, name)[:cutoff + 1]
            observed = getattr(actual, name)[:cutoff + 1]
            diff = _different(expected, observed) & rows[:, None]
            count += int(diff.sum())
            compared += int(rows.sum()) * market.shape[1]
            for t, a in np.argwhere(diff)[:max(0, _EXAMPLES - len(examples))]:
                examples.append({
                    "date": market.timestamp_labels[t], "symbol": market.symbols[a],
                    "field": name, "baseline": _scalar(expected[t, a]),
                    "recomputed": _scalar(observed[t, a]), "cutoff": label,
                })
        return {"differences": count, "compared_values": compared, "examples": examples}

    for cutoff in _cutoffs(candidates):
        label = market.timestamp_labels[cutoff]
        for kind in ("truncate", "perturb"):
            if cancel_event is not None and cancel_event.is_set():
                return {**_report(checks), "status": "cancelled"}

            checks.append(_check(kind, label, lambda kind=kind, cutoff=cutoff: compare(kind, cutoff)))
    if any(check["differences"] for check in checks):
        # 先排除非确定性/带状态策略，避免把随机变化直接解释为未来数据依赖。
        kind = "repeat"
        cutoff = max(i for i, label in enumerate(market.timestamp_labels) if start <= label[:10] <= end)
        label = market.timestamp_labels[cutoff]
        control = _check(kind, label, lambda: compare("repeat", cutoff))
        if control["differences"]:
            control.update(status="error", error="相同输入重算结果不一致，无法归因于未来数据")
        checks.append(control)
    return _report(checks)


def _perturb_frame(frame: pl.DataFrame, time_col: str, cutoff) -> pl.DataFrame:
    future = pl.col(time_col) > cutoff
    return frame.with_columns([
        pl.when(future).then(
            pl.col(name) * (1.5 + pl.col(time_col).rank("dense") * 0.03) + (pl.col(name) == 0)
        )
        .otherwise(pl.col(name)).cast(dtype).alias(name)
        for name, dtype in frame.schema.items()
        if dtype.is_numeric() and name != time_col
    ])


def _compare_frames(baseline, actual, time_col, start, cutoff):
    keys = ["symbol", time_col]
    bounds = (pl.col(time_col) >= start) & (pl.col(time_col) <= cutoff)
    left, right = baseline.filter(bounds), actual.filter(bounds)
    if left.select(keys).is_duplicated().any() or right.select(keys).is_duplicated().any():
        raise ValueError("检查输出包含重复时间和标的")
    names = [name for name, dtype in left.schema.items() if name not in keys and (dtype.is_numeric() or dtype == pl.Boolean)]
    if any(name not in right.columns for name in names):
        raise ValueError("重算输出缺少字段")
    # full join 能发现消失或新增的历史信号，而不只检查两边都存在的行。
    joined = (
        left.select(*keys, *names).with_columns(pl.lit(True).alias("_present"))
        .join(
            right.select(*keys, *names).with_columns(pl.lit(True).alias("_present")),
            on=keys, how="full", coalesce=True, suffix="__audit",
        ).sort(keys)
    )
    count, examples = 0, []
    for name in ["_present", *names]:
        expected, observed = joined[name].to_numpy(), joined[f"{name}__audit"].to_numpy()
        diff = _different(expected, observed)
        count += int(diff.sum())
        for row in np.flatnonzero(diff)[:max(0, _EXAMPLES - len(examples))]:
            examples.append({
                "date": str(joined[time_col][int(row)]), "symbol": joined["symbol"][int(row)],
                "field": "signal_presence" if name == "_present" else name,
                "baseline": _scalar(expected[row]), "recomputed": _scalar(observed[row]),
                "cutoff": str(cutoff),
            })
    return {"differences": count, "compared_values": joined.height * (len(names) + 1), "examples": examples}


def audit_frame(
    raw: pl.DataFrame, baseline: pl.DataFrame, evaluate: Callable[[pl.DataFrame], pl.DataFrame],
    *, start, end, time_col: str = "date", cancel_event=None,
) -> dict:
    times = raw[time_col].unique().sort().to_list()
    if times and isinstance(start, str) and not isinstance(times[0], str):
        start = type(times[0]).fromisoformat(start)
        end = type(times[0]).fromisoformat(end)
    candidates = [t for t in times[:-1] if start <= t <= end]
    checks = []

    def compare(kind, cutoff):
        if kind == "repeat":
            variant = raw
        elif kind == "truncate":
            variant = raw.filter(pl.col(time_col) <= cutoff)
        else:
            variant = _perturb_frame(raw, time_col, cutoff)
        return _compare_frames(baseline, evaluate(variant), time_col, start, cutoff)

    for cutoff in _cutoffs(candidates):
        for kind in ("truncate", "perturb"):
            if cancel_event is not None and cancel_event.is_set():
                return {**_report(checks), "status": "cancelled"}

            checks.append(_check(kind, cutoff, lambda kind=kind, cutoff=cutoff: compare(kind, cutoff)))
    if any(check["differences"] for check in checks):
        kind, cutoff = "repeat", end
        control = _check(kind, cutoff, lambda: compare("repeat", cutoff))
        if control["differences"]:
            control.update(status="error", error="相同输入重算结果不一致，无法归因于未来数据")
        checks.append(control)
    return _report(checks)


def _manifest_path(data_dir: Path, run_id: str) -> Path:
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("回测编号无效")
    root = Path(data_dir).resolve()
    path = root / "backtest_runs" / f"{run_id}.json"
    if path.resolve() != path:
        raise ValueError("回测记录目录无效")
    return path


def save_evidence(data_dir: Path, evidence: dict) -> None:
    from app.services.fs_utils import atomic_write_text

    path = _manifest_path(data_dir, evidence["run_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(evidence, ensure_ascii=False, sort_keys=True, allow_nan=False, default=str)
    if len(content.encode()) > _MAX_MANIFEST_BYTES:
        raise ValueError("回测版本记录超过大小限制")
    # 先占用同编号写锁，防止两个 worker 的检查与原子替换之间发生覆盖。
    lock_path = path.with_suffix(".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        if path.exists():
            raise FileExistsError("回测版本记录已存在")
        atomic_write_text(path, content)
    finally:
        lock_path.unlink(missing_ok=True)


def load_evidence(data_dir: Path, run_id: str) -> dict:
    path = _manifest_path(data_dir, run_id)
    if path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ValueError("回测版本记录超过大小限制")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("run_id") != run_id:
        raise ValueError("回测版本记录无效")
    return value


def audit_trade_timing(trades: list[dict], config: dict, backend: str) -> dict:
    """检查已记录的信号日与成交日，不把期末估值记录当成次日信号成交。"""
    differences, compared, examples = 0, 0, []
    for trade in trades:
        for side in ("entry", "exit"):
            signal = trade.get(f"{side}_signal_date")
            fill = trade.get(f"{side}_date")
            if not signal or not fill:
                continue
            signal, fill = str(signal)[:10], str(fill)[:10]
            next_open = config.get(f"{side}_fill") == "open_t+1"
            if side == "entry" and backend == "minute_filter":
                next_open = False  # 分钟策略按触发分钟入场，忽略日线 entry_fill。
            if side == "exit" and trade.get("exit_reason") != "signal":
                next_open = False
            compared += 1
            if signal > fill or (next_open and signal == fill):
                differences += 1
                if len(examples) < _EXAMPLES:
                    examples.append({
                        "symbol": trade["symbol"], "side": side,
                        "signal_date": signal, "fill_date": fill,
                    })
    return {
        "status": "failed" if differences else "passed" if compared else "insufficient",
        "compared_values": compared, "differences": differences, "examples": examples,
    }


def definition_versions(data_dir: Path, strategy) -> dict:
    """只读取策略及定义目录；绝不扫描偏好、密钥或环境变量。"""
    files = {}
    if strategy.file_path is not None:
        files["strategy"] = _file_digest(Path(strategy.file_path))
    for directory in ("custom_factors", "custom_signals", "position_policies"):
        root = Path(data_dir) / "user_data" / directory
        for path in sorted(root.glob("*")):
            if path.is_file() and path.suffix in {".json", ".py"}:
                files[f"{directory}/{path.name}"] = _file_digest(path)
    return {"sha256": json_digest(files), "files": files}


def _file_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def runtime_version() -> dict:
    from app import __version__

    root = Path(__file__).resolve().parents[1]
    paths = [
        "backtest/evidence.py", "backtest/engine.py", "backtest/strategy.py",
        "backtest/matrix.py", "backtest/fundamentals.py", "backtest/minute_replay.py",
        "strategy/engine.py", "strategy/scoring.py", "indicators/pipeline.py",
    ]
    files = {
        name: _file_digest(root / name)  # 冻结发行版可能只携带字节码。
        for name in paths
    }
    return {
        "application": __version__, "python": sys.version.split()[0],
        "polars": pl.__version__, "numpy": np.__version__,
        "code_sha256": json_digest(files), "code_files": files,
    }


class EvidenceRecorder:
    """一次回测专属的观察器，不进入共享缓存，也不保存全量行情副本。"""

    def __init__(self, data_dir: Path, config, strategy, strategy_engine, cancel_event=None):
        self.data_dir = data_dir
        self.config = config
        self.strategy = strategy
        self.cancel_event = cancel_event
        self.datasets: dict[str, dict] = {}
        self.outputs: dict[str, dict] = {}
        self.reports: list[dict] = []
        self.raw_panel: pl.DataFrame | None = None
        self.prepare_panel: Callable | None = None
        self.definitions = definition_versions(data_dir, strategy)
        self.runtime = runtime_version()
        self.children = []
        if strategy.composite is not None:
            for child in strategy.composite.children:
                child_def = strategy_engine.get(child.strategy_id)
                self.children.append({
                    "id": child.strategy_id, "weight": child.weight,
                    "definitions": definition_versions(data_dir, child_def),
                })

    def record_frame(self, name: str, frame: pl.DataFrame) -> None:
        self.datasets[name] = fingerprint_frame(frame)

    def capture_panel(self, raw, prepare, auxiliaries) -> None:
        self.raw_panel, self.prepare_panel = raw, prepare
        self.record_frame("daily_base", raw)
        for name, frame in auxiliaries.items():
            if frame is not None:
                self.record_frame(name, frame)

    def check_matrix(self, market, signals, evaluate) -> None:
        self.datasets["daily_matrix"] = fingerprint_matrix(market)
        self.reports.append({
            "scope": "daily_signals",
            **audit_matrix(
                market, signals, evaluate, start=str(self.config.start), end=str(self.config.end),
                cancel_event=self.cancel_event,
            ),
        })

    def check_panel(self, baseline, evaluate) -> None:
        self.outputs["daily_features_and_signals"] = fingerprint_frame(baseline)
        if self.raw_panel is None or self.prepare_panel is None:
            self.reports.append({"scope": "daily_signals", "status": "error", "checks": [], "differences": 0})
            return
        self.reports.append({
            "scope": "daily_features_and_signals",
            **audit_frame(
                self.raw_panel, baseline, lambda raw: evaluate(self.prepare_panel(raw)),
                start=self.config.start, end=self.config.end, cancel_event=self.cancel_event,
            ),
        })
        # 检查完成后释放对大面板及准备上下文的引用。
        self.raw_panel, self.prepare_panel = None, None

    def check_minute(self, history, result, evaluate) -> None:
        dtype = history.schema["datetime"]

        def decisions(value):
            rows = []
            for row in value.rows:
                timestamp = row.get("last_datetime")
                if timestamp is None:
                    raise ValueError("分钟信号缺少触发时间，无法检查")
                if isinstance(timestamp, str):
                    timestamp = datetime.fromisoformat(timestamp)
                rows.append({
                    "symbol": str(row["symbol"]), "datetime": timestamp,
                    "close": row.get("close"), "score": value.scores.get(str(row["symbol"]), 0.0),
                })
            return pl.DataFrame(rows, schema={
                "symbol": pl.Utf8, "datetime": dtype, "close": pl.Float64, "score": pl.Float64,
            })

        try:
            report = audit_frame(
                history, decisions(result), lambda variant: decisions(evaluate(variant)),
                start=history["datetime"].min(), end=history["datetime"].max(),
                time_col="datetime", cancel_event=self.cancel_event,
            )
        except (ValueError, TypeError, pl.exceptions.PolarsError):
            report = {"status": "error", "checks": [], "differences": 0}
        self.reports.append({"scope": "minute_signals", **report})

    @staticmethod
    def minute_sample_days(days):
        return set(_cutoffs(days))

    def finish(self, result, effective_config: dict) -> dict:
        checks = [check for report in self.reports for check in report["checks"]]
        summary = _report(checks)
        if any(r["status"] == "cancelled" for r in self.reports):
            summary["status"] = "cancelled"
        elif any(r["status"] == "error" for r in self.reports) and summary["status"] != "failed":
            summary["status"] = "error"
        elif any(r["status"] == "insufficient" for r in self.reports) and summary["status"] == "passed":
            summary["status"] = "partial"
        definitions_after = definition_versions(self.data_dir, self.strategy)
        data_digest = json_digest({key: value["sha256"] for key, value in sorted(self.datasets.items())})
        config_digest = json_digest(effective_config)
        strategy = {
            "id": self.config.strategy_id, "backend": self.strategy.execution_backend,
            "meta": self.strategy.meta, "definitions": self.definitions, "children": self.children,
            "changed_during_run": definitions_after != self.definitions,
        }
        strategy["sha256"] = json_digest(strategy)
        runtime = self.runtime
        return {
            "schema_version": 1, "run_id": result.run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "runtime": runtime,
            "config": effective_config, "config_sha256": config_digest,
            "strategy": strategy,
            "data": {
                "sha256": data_digest, "origin": "local_normalized_snapshot",
                "asset_type": self.config.asset_type, "daily_price_basis": "qfq",
                "datasets": self.datasets,
            },
            "reproducibility_sha256": json_digest({
                "data": data_digest, "strategy": strategy["sha256"], "config": config_digest, "runtime": runtime,
            }),
            "lookahead": {**summary, "reports": self.reports, "sampled": True},
            "execution_timing": audit_trade_timing(result.trades, effective_config, self.strategy.execution_backend),
            "outputs": self.outputs,
            "result_summary": {
                key: _scalar(result.stats.get(key)) for key in (
                    "total_return", "annual_return", "max_drawdown", "sharpe", "win_rate", "n_trades",
                )
            },
            "limitations": [
                "按交易时间抽样检查本次加载的输入、特征和信号，不是全时点无未来数据证明。",
                "供应商历史修订、历史股票池及静态维表的时点正确性需另行核实。",
                "记录内容指纹和有效参数，不归档全量行情；原始数据被覆盖后不能仅凭指纹还原。",
            ],
        }
