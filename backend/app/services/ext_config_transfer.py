"""单表配置分享：沿用配置备份 ZIP，凭据剥离，导入只创建新表。"""
from __future__ import annotations

import copy
import io
import json
import re
import zipfile
from pathlib import Path
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit

from pydantic import ValidationError

from app import __version__, secrets_store
from app.services import config_backup, ext_data
from app.services.ext_data import ExtConfig, ExtConfigStore
from app.services.ext_data_schema import CreateExtReq, PullConfigReq
from app.services.package_archive import read_code_archive

MAX_UPLOAD = 4 * 1024 * 1024
MAX_JSON = 1024 * 1024
_ID = re.compile(r"^[a-zA-Z0-9_]{1,64}$")
_RESERVED_IDS = {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}
_STATE = {"last_run", "last_status", "last_message", "last_rows", "next_run"}
_CREDENTIAL_NAMES = {
    "key", "apikey", "token", "accesstoken", "refreshtoken", "secret", "clientsecret",
    "password", "passwd", "authorization", "proxyauthorization", "cookie", "setcookie",
    "signature", "sig", "credential", "credentials", "session", "sessionid",
}


def _credential_name(name: str) -> bool:
    name = re.sub(r"[^a-z0-9]", "", name.lower())
    return name in _CREDENTIAL_NAMES or name.endswith((
        "apikey", "token", "secret", "password", "accesskey", "secretkey", "credential", "signature",
    ))


def _known_secrets(config: dict) -> set[str]:
    values = {v for k, v in secrets_store.load().items()
              if _credential_name(k) and isinstance(v, str) and v}
    key = ext_data.get_ext_api_key(config["id"])
    if key:
        values.add(key)
    auth = (config.get("pull") or {}).get("auth") or {}
    if auth.get("key_ref"):
        ref = auth["key_ref"]
        key = secrets_store.get_env_backed_secret(ref, ref.upper())
        if key:
            values.add(key)
    return values


def _strip_literals(value, secrets: set[str], warnings: list[str]):
    if isinstance(value, dict):
        return {k: _strip_literals(v, secrets, warnings) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_literals(v, secrets, warnings) for v in value]
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            for literal in {secret, quote(secret, safe=""), quote_plus(secret)}:
                if literal and literal in value:
                    value = value.replace(literal, "")
                    warnings.append("已移除配置文本中的凭据，请核对接口地址和请求参数。")
    return value


def _clean_url(url: str, auth: dict, warnings: list[str]) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ValueError("拉取地址格式不正确，请检查接口地址") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("拉取地址必须是有效的 HTTP 或 HTTPS 地址")
    query = []
    for name, value in parse_qsl(parsed.query, keep_blank_values=True):
        if _credential_name(name) or (auth.get("type") == "query" and name == auth.get("param")):
            warnings.append("已移除 URL 中的凭据参数，请填写本表自己的 API Key。")
            if auth.get("type", "none") == "none":
                auth.update(type="query", param=name)
        else:
            query.append((name, value))
    if parsed.username is not None or parsed.password is not None:
        warnings.append("已移除 URL 中的用户名和密码，请重新配置接口鉴权。")
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, urlencode(query), ""))


def _clean_body(value, warnings: list[str]):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _credential_name(str(key)):
                out[key] = ""
                warnings.append("请求体中的凭据已清空；使用此类鉴权时请核对并补填请求体。")
            else:
                out[key] = _clean_body(item, warnings)
        return out
    if isinstance(value, list):
        return [_clean_body(item, warnings) for item in value]
    return value


def _portable_config(raw: dict, *, secrets: set[str] | None = None) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    raw = copy.deepcopy(raw)
    try:
        base = CreateExtReq.model_validate(raw).model_dump()
        if len({field["name"] for field in base["fields"]}) != len(base["fields"]):
            raise ValueError("扩展数据字段名不能重复")
        if raw.get("pull"):
            pull = dict(raw["pull"])
            for key in _STATE:
                pull.pop(key, None)
            auth = {k: v for k, v in (pull.get("auth") or {}).items() if k in {"type", "header", "param"}}
            if (pull.get("auth") or {}).get("key_ref"):
                warnings.append("原设备凭据引用已移除，请填写本表自己的 API Key。")
            pull["url"] = _clean_url(pull.get("url", ""), auth, warnings)
            headers = {}
            for name, value in (pull.get("headers") or {}).items():
                if (_credential_name(name)
                        or (auth.get("type") in {"header", "bearer"}
                            and name.lower() == str(auth.get("header", "Authorization")).lower())):
                    warnings.append("已移除请求头中的凭据，请填写本表自己的 API Key。")
                    if auth.get("type", "none") == "none" and name.lower() != "cookie":
                        auth.update(type="bearer" if str(value).lower().startswith("bearer ") else "header", header=name)
                else:
                    headers[name] = value
            pull["headers"] = headers
            body = pull.get("body")
            if body:
                try:
                    pull["body"] = json.dumps(_clean_body(json.loads(body), warnings), ensure_ascii=False)
                except json.JSONDecodeError:
                    pairs = parse_qsl(body, keep_blank_values=True)
                    if pairs and "=" in body:
                        pull["body"] = urlencode(_clean_body(dict(pairs), warnings))
            if pull.get("fanout"):
                fanout = dict(pull["fanout"])
                fanout["url"] = _clean_url(fanout.get("url", ""), auth, warnings)
                pull["fanout"] = _clean_body(fanout, warnings)
            pull["auth"] = auth or None
            pull["enabled"] = False
            validated = PullConfigReq.model_validate(pull).model_dump()
            base["pull"] = validated
        base = _strip_literals(base, secrets or set(), warnings)
        json.dumps(base, allow_nan=False)
    except (ValidationError, TypeError, KeyError) as exc:
        raise ValueError("扩展数据配置格式不正确，请检查标识、字段和拉取参数") from exc
    return base, list(dict.fromkeys(warnings))


def export_config(data_dir: Path, config_id: str) -> tuple[bytes, str]:
    if not _ID.fullmatch(config_id) or config_id.upper() in _RESERVED_IDS:
        raise ValueError("扩展标识不合法或是系统保留名称")
    config = ExtConfigStore(data_dir).get(config_id)
    if config is None:
        raise FileNotFoundError("扩展数据配置不存在")
    raw = config.to_dict()
    secrets = _known_secrets(raw)
    portable, warnings = _portable_config(raw, secrets=secrets)
    content = json.dumps(portable, ensure_ascii=False, indent=2, allow_nan=False).encode()
    # Runtime state is excluded; never publish a known credential hidden in a
    # field name or an unexpected location that the sanitizer could not remove.
    if any(secret.encode() in content for secret in secrets):
        raise ValueError("配置中仍含凭据，已停止导出；请将凭据移到 API Key 设置后重试")
    manifest = {
        "format": config_backup.BACKUP_FORMAT, "version": config_backup.BACKUP_VERSION,
        "package_type": "ext-data-config", "app_version": __version__, "include_secrets": False,
        "categories": [{"key": "ext_data", "label": "扩展数据集配置", "count": 1}],
        "warnings": warnings,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr(f"ext_data/{config_id}/config.json", content)
    return output.getvalue(), f"tsp-ext-data-{config_id}.zip"


def _read_config(data: bytes) -> tuple[dict, list[str]]:
    if not data or len(data) > MAX_UPLOAD:
        raise ValueError("请选择不超过 4 MiB 的扩展配置 ZIP")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 16 or any(i.file_size > MAX_JSON for i in infos) or sum(i.file_size for i in infos) > 2 * MAX_JSON:
                raise ValueError("扩展配置包条目或解压大小超过上限")
        files = read_code_archive(data)
        if len(files) != 2 or "manifest.json" not in files:
            raise ValueError("扩展配置包只允许清单和一张表的 config.json，不接收密钥或数据文件")
        manifest = json.loads(files.pop("manifest.json"))
        if (not isinstance(manifest, dict) or manifest.get("format") != config_backup.BACKUP_FORMAT
                or manifest.get("version") != config_backup.BACKUP_VERSION):
            raise ValueError("不支持的扩展配置包格式或版本")
        path, payload = next(iter(files.items()))
        match = re.fullmatch(r"ext_data/([a-zA-Z0-9_]{1,64})/config\.json", path)
        raw = json.loads(payload)
        if not match or not isinstance(raw, dict) or raw.get("id") != match[1]:
            raise ValueError("扩展配置标识与包内路径不一致")
        portable, warnings = _portable_config(raw)
        notes = manifest.get("warnings") or []
        # Only locally generated notices are displayed. A package's arbitrary
        # manifest strings could themselves contain the sender's credentials.
        if notes:
            warnings.append("此配置导出时清理过凭据，导入后请核对接口和鉴权设置。")
        return portable, list(dict.fromkeys(warnings))
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("无法读取扩展配置包，请选择有效的 ZIP 文件") from exc


def inspect_config(data_dir: Path, data: bytes) -> dict:
    config, warnings = _read_config(data)
    pull = config.get("pull") or {}
    return {
        "id": config["id"], "label": config["label"], "mode": config["mode"],
        "fields": config["fields"], "field_count": len(config["fields"]),
        "conflict": (data_dir / "ext_data" / config["id"]).exists(),
        "requires_key": (pull.get("auth") or {}).get("type", "none") != "none",
        "has_pull": bool(pull), "pull_url": pull.get("url"), "pull_method": pull.get("method"),
        "warnings": warnings,
    }


def import_config(data_dir: Path, data: bytes, *, config_id: str | None = None) -> dict:
    config, _ = _read_config(data)
    if config_id is not None:
        if not _ID.fullmatch(config_id) or config_id.upper() in _RESERVED_IDS:
            raise ValueError("导入标识只允许 1–64 位英文字母、数字和下划线，且不能使用系统保留名称")
        config["id"] = config_id
    store = ExtConfigStore(data_dir)
    created = ExtConfig.from_dict(config)
    store.create(created)
    return created.to_dict()
