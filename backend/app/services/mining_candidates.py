"""Trusted promotion and explicit publication for persisted mining candidates.

Concurrency protection is process-local; V1 remains a single-process service.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow.parquet as pq

from app.backtest.candidates import CandidateStore
from app.backtest.factor import FACTOR_COLUMNS
from app.backtest.mining import compute_candidate_signature, evaluate_candidate_gate
from app.services.mining_jobs import SUCCESS_RUN_STATUSES, MiningRunStore
from app.strategy.ai_generator import AIStrategyGenerator
from app.strategy.engine import StrategyEngine

_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_MAX_ARTIFACT_ROWS = 32
_MAX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
_MAX_DEFINITION_BYTES = 16 * 1024
_FACTOR_IDS = frozenset(str(item["id"]) for item in FACTOR_COLUMNS)
_FACTOR_DEFINITION_FIELDS = frozenset({"kind", "factor_names", "scoring", "directions"})
_EXISTING_DEFINITION_FIELDS = frozenset({"kind", "strategy_id"})
_BACKLINK_FIELDS = frozenset({"promoted_candidate_id", "published_strategy_id"})
_PUBLISHED_PREFIX = "mined_factor_"
_STRATEGY_ID_PATTERN = re.compile(r"^mined_factor_[A-Za-z0-9_-]{1,49}$")
_ARTIFACT_SCHEMA = {
    "signature": pl.String,
    "name": pl.String,
    "kind": pl.String,
    "factor_names_json": pl.String,
    "strategy_id": pl.String,
    "definition_json": pl.String,
    "regime_state": pl.String,
    "score": pl.Float64,
    "oos_return": pl.Float64,
    "oos_sharpe": pl.Float64,
    "oos_max_drawdown": pl.Float64,
    "oos_positive_fold_ratio": pl.Float64,
    "oos_n_trades": pl.Int64,
    "confidence": pl.String,
    "valid_folds": pl.Int64,
    "skipped_folds": pl.Int64,
    "promoted_candidate_id": pl.String,
    "published_strategy_id": pl.String,
}
_LOCK = threading.RLock()


class MiningCandidateService:
    def __init__(
        self,
        data_dir: Path | str,
        run_store: MiningRunStore,
        candidate_store: CandidateStore,
        strategy_engine: StrategyEngine,
        *,
        strategy_cache_invalidator: Callable[[Path], None] | None = None,
        monitor_state_invalidator: Callable[[], None] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.run_store = run_store
        self.candidate_store = candidate_store
        self.strategy_engine = strategy_engine
        if strategy_cache_invalidator is None:
            from app.services.strategy_cache import clear_cache

            strategy_cache_invalidator = clear_cache
        self._strategy_cache_invalidator = strategy_cache_invalidator
        self._monitor_state_invalidator = monitor_state_invalidator

    def promote(self, run_id: str, signature: str) -> dict[str, Any]:
        with _LOCK:
            manifest, summary, path, frame, row, definition = self._load_candidate(
                run_id, signature
            )
            kind, name, source_id, config = self._promotion_config(
                manifest, summary, row, definition
            )
            item = self.candidate_store.create_or_get_by_provenance(
                origin_run_id=run_id,
                candidate_signature=signature,
                kind=kind,
                name=name,
                source_id=source_id,
                config=config,
                metrics=self._candidate_metrics(row),
                data_as_of=_optional_string(summary.get("data_as_of")),
                status="pending",
            )
            if row.get("promoted_candidate_id") != item["id"]:
                self._write_backlink(
                    path,
                    frame,
                    signature,
                    "promoted_candidate_id",
                    item["id"],
                )
            return item

    def publish(self, run_id: str, signature: str) -> dict[str, Any]:
        with _LOCK:
            manifest, summary, path, frame, row, definition = self._load_candidate(
                run_id, signature
            )
            gate = evaluate_candidate_gate(
                confidence=row.get("confidence"),
                valid_folds=row.get("valid_folds"),
                positive_fold_ratio=row.get("oos_positive_fold_ratio"),
                sharpe=row.get("oos_sharpe"),
                max_drawdown=row.get("oos_max_drawdown"),
                n_trades=row.get("oos_n_trades"),
            )
            if not gate.qualified:
                raise ValueError(
                    "candidate does not meet the promotion gate: "
                    + "; ".join(gate.reasons)
                )
            asset_type = self._asset_type(manifest)
            if definition["kind"] == "existing_strategy":
                published_id = str(definition["strategy_id"])
                self._validate_publication_backlink(row, published_id)
                self._verify_public_strategy(published_id, asset_type)
                if row.get("published_strategy_id") != published_id:
                    self._write_backlink(
                        path,
                        frame,
                        signature,
                        "published_strategy_id",
                        published_id,
                    )
                return {"ok": True, "strategy_id": published_id}

            published_id = self._validate_published_id(
                _published_strategy_id(run_id, signature)
            )
            self._validate_publication_backlink(row, published_id)
            source = self._render_factor_strategy(
                manifest, summary, row, definition, published_id
            )

            target = self._custom_strategy_path(published_id)
            created = self._publish_or_verify_source(
                target,
                source,
                published_id,
                run_id,
                signature,
                asset_type,
            )
            try:
                self._strategy_cache_invalidator(self.data_dir)
                if self._monitor_state_invalidator is not None:
                    self._monitor_state_invalidator()
            except Exception as exc:
                rollback_error = (
                    self._rollback_created_source(target, source) if created else None
                )
                message = f"strategy runtime invalidation failed: {exc}"
                if rollback_error is not None:
                    message += f"; strategy rollback failed: {rollback_error}"
                raise RuntimeError(message) from exc
            if row.get("published_strategy_id") != published_id:
                self._write_backlink(
                    path,
                    frame,
                    signature,
                    "published_strategy_id",
                    published_id,
                )
            return {"ok": True, "strategy_id": published_id}

    def _load_candidate(
        self,
        run_id: str,
        signature: str,
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
        Path,
        pl.DataFrame,
        dict[str, Any],
        dict[str, Any],
    ]:
        if not isinstance(signature, str) or not signature:
            raise ValueError("candidate signature must not be empty")
        manifest = self.run_store.get(run_id)
        if manifest is None:
            raise KeyError(run_id)
        if manifest.get("status") not in SUCCESS_RUN_STATUSES:
            raise ValueError("mining candidates require a successful run")
        path = self._registered_candidates_path(manifest)
        frame = self._read_artifact(path)
        matches = frame.filter(pl.col("signature") == signature)
        if matches.height == 0:
            raise KeyError(signature)
        if matches.height != 1:
            raise ValueError("mining candidates artifact contains a duplicate signature")
        row = matches.row(0, named=True)
        self._asset_type(manifest)
        definition = self._validate_definition(manifest, row, signature)
        summary = self._validated_summary(self.run_store.read_summary(run_id))
        return manifest, summary, path, frame, row, definition

    def _registered_candidates_path(self, manifest: Mapping[str, Any]) -> Path:
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise ValueError("mining candidates artifact is not registered")
        raw_path = artifacts.get("candidates")
        if raw_path != "candidates.parquet":
            raise ValueError(
                "only the registered candidates.parquet artifact can be used"
            )
        path = self.run_store.artifact_path(
            str(manifest["run_id"]), "candidates"
        )
        if path.is_symlink() or not path.is_file():
            raise ValueError("mining candidates artifact is unavailable")
        return path

    @staticmethod
    def _read_artifact(path: Path) -> pl.DataFrame:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise RuntimeError("failed to read mining candidates artifact") from exc
        if size <= 0 or size > _MAX_ARTIFACT_BYTES:
            raise ValueError("mining candidates artifact exceeds its size limit")
        try:
            parquet = pq.ParquetFile(path)
            metadata = parquet.metadata
            schema = pl.read_parquet_schema(path)
        except Exception as exc:
            raise RuntimeError("failed to read mining candidates artifact") from exc
        if metadata is None or metadata.num_rows > _MAX_ARTIFACT_ROWS:
            raise ValueError("mining candidates artifact exceeds its row limit")
        uncompressed = sum(
            metadata.row_group(index).total_byte_size
            for index in range(metadata.num_row_groups)
        )
        if uncompressed > _MAX_UNCOMPRESSED_BYTES:
            raise ValueError(
                "mining candidates artifact exceeds its uncompressed size limit"
            )
        missing = sorted(set(_ARTIFACT_SCHEMA) - set(schema))
        if missing:
            raise ValueError(f"mining candidates artifact schema is invalid: {missing}")
        invalid_types = [
            name for name, dtype in _ARTIFACT_SCHEMA.items()
            if schema[name] != dtype
        ]
        if invalid_types:
            raise ValueError(
                "mining candidates artifact column types are invalid: "
                f"{invalid_types}"
            )
        try:
            return pl.read_parquet(path, columns=list(_ARTIFACT_SCHEMA))
        except Exception as exc:
            raise RuntimeError("failed to read mining candidates artifact") from exc

    def _validate_definition(
        self,
        manifest: Mapping[str, Any],
        row: Mapping[str, Any],
        signature: str,
    ) -> dict[str, Any]:
        raw_definition = row.get("definition_json")
        if (
            not isinstance(raw_definition, str)
            or len(raw_definition.encode("utf-8")) > _MAX_DEFINITION_BYTES
        ):
            raise ValueError("mining candidate definition is unavailable")
        try:
            definition = json.loads(raw_definition)
        except json.JSONDecodeError as exc:
            raise ValueError("mining candidate definition is invalid") from exc
        if not isinstance(definition, dict):
            raise ValueError("mining candidate definition must be an object")
        kind = definition.get("kind")
        if kind == "factor_rank":
            if set(definition) != _FACTOR_DEFINITION_FIELDS:
                raise ValueError("factor candidate definition contains unsupported fields")
            expected_kind = "factor_combination"
        elif kind == "existing_strategy":
            if set(definition) != _EXISTING_DEFINITION_FIELDS:
                raise ValueError("existing candidate definition contains unsupported fields")
            expected_kind = "existing_strategy"
        else:
            raise ValueError(f"unsupported mining candidate kind: {kind!r}")
        if row.get("kind") != expected_kind:
            raise ValueError("mining candidate kind does not match its definition")
        if (
            not isinstance(row.get("name"), str)
            or not row["name"]
            or len(row["name"]) > 80
            or row.get("regime_state") != "overall"
        ):
            raise ValueError("mining candidate row metadata is invalid")
        if kind == "factor_rank":
            self._validate_factor_definition(manifest, row, definition)
        else:
            self._validate_existing_definition(manifest, row, definition)
        computed = compute_candidate_signature(definition)
        if row.get("signature") != signature or computed != signature:
            raise ValueError("mining candidate signature does not match its definition")
        score = row.get("score")
        if (
            score is not None
            and (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
            )
        ):
            raise ValueError("candidate score must be finite")
        return definition

    @staticmethod
    def _request(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
        request = manifest.get("request")
        if not isinstance(request, Mapping):
            raise ValueError("mining origin request is unavailable")
        return request

    def _validate_factor_definition(
        self,
        manifest: Mapping[str, Any],
        row: Mapping[str, Any],
        definition: Mapping[str, Any],
    ) -> None:
        factor_names = definition.get("factor_names")
        if (
            not isinstance(factor_names, list)
            or not 1 <= len(factor_names) <= 4
            or len(set(factor_names)) != len(factor_names)
            or any(not isinstance(name, str) or not name for name in factor_names)
        ):
            raise ValueError("factor candidate must contain 1 to 4 unique factors")
        scoring = definition.get("scoring")
        directions = definition.get("directions")
        if not isinstance(scoring, Mapping) or set(scoring) != set(factor_names):
            raise ValueError("factor scoring keys must exactly match factor names")
        if not isinstance(directions, Mapping) or set(directions) != set(factor_names):
            raise ValueError("factor direction keys must exactly match factor names")
        for factor_name in factor_names:
            weight = scoring[factor_name]
            if (
                isinstance(weight, bool)
                or not isinstance(weight, (int, float))
                or not math.isfinite(float(weight))
                or float(weight) <= 0.0
            ):
                raise ValueError("factor weights must be finite and positive")
            if directions[factor_name] not in {"high", "low"}:
                raise ValueError("factor directions must be high or low")
        unknown = sorted(set(factor_names) - _FACTOR_IDS)
        if unknown:
            raise ValueError(f"factor candidate contains unknown factors: {unknown}")
        selected = self._request(manifest).get("factor_names")
        if not isinstance(selected, list) or not set(factor_names) <= set(selected):
            raise ValueError("factor candidate contains factors absent from its origin request")
        try:
            persisted_names = json.loads(str(row.get("factor_names_json")))
        except json.JSONDecodeError as exc:
            raise ValueError("factor candidate factor list is invalid") from exc
        if persisted_names != factor_names or row.get("strategy_id") is not None:
            raise ValueError("factor candidate columns do not match its definition")

    def _validate_existing_definition(
        self,
        manifest: Mapping[str, Any],
        row: Mapping[str, Any],
        definition: Mapping[str, Any],
    ) -> None:
        strategy_id = definition.get("strategy_id")
        if not isinstance(strategy_id, str) or not strategy_id:
            raise ValueError("existing candidate strategy ID is invalid")
        selected = self._request(manifest).get("strategy_ids")
        if not isinstance(selected, list) or strategy_id not in selected:
            raise ValueError("existing candidate was absent from its origin request")
        if row.get("strategy_id") != strategy_id:
            raise ValueError("existing candidate strategy ID does not match its definition")
        asset_type = str(self._request(manifest).get("asset_type") or "stock")
        self._verify_public_strategy(strategy_id, asset_type)

    def _promotion_config(
        self,
        manifest: Mapping[str, Any],
        summary: Mapping[str, Any],
        row: Mapping[str, Any],
        definition: Mapping[str, Any],
    ) -> tuple[str, str, str, dict[str, Any]]:
        request = self._request(manifest)
        signature = str(row["signature"])
        provenance = {
            "origin_run_id": str(manifest["run_id"]),
            "candidate_signature": signature,
            "regime_state": str(row.get("regime_state") or "overall"),
            "algorithm_version": str(summary.get("algorithm_version") or "mining-v1"),
            "methodology_version": str(summary.get("methodology_version") or "factor_v2"),
        }
        common = {
            "start": request.get("start"),
            "end": request.get("end"),
            "asset_type": request.get("asset_type") or "stock",
            "matching": "open_t+1",
            "entry_fill": "open_t+1",
            "exit_fill": "open_t+1",
            "commission_pct": request.get("commission_pct", 0.0002),
            "stamp_tax_pct": request.get("stamp_tax_pct", 0.0005),
            "slippage_bps": request.get("slippage_bps", 5.0),
            "mode": "position",
            "minute_fill": False,
            **provenance,
        }
        name = str(row.get("name") or signature)[:80]
        if definition["kind"] == "existing_strategy":
            strategy_id = str(definition["strategy_id"])
            return "strategy", name, strategy_id, {
                **common,
                "strategy_id": strategy_id,
            }
        factor_names = list(definition["factor_names"])
        source_id = _published_strategy_id(str(manifest["run_id"]), signature)
        return "strategy", name, source_id, {
            **common,
            "strategy_id": source_id,
            "factor_names": factor_names,
            "directions": [definition["directions"][name] for name in factor_names],
            "weights": [float(definition["scoring"][name]) for name in factor_names],
        }

    @staticmethod
    def _candidate_metrics(row: Mapping[str, Any]) -> dict[str, Any]:
        fields = (
            "oos_sharpe",
            "oos_return",
            "oos_max_drawdown",
            "oos_positive_fold_ratio",
            "oos_n_trades",
            "valid_folds",
            "skipped_folds",
            "confidence",
        )
        result: dict[str, Any] = {}
        for field in fields:
            value = row.get(field)
            if value is None:
                continue
            if field == "confidence":
                if not isinstance(value, str) or not value or len(value) > 32:
                    raise ValueError("candidate confidence is invalid")
            elif (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"candidate metric {field} must be finite")
            result[field] = value
        return result

    @staticmethod
    def _validated_summary(summary: Mapping[str, Any]) -> dict[str, str]:
        result: dict[str, str] = {}
        for field in ("data_as_of", "algorithm_version", "methodology_version"):
            value = summary.get(field)
            if not isinstance(value, str) or not value or len(value) > 120:
                raise ValueError(f"mining summary {field} is invalid")
            result[field] = value
        return result

    @staticmethod
    def _asset_type(manifest: Mapping[str, Any]) -> str:
        asset_type = MiningCandidateService._request(manifest).get("asset_type")
        if asset_type not in {"stock", "etf"}:
            raise ValueError("mining origin asset_type is invalid")
        return str(asset_type)

    def _verify_public_strategy(
        self,
        strategy_id: str,
        asset_type: str,
        *,
        path: Path | None = None,
        run_id: str | None = None,
        signature: str | None = None,
    ) -> None:
        strategy = self.strategy_engine.get(strategy_id)
        if strategy.meta.get("research_only"):
            raise ValueError("research-only strategy cannot be published")
        if strategy.execution_backend != "matrix_native":
            raise ValueError("published strategy must be matrix-native")
        if "1d" not in strategy.meta.get("timeframes", []):
            raise ValueError("published strategy must support 1d")
        if asset_type not in strategy.meta.get("asset_types", []):
            raise ValueError("published strategy does not support the run asset type")
        public_ids = {
            str(meta.get("id")) for meta in self.strategy_engine.list_strategies()
        }
        if strategy_id not in public_ids:
            raise ValueError("published strategy is not publicly discoverable")
        if path is not None and (
            strategy.source != "custom"
            or strategy.file_path is None
            or strategy.file_path.resolve() != path.resolve()
            or strategy.meta.get("origin_run_id") != run_id
            or strategy.meta.get("candidate_signature") != signature
        ):
            raise ValueError("published strategy provenance is invalid")

    @staticmethod
    def _validate_published_id(strategy_id: str) -> str:
        if not _STRATEGY_ID_PATTERN.fullmatch(strategy_id):
            raise ValueError(
                f"strategy_id must use {_PUBLISHED_PREFIX!r} and safe characters"
            )
        return strategy_id

    @staticmethod
    def _validate_publication_backlink(
        row: Mapping[str, Any],
        strategy_id: str,
    ) -> None:
        existing = _optional_string(row.get("published_strategy_id"))
        if existing is not None and existing != strategy_id:
            raise ValueError("candidate publication backlink is inconsistent")

    def _custom_strategy_path(self, strategy_id: str) -> Path:
        unresolved_root = self.data_dir / "strategies" / "custom"
        unresolved_root.mkdir(parents=True, exist_ok=True)
        if unresolved_root.is_symlink():
            raise ValueError("custom strategy directory must not be a symlink")
        root = unresolved_root.resolve()
        if not root.is_relative_to(self.data_dir):
            raise ValueError("custom strategy directory escapes data_dir")
        path = (root / f"{strategy_id}.py").resolve(strict=False)
        if path.parent != root:
            raise ValueError("strategy publication path escapes custom directory")
        return path

    def _render_factor_strategy(
        self,
        manifest: Mapping[str, Any],
        summary: Mapping[str, Any],
        row: Mapping[str, Any],
        definition: Mapping[str, Any],
        strategy_id: str,
    ) -> str:
        asset_type = self._asset_type(manifest)
        factor_names = list(definition["factor_names"])
        scoring = {
            name: float(definition["scoring"][name]) for name in factor_names
        }
        directions = {name: definition["directions"][name] for name in factor_names}
        meta = {
            "id": strategy_id,
            "name": row["name"],
            "description": "Published mining factor-rank candidate",
            "tags": ["mining", "factor-rank"],
            "asset_types": [asset_type],
            "timeframes": ["1d"],
            "research_only": False,
            "origin_run_id": manifest["run_id"],
            "candidate_signature": row["signature"],
            "mining_algorithm_version": summary["algorithm_version"],
            "factor_methodology_version": summary["methodology_version"],
            "params": [
                {
                    "id": "entry_score",
                    "label": "Entry minimum score",
                    "type": "float",
                    "default": 70.0,
                    "min": 0.0,
                    "max": 100.0,
                    "step": 5.0,
                },
                {
                    "id": "exit_score",
                    "label": "Exit maximum score",
                    "type": "float",
                    "default": 40.0,
                    "min": 0.0,
                    "max": 100.0,
                    "step": 5.0,
                },
                {
                    "id": "top_rank",
                    "label": "Daily selection limit",
                    "type": "int",
                    "default": 20,
                    "min": 1,
                    "max": 100,
                    "step": 1,
                },
            ],
            "scoring": {},
            "order_by": "score",
            "descending": True,
            "limit": 100,
        }
        return (
            '"""Trusted factor-rank strategy published from a mining run."""\n'
            "from app.strategy.research.factor_rank_research import "
            "FactorRankResearchMatrixStrategy\n\n"
            f"META = {meta!r}\n\n"
            'EXECUTION_BACKEND = "matrix_native"\n'
            'ENTRY_SIGNALS = ["signal_factor_rank_entry"]\n'
            'EXIT_SIGNALS = ["signal_factor_rank_exit"]\n'
            "STOP_LOSS = -0.08\n"
            "MAX_HOLD_DAYS = 30\n\n"
            f"SCORING = {scoring!r}\n"
            f"DIRECTIONS = {directions!r}\n"
            "MATRIX_STRATEGY = FactorRankResearchMatrixStrategy(SCORING, DIRECTIONS)\n"
        )

    def _publish_or_verify_source(
        self,
        path: Path,
        source: str,
        strategy_id: str,
        run_id: str,
        signature: str,
        asset_type: str,
    ) -> bool:
        validation = AIStrategyGenerator().validate_code(source)
        if not validation.get("valid"):
            raise ValueError(
                f"rendered strategy failed validation: {validation.get('error')}"
            )
        if validation.get("meta", {}).get("id") != strategy_id:
            raise ValueError("rendered strategy META id is invalid")
        if path.exists() or path.is_symlink():
            self._verify_existing_source(
                path, source, strategy_id, run_id, signature, asset_type
            )
            return False
        if self.strategy_engine.has(strategy_id):
            raise ValueError(f"strategy ID already exists: {strategy_id}")

        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        linked = False
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(source)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise ValueError(f"strategy path already exists: {strategy_id}") from exc
            except OSError as exc:
                raise RuntimeError("failed to create strategy source") from exc
            linked = True
            self._fsync_directory(path.parent)
            try:
                self.strategy_engine.reload()
                self._verify_public_strategy(
                    strategy_id,
                    asset_type,
                    path=path,
                    run_id=run_id,
                    signature=signature,
                )
            except Exception as exc:
                rollback_error = self._rollback_publication(path, temporary)
                message = f"strategy publication failed: {exc}"
                if rollback_error is not None:
                    message += f"; registry rollback failed: {rollback_error}"
                raise RuntimeError(message) from exc
        finally:
            temporary.unlink(missing_ok=True)
            if linked:
                self._fsync_directory(path.parent)
        return True

    def _verify_existing_source(
        self,
        path: Path,
        source: str,
        strategy_id: str,
        run_id: str,
        signature: str,
        asset_type: str,
    ) -> None:
        if path.is_symlink() or not path.is_file():
            raise ValueError("strategy publication target is not a regular file")
        try:
            existing_source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError("existing strategy source is unreadable") from exc
        if existing_source != source:
            raise ValueError(f"strategy ID collision: {strategy_id}")
        try:
            self._verify_public_strategy(
                strategy_id,
                asset_type,
                path=path,
                run_id=run_id,
                signature=signature,
            )
        except ValueError:
            self.strategy_engine.reload()
            self._verify_public_strategy(
                strategy_id,
                asset_type,
                path=path,
                run_id=run_id,
                signature=signature,
            )

    def _rollback_publication(self, path: Path, temporary: Path) -> Exception | None:
        try:
            if path.exists() and temporary.exists() and os.path.samefile(path, temporary):
                path.unlink()
                self._fsync_directory(path.parent)
            self.strategy_engine.reload()
        except Exception as exc:
            return exc
        return None

    def _rollback_created_source(self, path: Path, source: str) -> Exception | None:
        try:
            if path.is_file() and not path.is_symlink():
                if path.read_text(encoding="utf-8") != source:
                    return RuntimeError("published strategy source changed before rollback")
                path.unlink()
                self._fsync_directory(path.parent)
            self.strategy_engine.reload()
        except Exception as exc:
            return exc
        return None

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _write_backlink(
        path: Path,
        frame: pl.DataFrame,
        signature: str,
        field: str,
        value: str,
    ) -> None:
        if field not in _BACKLINK_FIELDS:
            raise ValueError("unsupported mining candidate backlink")
        matches = frame.filter(pl.col("signature") == signature)
        if matches.height != 1:
            raise ValueError("mining candidate backlink target is no longer unique")
        updated = frame.with_columns(
            pl.when(pl.col("signature") == signature)
            .then(pl.lit(value))
            .otherwise(pl.col(field))
            .alias(field)
        )
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            updated.write_parquet(temporary)
            with temporary.open("r+b") as stream:
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("failed to update mining candidate artifact") from exc


def _published_strategy_id(run_id: str, signature: str) -> str:
    payload = f"{run_id}\0{signature}".encode()
    digest = hashlib.blake2b(payload, digest_size=10).hexdigest()
    return f"{_PUBLISHED_PREFIX}{digest}"


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
