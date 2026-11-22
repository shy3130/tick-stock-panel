"""Download verified result artifacts without reopening the research inputs."""

import hashlib
from contextlib import ExitStack
from tempfile import TemporaryFile
from zipfile import ZipFile

from pydantic import ValidationError
from tivelo_backtest.archives import MAX_ARCHIVE_BYTES
from tivelo_backtest.study_models import StudyArchive
from tivelo_contracts.artifacts import ResultArchive
from tivelo_contracts.base import canonical_bytes

from .artifacts import read_document
from .storage import JobError

DOWNLOAD_CHUNK = 1024 * 1024


def _write_artifact(bundle, store, root, declared):
    path = store.path(root, declared.path)
    if not path.is_file() or path.stat().st_size != declared.size_bytes:
        raise JobError("ARTIFACT_CORRUPT", "执行制品缺失或大小已改变")
    digest = hashlib.sha256()
    remaining = declared.size_bytes
    with path.open("rb") as source, bundle.open(f"output/{declared.path}", "w") as target:
        while chunk := source.read(min(DOWNLOAD_CHUNK, remaining + 1)):
            remaining -= len(chunk)
            if remaining < 0:
                raise JobError("ARTIFACT_CORRUPT", "执行制品大小已改变")
            digest.update(chunk)
            target.write(chunk)
    if remaining or digest.hexdigest() != declared.sha256:
        raise JobError("ARTIFACT_CORRUPT", "执行制品与清单摘要不一致")


def prepare_result_zip(store, report):
    reference = report.execution
    models = {"result": ResultArchive, "study": StudyArchive}
    model = models.get(reference.kind)
    if model is None:
        raise JobError("ARTIFACT_CORRUPT", "报告未引用可导出的执行制品")
    try:
        marker = store.path(reference.path, "manifest.json")
        if not marker.is_file():
            raise JobError("ARTIFACT_CORRUPT", "执行制品清单缺失")
        manifest_bytes = read_document(marker)
        manifest = model.model_validate_json(manifest_bytes)
        if manifest.archive_id != reference.identity:
            raise JobError("ARTIFACT_CORRUPT", "执行制品与报告引用不一致")
        files = manifest.contents.files
        if sum(file.size_bytes for file in files) > MAX_ARCHIVE_BYTES:
            raise JobError("RESOURCE_LIMIT", "执行制品超过归档大小限制")
        # Stage ZIP_STORED on disk so corruption or write errors are reported before any download.
        with ExitStack() as pending:
            stream = pending.enter_context(TemporaryFile())
            with ZipFile(stream, "w") as bundle:
                bundle.writestr("report.json", canonical_bytes(report))
                for file in files:
                    _write_artifact(bundle, store, reference.path, file)
                bundle.writestr("output/manifest.json", manifest_bytes)
            size = stream.tell()
            stream.seek(0)
            # The response owns the temporary file after the completed ZIP is ready to send.
            pending.pop_all()
    except (FileNotFoundError, ValidationError) as exc:
        raise JobError("ARTIFACT_CORRUPT", "执行制品清单或文件已改变") from exc
    except OSError as exc:
        raise JobError("PUBLICATION_FAILED", "结果 ZIP 读写失败，请检查存储空间和权限") from exc
    return stream, size
