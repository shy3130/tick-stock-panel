"""量化研究候选方案的轻量本地存储。"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

CandidateKind = Literal["factor", "strategy", "auction"]
CandidateStatus = Literal["pending", "validated", "rejected"]

MAX_CANDIDATES = 200
MAX_NAME_LENGTH = 80
MAX_PAYLOAD_BYTES = 32 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024

_MINING_SOURCE_CONFIG_FIELDS = frozenset(
    {
        "origin_run_id",
        "candidate_signature",
        "regime_state",
        "algorithm_version",
        "methodology_version",
    }
)
_CONFIG_FIELDS: dict[str, frozenset[str]] = {
    "factor": frozenset(
        {
            "factor_name",
            "symbols",
            "start",
            "end",
            "n_groups",
            "rebalance",
            "weight",
            "fees_pct",
            "slippage_bps",
            "asset_type",
        }
    )
    | _MINING_SOURCE_CONFIG_FIELDS,
    "strategy": frozenset(
        {
            "strategy_id",
            "symbols",
            "start",
            "end",
            "params",
            "overrides",
            "matching",
            "entry_fill",
            "exit_fill",
            "fees_pct",
            "commission_pct",
            "stamp_tax_pct",
            "slippage_bps",
            "max_positions",
            "max_exposure_pct",
            "initial_capital",
            "position_sizing",
            "mode",
            "holding_days",
            "asset_type",
            "minute_fill",
            "regime_filter",
            "factor_names",
            "directions",
            "weights",
        }
    )
    | _MINING_SOURCE_CONFIG_FIELDS,
    "auction": frozenset(
        {
            "trade_date",
            "as_of_ms",
            "style",
            "symbols",
            "limit",
        }
    ),
}
_MINING_METRIC_FIELDS = frozenset(
    {
        "oos_sharpe",
        "oos_return",
        "oos_max_drawdown",
        "oos_positive_fold_ratio",
        "oos_n_trades",
        "valid_folds",
        "skipped_folds",
        "confidence",
        "coverage",
        "turnover",
        "long_short_sharpe",
    }
)
_METRIC_FIELDS: dict[str, frozenset[str]] = {
    "factor": frozenset(
        {
            "ic_mean",
            "ic_std",
            "ir",
            "ic_win_rate",
            "long_short_return",
            "long_short_max_drawdown",
            "n_symbols",
            "n_dates",
            "elapsed_ms",
        }
    )
    | _MINING_METRIC_FIELDS,
    "strategy": frozenset(
        {
            "total_return",
            "annual_return",
            "max_drawdown",
            "sharpe",
            "sortino",
            "win_rate",
            "n_trades",
            "profit_factor",
            "avg_return",
            "median_return",
            "elapsed_ms",
        }
    )
    | _MINING_METRIC_FIELDS,
    "auction": frozenset(
        {
            "coverage_1d",
            "coverage_5d",
            "coverage_10d",
            "coverage_20d",
            "average_return_1d",
            "average_return_5d",
            "average_return_10d",
            "average_return_20d",
            "hit_rate_1d",
            "hit_rate_5d",
            "hit_rate_10d",
            "hit_rate_20d",
        }
    ),
}
_lock = threading.RLock()


class CandidateStoreError(RuntimeError):
    pass


class CandidateValidationError(CandidateStoreError):
    pass


class CandidateStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "user_data" / "research_candidates.json"

    def list(self) -> list[dict[str, Any]]:
        with _lock:
            return self._load()

    def create(
        self,
        *,
        kind: CandidateKind,
        name: str,
        source_id: str,
        config: dict[str, Any],
        metrics: dict[str, Any],
        data_as_of: str | None,
        status: CandidateStatus = "pending",
    ) -> dict[str, Any]:
        clean_name = self._validate_name(name)
        clean_source_id = source_id.strip()
        if not clean_source_id or len(clean_source_id) > 120:
            raise CandidateValidationError("候选来源标识不能为空且不能超过 120 个字符")
        clean_config = self._validate_config(kind, config)
        clean_metrics = self._validate_metrics(kind, metrics)

        with _lock:
            items = self._load()
            if len(items) >= MAX_CANDIDATES:
                raise CandidateValidationError(f"候选方案最多保存 {MAX_CANDIDATES} 个")
            now = datetime.now(UTC).isoformat()
            item = {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "name": clean_name,
                "source_id": clean_source_id,
                "config": clean_config,
                "metrics": clean_metrics,
                "data_as_of": data_as_of,
                "status": status,
                "created_at": now,
                "updated_at": now,
            }
            items.insert(0, item)
            self._write(items)
            return item

    def create_or_get_by_provenance(
        self,
        *,
        origin_run_id: str,
        candidate_signature: str,
        kind: CandidateKind,
        name: str,
        source_id: str,
        config: dict[str, Any],
        metrics: dict[str, Any],
        data_as_of: str | None,
        status: CandidateStatus = "pending",
    ) -> dict[str, Any]:
        """Atomically return or create one item for a mining run candidate."""
        clean_name = self._validate_name(name)
        clean_source_id = source_id.strip()
        if not clean_source_id or len(clean_source_id) > 120:
            raise CandidateValidationError("候选来源标识不能为空且不能超过 120 个字符")
        clean_config = self._validate_config(kind, config)
        clean_metrics = self._validate_metrics(kind, metrics)
        if (
            clean_config.get("origin_run_id") != origin_run_id
            or clean_config.get("candidate_signature") != candidate_signature
        ):
            raise CandidateValidationError("候选来源与配置中的挖掘溯源不一致")

        with _lock:
            items = self._load()
            for item in items:
                item_config = item.get("config") or {}
                if (
                    item_config.get("origin_run_id") == origin_run_id
                    and item_config.get("candidate_signature") == candidate_signature
                ):
                    if (
                        item.get("kind") == kind
                        and item.get("source_id") == clean_source_id
                        and item_config == clean_config
                        and item.get("metrics") == clean_metrics
                        and item.get("data_as_of") == data_as_of
                    ):
                        return item
                    raise CandidateValidationError(
                        "相同挖掘溯源的候选内容冲突, 已停止覆盖"
                    )
            if len(items) >= MAX_CANDIDATES:
                raise CandidateValidationError(f"候选方案最多保存 {MAX_CANDIDATES} 个")
            now = datetime.now(UTC).isoformat()
            item = {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "name": clean_name,
                "source_id": clean_source_id,
                "config": clean_config,
                "metrics": clean_metrics,
                "data_as_of": data_as_of,
                "status": status,
                "created_at": now,
                "updated_at": now,
            }
            items.insert(0, item)
            self._write(items)
            return item

    def update(
        self,
        candidate_id: str,
        *,
        name: str | None = None,
        status: CandidateStatus | None = None,
    ) -> dict[str, Any]:
        with _lock:
            items = self._load()
            for item in items:
                if item["id"] != candidate_id:
                    continue
                if name is not None:
                    item["name"] = self._validate_name(name)
                if status is not None:
                    item["status"] = status
                item["updated_at"] = datetime.now(UTC).isoformat()
                self._write(items)
                return item
        raise KeyError(candidate_id)

    def delete(self, candidate_id: str) -> None:
        with _lock:
            items = self._load()
            remaining = [item for item in items if item["id"] != candidate_id]
            if len(remaining) == len(items):
                raise KeyError(candidate_id)
            self._write(remaining)

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            if self.path.stat().st_size > MAX_FILE_BYTES:
                raise CandidateStoreError("候选方案文件过大, 已停止读取")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except CandidateStoreError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CandidateStoreError("候选方案文件损坏或无法读取, 未执行覆盖写入") from exc
        if not isinstance(raw, list):
            raise CandidateStoreError("候选方案文件格式无效, 未执行覆盖写入")
        return [item for value in raw if (item := self._normalize(value)) is not None]

    def _write(self, items: list[dict[str, Any]]) -> None:
        payload = json.dumps(items, ensure_ascii=False, indent=2, allow_nan=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise CandidateStoreError("候选方案保存失败") from exc

    @staticmethod
    def _normalize(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        kind = value.get("kind")
        if kind not in _CONFIG_FIELDS or not isinstance(value.get("id"), str):
            return None
        raw_config = value.get("config") if isinstance(value.get("config"), dict) else {}
        config = {key: item for key, item in raw_config.items() if key in _CONFIG_FIELDS[kind]}
        raw_metrics = value.get("metrics") if isinstance(value.get("metrics"), dict) else {}
        metrics = {
            key: item
            for key, item in raw_metrics.items()
            if key in _METRIC_FIELDS[kind] and not isinstance(item, (dict, list))
        }
        source_id = value.get("source_id") or config.get(f"{kind}_name") or config.get(f"{kind}_id")
        if not isinstance(source_id, str) or not source_id:
            return None
        status = value.get("status")
        if status not in {"pending", "validated", "rejected"}:
            status = "pending"
        return {
            "id": value["id"],
            "kind": kind,
            "name": str(value.get("name") or source_id)[:MAX_NAME_LENGTH],
            "source_id": source_id,
            "config": config,
            "metrics": metrics,
            "data_as_of": value.get("data_as_of"),
            "status": status,
            "created_at": str(value.get("created_at") or ""),
            "updated_at": str(value.get("updated_at") or value.get("created_at") or ""),
        }

    @staticmethod
    def _validate_name(name: str) -> str:
        clean = name.strip()
        if not clean:
            raise CandidateValidationError("候选名称不能为空")
        if len(clean) > MAX_NAME_LENGTH:
            raise CandidateValidationError(f"候选名称不能超过 {MAX_NAME_LENGTH} 个字符")
        return clean

    @staticmethod
    def _validate_config(kind: CandidateKind, config: dict[str, Any]) -> dict[str, Any]:
        unknown = set(config) - _CONFIG_FIELDS[kind]
        if unknown:
            raise CandidateValidationError(
                f"候选配置包含不允许的字段: {', '.join(sorted(unknown))}"
            )
        CandidateStore._check_json_size(config)
        return config

    @staticmethod
    def _validate_metrics(kind: CandidateKind, metrics: dict[str, Any]) -> dict[str, Any]:
        unknown = set(metrics) - _METRIC_FIELDS[kind]
        if unknown:
            raise CandidateValidationError(
                f"候选指标包含不允许的字段: {', '.join(sorted(unknown))}"
            )
        if any(isinstance(value, (dict, list)) for value in metrics.values()):
            raise CandidateValidationError("候选指标只允许保存标量摘要")
        CandidateStore._check_json_size(metrics)
        return metrics

    @staticmethod
    def _check_json_size(value: dict[str, Any]) -> None:
        try:
            payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise CandidateValidationError("候选内容无法序列化") from exc
        if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise CandidateValidationError("候选内容超过 32KB 限制")
