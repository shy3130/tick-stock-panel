"""Read the exact bytes selected from a v1 archive."""

import hashlib
import stat
from contextlib import contextmanager
from datetime import date
from tempfile import SpooledTemporaryFile

import pyarrow.parquet as pq
from tivelo_contracts.errors import ErrorCode, TiveloError

DAILY_TABLES = {"daily_raw_v1": "kline_daily", "enriched_v1": "kline_daily_enriched"}
FACTOR_TABLE = "adj_factor"
MINUTE_TABLE = "kline_minute"
MINUTE_MARKER = ".raw_basis"


@contextmanager
def verified_parquet(path, digest, context):
    """Decode only the exact file bytes fixed for this observation."""
    with SpooledTemporaryFile(max_size=4 * 1024**2) as frozen:
        copy_verified(path, frozen, digest, context)
        frozen.seek(0)
        with pq.ParquetFile(frozen) as parquet:
            yield parquet


def identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def changed():
    return TiveloError(ErrorCode.DATA_CONFLICT, "Legacy source changed after selection")


def date_partition_files(table, start, end, *, file_id=identity):
    files = {}
    if table.is_symlink() or not table.is_dir():
        raise changed()
    for folder in table.iterdir():
        if not folder.name.startswith("date="):
            continue
        name = folder.name[5:]
        if not start.isoformat() <= name <= end.isoformat():
            continue
        if (
            folder.is_symlink()
            or not folder.is_dir()
            or date.fromisoformat(name).isoformat() != name
        ):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid dated archive partition")
        for path in folder.iterdir():
            if path.suffix == ".parquet":
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid archive partition file")
                files[path] = file_id(info)
    return files


def copy_verified(path, output, expected_digest, context):
    # Atomic replacements and in-place edits must both preserve the selected bytes.
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024**2):
            context.check_cancelled()
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != expected_digest:
        raise changed()
