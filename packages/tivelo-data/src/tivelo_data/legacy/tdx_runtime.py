"""Prepare one private copy of the optional TdxAiData library and its token settings."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

from .base import ProviderRequestError

_DLL_BY_PLATFORM = {"win32": "TdxAiData.dll", "darwin": "libTdxAiData.dylib"}
LIB_DLL_NAME = _DLL_BY_PLATFORM.get(sys.platform, "libTdxAiData.so")
LIB_ENV = "TDX_AI_DATA_LIB"  # tdxaidata 读取: 指向 DLL 文件路径


class TdxQuantError(ProviderRequestError):
    """tdxaidata 调用失败(网络/参数/协议)。"""


class TdxQuantAuthError(TdxQuantError):
    """服务端明确拒绝凭据或额度; 重试同一请求不能恢复。"""


class TdxQuantRateLimitError(TdxQuantError):
    """The server explicitly requested a lower call rate."""


def _raise_response_error(raw, api_key: str = "") -> None:
    """SDK._clean 会打印并抹掉 ErrorId, 必须在它之前保留失败语义。"""
    value = raw
    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(value)
        except (ValueError, UnicodeDecodeError):
            return
    if not isinstance(value, dict):
        return
    error_id = value.get("ErrorId")
    if (error_id is None or str(error_id) == "0") and "error" not in value:
        return
    message = str(value.get("Error") or value.get("error") or "未知错误")
    if api_key:
        message = message.replace(api_key, "[redacted]")
    message = re.sub(r"TDX-[\w-]+", "[redacted]", message, flags=re.IGNORECASE)[:200]
    if re.search(r"rate.?limit|too many requests|限频|请求频繁|频率超限", message, re.IGNORECASE):
        raise TdxQuantRateLimitError("TdxQuant 请求频率超限")
    if re.search(
        r"tokenkey|unauthori[sz]ed|invalid.*(?:key|token)|(?:key|token).*(?:error|expired)|权限|积分|额度|认证",
        message,
        re.IGNORECASE,
    ):
        raise TdxQuantAuthError(
            "TdxQuant 凭据、接口权限或额度不可用，请核对所配置 Key 与通达信后台状态。服务端: "
            + message
        )
    raise TdxQuantError(f"TdxQuant 服务端请求失败: {message}")


def package_lib_dir() -> Path:
    """tdxaidata 包自带的 lib 目录(平台 DLL + 运行时 ini)。"""
    import tdxaidata

    return Path(tdxaidata.__file__).resolve().parent / "lib"


def write_token_ini(ini: Path, api_key: str) -> None:
    """把 [Token] token=<key> 写入 ini, 保留其余配置行。"""
    lines = (
        ini.read_text(encoding="utf-8", errors="replace").splitlines()
        if ini.exists()
        else ["[Token]"]
    )
    out: list[str] = []
    in_token = False
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower() == "[token]":
            in_token = True
            out.append(line)
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_token = False
        if in_token and "=" in line and stripped.lower().split("=", 1)[0].strip() == "token":
            if not replaced:
                out.append(f"token={api_key}")
                replaced = True
            continue
        out.append(line)
    if "[Token]" not in {line.strip() for line in out}:
        out.extend(["[Token]", f"token={api_key}"])
    elif not replaced:
        idx = next(i for i, line in enumerate(out) if line.strip().lower() == "[token]")
        out.insert(idx + 1, f"token={api_key}")
    ini.write_text("\n".join(out) + "\n", encoding="utf-8")


def materialize_lib(api_key: str, lib_root: Path, source: Path | None = None) -> Path:
    """复制包内 lib 到私有目录并写入 token, 返回 DLL 路径。

    marker 记 token 指纹 + DLL mtime/size, 未变化时直接复用(每轮 realtime
    轮询都会走到这里, 不能反复复制几十 MB 的 DLL)。
    """
    src = source or package_lib_dir()
    dll = src / LIB_DLL_NAME
    if not dll.exists():
        raise TdxQuantError(f"tdxaidata lib 缺少 {LIB_DLL_NAME}: {src}")
    lib_root.mkdir(parents=True, exist_ok=True)
    marker = lib_root / ".tdxquant_lib_marker"
    stat = dll.stat()
    want = f"{hashlib.sha256(api_key.encode()).hexdigest()}:{stat.st_mtime_ns}:{stat.st_size}"
    copied = lib_root / LIB_DLL_NAME
    if marker.exists() and marker.read_text(encoding="utf-8") == want and copied.exists():
        return copied
    for item in src.iterdir():
        if item.is_file():
            shutil.copy2(item, lib_root / item.name)
    for ini_name in ("TdxAiData.ini", "user.ini"):
        write_token_ini(lib_root / ini_name, api_key)
    marker.write_text(want, encoding="utf-8")
    return copied


def historical_arguments(code: str, end: date | datetime, count: int, period: str) -> tuple:
    stamp = (
        end.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(end, datetime)
        else f"{end:%Y-%m-%d} 00:00:00"
    )
    return code, "", stamp, period, "none", int(count)
