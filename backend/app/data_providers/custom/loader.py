"""Load custom data source definitions from user data files."""
from __future__ import annotations

import hashlib
import importlib
import logging
import math
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import yaml

from app import secrets_store
from app.config import settings
from app.data_providers.base import ProviderRequestError
from app.data_providers.custom.config import (
    DEFAULT_TIMEOUT,
    MAX_TIMEOUT,
    CustomSourceConfig,
    config_from_dict,
    load_config,
)
from app.data_providers.custom.managed import ManagedProvider
from app.data_providers.custom.provider import GenericHTTPProvider
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

_PROVIDERS: dict[str, GenericHTTPProvider] = {}
_LOAD_ERRORS: list[dict] = []

# 内置插件状态: {name: {available, status, runtime, ...}} 供设置页独立分类展示。
# available=False 的插件不注册进 _PROVIDERS (不可切换), 但记录状态供 UI 显示安装提示。
_PLUGIN_STATUS: dict[str, dict] = {}

_NAME_RE = re.compile(r"^[a-z0-9_]+$")


def plugins_dir() -> Path:
    """内置可选插件目录 (app/plugins/, 与现有包结构一致, 开发态/容器态路径统一)。"""
    return Path(__file__).resolve().parents[2] / "plugins"


def data_sources_dir() -> Path:
    return settings.data_dir / "data_sources"


def load_all(path: Path | None = None) -> None:
    """Load all custom provider YAML files into process memory."""
    global _PROVIDERS, _LOAD_ERRORS
    retained = {name: provider for name, provider in _PROVIDERS.items()
                if _PLUGIN_STATUS.get(name, {}).get("origin") == "external"}
    for name, provider in _PROVIDERS.items():
        if name not in retained:
            provider.close()
    _PROVIDERS = dict(retained)
    _LOAD_ERRORS = []

    base = path or data_sources_dir()
    base.mkdir(parents=True, exist_ok=True)
    for file in sorted([*base.glob("*.yaml"), *base.glob("*.yml")]):
        try:
            config = load_config(file)
            provider = GenericHTTPProvider(config)
            errors = provider.validate()
            if errors:
                _LOAD_ERRORS.append({"path": str(file), "name": config.name, "errors": errors})
                provider.close()
                continue
            _PROVIDERS[config.name] = provider
        except Exception as e:  # noqa: BLE001
            logger.warning("custom data source load failed %s: %s", file, e)
            _LOAD_ERRORS.append({"path": str(file), "errors": [str(e)]})

    # 内置可选插件 (plugins/ 目录)。与用户 YAML 源独立, 缺依赖只记状态不报错。
    _load_builtin_plugins()


def list_sources() -> list[dict]:
    """只列出用户自定义 (YAML) 源。内置插件 (builtin=True) 由 list_plugins 独立呈现。"""
    return [
        {
            "name": provider.name,
            "display_name": provider.config.display_name,
            "datasets": sorted(provider.config.datasets.keys()),
            "path": str(provider.config.path) if provider.config.path else None,
        }
        for provider in _PROVIDERS.values()
        if not getattr(provider, "builtin", False)
    ]


def list_plugins() -> list[dict]:
    """返回所有内置插件的状态 (含已装/未装), 供设置页独立分类显示。"""
    return [dict(item) for item in _PLUGIN_STATUS.values()]


def _plugin_key_masked(name: str, api_key_env: str) -> str:
    """插件当前生效 Key 的脱敏串 (secrets.json 优先, .env 兜底), 未配置返回空。

    与 TickFlow Key 的展示契约一致 (settings API 的 tickflow_api_key_masked):
    完整 Key 永不出后端, 只出 mask() 结果, 供设置页常驻显示。
    """
    env = str(api_key_env or "").strip()
    if not env:
        return ""
    key = secrets_store.get_env_backed_secret(f"{name.lower()}_api_key", env)
    return secrets_store.mask(key) if key else ""


def plugin_manifest(name: str) -> dict | None:
    """读取指定插件的 plugin.yaml 清单。"""
    if not _NAME_RE.fullmatch(name or ""):
        return None
    from . import packages
    external = packages.manifest(name)
    if external is not None:
        return external
    plugin_dir = plugins_dir() / name
    manifest_path = plugin_dir / "plugin.yaml"
    if not manifest_path.exists():
        return None
    return yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}


def plugin_dir_of(name: str) -> Path:
    """返回插件目录路径。"""
    return plugins_dir() / (name or "")


def probe_plugin_key(name: str, api_key: str) -> tuple[bool, str]:
    """调用插件的 probe_api_key(key) 探测候选 Key(不落盘)。

    约定: 声明了 api_key_env 的插件, 其 entry 模块提供模块级
    probe_api_key(key) -> (ok, reason)。未声明或未提供 → (False, 原因)。
    """
    manifest = plugin_manifest(name)
    if manifest is None:
        return False, f"插件 '{name}' 不存在"
    if not manifest.get("api_key_env"):
        return False, f"插件 '{name}' 不支持在界面配置 Key"
    entry = str(manifest.get("entry") or "")
    if ":" not in entry:
        return False, f"插件 '{name}' entry 非法"
    try:
        if _PLUGIN_STATUS.get(name, {}).get("origin") == "external":
            from . import packages
            if not packages.read_state(name).get("enabled", True):
                return False, "请先启用插件"
            module = packages.import_module(manifest, entry.split(":", 1)[0])
        else:
            module = importlib.import_module(entry.split(":", 1)[0])
    except Exception:  # noqa: BLE001 — do not disclose plugin exception payloads
        return False, "插件模块加载失败，请检查依赖与兼容范围"
    probe = getattr(module, "probe_api_key", None)
    if probe is None:
        return False, f"插件 '{name}' 未提供 Key 探测"
    try:
        ok, reason = probe(api_key)
        return bool(ok), (str(reason).replace(api_key, "[已隐藏]")
                          if _PLUGIN_STATUS.get(name, {}).get("origin") == "external" else str(reason))
    except Exception:  # noqa: BLE001 — plugin exceptions may contain credentials
        return False, "Key 探测失败，请检查凭据、网络与接口权限"


def install_plugin(name: str) -> tuple[bool, str]:
    """安装指定插件的依赖。根据 runtime 执行 npm install / pip install。

    返回 (是否成功, 消息)。成功后调用方应 reload 重新扫描。
    依赖未找到 (npm/pip 缺失) 或命令失败时返回 False。
    """
    manifest = plugin_manifest(name)
    if manifest is None:
        return False, f"插件 '{name}' 不存在或无 plugin.yaml"
    runtime = str(manifest.get("runtime", "none")).lower()
    pdir = plugin_dir_of(name)
    if not pdir.exists():
        return False, f"插件目录不存在: {pdir}"

    try:
        if runtime == "node":
            npm = shutil.which("npm")
            if not npm:
                return False, "未找到 npm, 请先安装 Node.js (>=18)"
            # 在插件目录执行 npm install
            result = subprocess.run(
                [npm, "install", "--omit=dev", "--no-audit", "--no-fund"],
                cwd=str(pdir),
                capture_output=True,
                text=True,
                timeout=300,
            )
        elif runtime == "python":
            import sys
            # Python 型插件: 优先用 uv pip install (uv 管理的 venv 无 pip 模块),
            # 回退 python -m pip。都装进当前后端虚拟环境。
            # 关键: uv 分支必须显式传 --python sys.executable。dev.ps1 直接跑
            # .venv/Scripts/python.exe 而不 activate, 后端进程 VIRTUAL_ENV 为空,
            # uv pip 会默认选 PATH 上的基础解释器 (如 conda base, 常为只读) →
            # 装错环境并 exit 2 (访问拒绝)。--python 锁定后端自身 venv, 与 pip
            # 回退路径 (sys.executable -m pip) 的目标一致。
            # uv 容错: 用户全局 uv.toml 配置错误时 exit 2, 回退 --no-config 重试。
            # UV_HTTP_TIMEOUT=300: akshare 等含大包(如 mini-racer 14MB), 默认 30s 不够。
            req = pdir / "requirements.txt"
            if not req.exists():
                return False, "Python 型插件需要 requirements.txt"
            uv_bin = shutil.which("uv")
            if uv_bin:
                result = subprocess.run(
                    [uv_bin, "pip", "install", "--python", sys.executable, "-r", str(req)],
                    capture_output=True, text=True, timeout=300,
                    env={**__import__("os").environ, "UV_HTTP_TIMEOUT": "300"},
                )
                # exit 2 通常是配置文件解析错误, 绕过配置重试
                # --no-config 会丢镜像, 显式传国内镜像加速 (与用户 uv.toml 意图一致)
                if result.returncode == 2:
                    result = subprocess.run(
                        [uv_bin, "pip", "install", "--no-config",
                         "--index-url", "https://pypi.tuna.tsinghua.edu.cn/simple",
                         "--python", sys.executable,
                         "-r", str(req)],
                        capture_output=True, text=True, timeout=300,
                        env={**__import__("os").environ, "UV_HTTP_TIMEOUT": "300"},
                    )
            else:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", str(req)],
                    capture_output=True, text=True, timeout=300,
                )
        else:
            return False, f"runtime={runtime} 无需安装依赖"
    except subprocess.TimeoutExpired:
        return False, "安装超时 (5分钟), 请检查网络后重试"
    except Exception as e:  # noqa: BLE001
        return False, f"安装失败: {e}"

    if result.returncode != 0:
        # 取 stderr 的第一个 error: 行(真正的错误原因), 避免把 uv 的长字段列表返回给用户
        raw = (result.stderr or result.stdout or "").strip()
        first_err = ""
        for line in raw.splitlines():
            if line.strip().startswith(("error", "Error", "Caused by")):
                first_err = line.strip()
                break
        msg = first_err or raw[-200:]
        return False, f"安装失败 (exit {result.returncode}): {msg}"
    return True, "安装成功"


def uninstall_plugin(name: str) -> tuple[bool, str]:
    """卸载指定插件的依赖。

    node 型: 删除 node_modules 目录 (干净彻底, 下次需要重新 npm install)。
    python 型: pip uninstall (包名从 requirements.txt 推断)。
    """
    import shutil as _shutil

    manifest = plugin_manifest(name)
    if manifest is None:
        return False, f"插件 '{name}' 不存在或无 plugin.yaml"
    runtime = str(manifest.get("runtime", "none")).lower()
    pdir = plugin_dir_of(name)
    if not pdir.exists():
        return False, f"插件目录不存在: {pdir}"

    if runtime == "node":
        nm = pdir / "node_modules"
        if not nm.exists():
            return True, "node_modules 不存在, 无需卸载"
        try:
            _shutil.rmtree(nm)
            return True, "已删除 node_modules"
        except Exception as e:  # noqa: BLE001
            return False, f"删除 node_modules 失败: {e}"

    if runtime == "python":
        req = pdir / "requirements.txt"
        if not req.exists():
            return False, "Python 型插件缺少 requirements.txt, 无法自动卸载"
        # 读 requirements.txt 拿包名, 逐个 pip uninstall -y
        pkgs = [l.strip().split("==")[0].split(">=")[0].strip()
                for l in req.read_text().splitlines()
                if l.strip() and not l.startswith("#")]
        if not pkgs:
            return True, "requirements.txt 无有效包名"
        import sys
        uv_bin = _shutil.which("uv")
        # 与 install 一致: uv 分支显式 --python 锁定后端 venv (无 VIRTUAL_ENV 时
        # 会默认落到 PATH 基础解释器), 与 pip 回退 (sys.executable -m pip) 目标一致。
        cmd = ([uv_bin, "pip", "uninstall", "--python", sys.executable, *pkgs]
               if uv_bin else [sys.executable, "-m", "pip", "uninstall", "-y", *pkgs])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                return False, f"卸载失败: {(result.stderr or '').strip()[-300:]}"
            return True, f"已卸载 {len(pkgs)} 个包"
        except Exception as e:  # noqa: BLE001
            return False, f"卸载失败: {e}"

    return False, f"runtime={runtime} 无需卸载"


def is_builtin(name: str) -> bool:
    """判断 name 是否为内置插件 (不可被用户编辑/删除)。"""
    return ((name or "").lower() in _PLUGIN_STATUS
            and _PLUGIN_STATUS[(name or "").lower()].get("origin") != "external")


def names() -> set[str]:
    return set(_PROVIDERS)


def errors() -> list[dict]:
    return list(_LOAD_ERRORS)


def get_provider(name: str) -> GenericHTTPProvider:
    provider = _PROVIDERS.get((name or "").lower())
    if provider is None:
        raise ValueError(f"Custom data source not found or invalid: {name}")
    return provider


def create_provider(config: dict) -> GenericHTTPProvider:
    """Build a validated temporary provider without writing or registering it."""
    parsed = config_from_dict(_sanitize_for_yaml(config))
    provider = GenericHTTPProvider(parsed)
    errors = provider.validate()
    if errors:
        provider.close()
        raise ValueError("; ".join(errors))
    return provider


def is_custom_provider(name: str) -> bool:
    return (name or "").lower() in _PROVIDERS


def provider_has_dataset(name: str, dataset: str) -> bool:
    """判断某个 custom 源是否配置了指定数据集。

    用于主流程分流: 总开关选了 custom, 但某个数据集未启用时, 该数据集回退 TickFlow。
    """
    provider = _PROVIDERS.get((name or "").lower())
    if provider is None:
        return False
    return dataset in provider.config.datasets


def get_config_dict(name: str) -> dict | None:
    """读取一个已加载 custom 源的原始配置 dict(用于前端编辑回填)。内置插件不可编辑。"""
    if name in _PLUGIN_STATUS:
        return None
    provider = _PROVIDERS.get((name or "").lower())
    if provider is None:
        return None
    return _config_to_dict(provider.config)


def _config_to_dict(config: CustomSourceConfig) -> dict:
    auth = config.auth
    out: dict = {
        "name": config.name,
        "display_name": config.display_name,
        "auth": {
            "type": auth.type,
            **({"token_env": auth.token_env} if auth.token_env else {}),
            **({"header": auth.header} if auth.type in {"bearer", "header"} and auth.header != "Authorization" else {}),
            **({"param": auth.param} if auth.type == "query" and auth.param != "token" else {}),
        },
        "datasets": {},
    }
    for ds_name, ds in config.datasets.items():
        out["datasets"][ds_name] = {
            "url": ds.url,
            "method": ds.method,
            **({"batch": ds.batch} if ds.batch is not None else {}),
            **({"rpm": ds.rpm} if ds.rpm is not None else {}),
            **({"timeout": ds.timeout} if ds.timeout != DEFAULT_TIMEOUT else {}),
            "response_path": ds.response_path,
            "field_map": dict(ds.field_map),
            **({"transforms": dict(ds.transforms)} if ds.transforms else {}),
            **({
                "symbols_param": ds.symbols_param,
                "start_param": ds.start_param,
                "end_param": ds.end_param,
            } if ds_name != "realtime" else {}),
            **({"asset_type_param": ds.asset_type_param} if ds_name == "minute" and ds.asset_type_param else {}),
            **({"freq_param": ds.freq_param} if ds_name == "minute" and ds.freq_param else {}),
            **({"pct_unit": ds.pct_unit} if ds_name == "realtime" and ds.pct_unit else {}),
        }
    return out


def save_config(name: str, config: dict) -> Path:
    """把一份配置 dict 写成 data/data_sources/{name}.yaml, 返回写入路径。"""
    if name in _PLUGIN_STATUS:
        raise ValueError(f"'{name}' 是内置插件, 不可编辑")
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"invalid data source name: {name!r} (only lowercase a-z 0-9 _ allowed)")
    base = data_sources_dir()
    base.mkdir(parents=True, exist_ok=True)
    path = (base / f"{name}.yaml").resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError("invalid data source name: path escape detected")
    cleaned = _sanitize_for_yaml(config)
    atomic_write_text(path, yaml.safe_dump(cleaned, allow_unicode=True, sort_keys=False))
    return path


def delete_config(name: str) -> bool:
    """删除 data/data_sources/{name}.yaml。返回是否真的删除了。"""
    if name in _PLUGIN_STATUS:
        raise ValueError(f"'{name}' 是内置插件, 不可删除")
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"invalid data source name: {name!r}")
    base = data_sources_dir().resolve()
    path = (base / f"{name}.yaml").resolve()
    if not path.is_relative_to(base):
        raise ValueError("invalid data source name: path escape detected")
    if not path.exists():
        return False
    path.unlink()
    return True


def _sanitize_for_yaml(config: dict) -> dict:
    """剔除前端可能塞进来的空值/未启用数据集, 保证写入的 yaml 干净。"""
    out: dict = {
        "name": str(config.get("name", "")).lower(),
        "display_name": str(config.get("display_name") or config.get("name", "")),
    }
    auth_raw = config.get("auth") or {}
    auth_type = str(auth_raw.get("type", "none") or "none").lower()
    auth: dict = {"type": auth_type}
    if auth_type != "none" and auth_raw.get("token_env"):
        auth["token_env"] = str(auth_raw["token_env"])
        if auth_type in {"bearer", "header"} and auth_raw.get("header"):
            auth["header"] = str(auth_raw["header"])
        if auth_type == "query" and auth_raw.get("param"):
            auth["param"] = str(auth_raw["param"])
    out["auth"] = auth

    datasets_out: dict = {}
    for ds_name, ds_cfg in (config.get("datasets") or {}).items():
        if ds_name not in {"daily", "adj_factor", "realtime", "minute", "depth5", "full_minute", "financial"}:
            continue
        if not isinstance(ds_cfg, dict):
            continue
        ds = _sanitize_dataset(ds_name, ds_cfg)
        if ds:
            datasets_out[ds_name] = ds
    out["datasets"] = datasets_out
    return out


def _sanitize_dataset(ds_name: str, ds_cfg: dict) -> dict:
    out: dict = {}
    url = str(ds_cfg.get("url", "") or "").strip()
    if not url:
        return out
    out["url"] = url
    method = str(ds_cfg.get("method", "GET") or "GET").upper()
    out["method"] = method
    if ds_cfg.get("batch") is not None:
        try:
            out["batch"] = int(ds_cfg["batch"])
        except (TypeError, ValueError):
            pass
    if ds_cfg.get("rpm") is not None:
        try:
            out["rpm"] = int(ds_cfg["rpm"])
        except (TypeError, ValueError):
            pass
    if ds_cfg.get("timeout") is not None:
        try:
            timeout = float(ds_cfg["timeout"])
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"{ds_name}: timeout must be a number between 0 and "
                f"{MAX_TIMEOUT:g} seconds"
            ) from e
        if not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT:
            raise ValueError(
                f"{ds_name}: timeout must be between 0 and {MAX_TIMEOUT:g} seconds"
            )
        if timeout != DEFAULT_TIMEOUT:
            out["timeout"] = timeout
    out["response_path"] = str(ds_cfg.get("response_path", "") or "")
    field_map = {
        str(k): str(v)
        for k, v in (ds_cfg.get("field_map") or {}).items()
        if str(k).strip() and str(v).strip()
    }
    if field_map:
        out["field_map"] = field_map
    transforms = {
        str(k): str(v)
        for k, v in (ds_cfg.get("transforms") or {}).items()
        if str(k).strip() and str(v).strip()
    }
    if transforms:
        out["transforms"] = transforms
    if ds_name != "realtime":
        symbols_param = str(ds_cfg.get("symbols_param") or "").strip()
        start_param = str(ds_cfg.get("start_param") or "").strip()
        end_param = str(ds_cfg.get("end_param") or "").strip()
        if symbols_param:
            out["symbols_param"] = symbols_param
        if start_param:
            out["start_param"] = start_param
        if end_param:
            out["end_param"] = end_param
    pct_unit = str(ds_cfg.get("pct_unit") or "").strip().lower()
    if pct_unit:
        if ds_name != "realtime":
            raise ValueError(f"{ds_name}: pct_unit 仅用于 realtime 数据集")
        if pct_unit not in ("percent", "decimal"):
            raise ValueError(f"{ds_name}: pct_unit 必须是 percent 或 decimal")
        out["pct_unit"] = pct_unit
    if ds_name == "minute":
        asset_type_param = str(ds_cfg.get("asset_type_param") or "").strip()
        freq_param = str(ds_cfg.get("freq_param") or "").strip()
        if asset_type_param:
            out["asset_type_param"] = asset_type_param
        if freq_param:
            out["freq_param"] = freq_param
    request_params = [
        out.get("symbols_param", "symbols"),
        out.get("start_param", "start_time"),
        out.get("end_param", "end_time"),
    ]
    if ds_name == "minute":
        request_params.extend(
            name for name in (out.get("asset_type_param"), out.get("freq_param")) if name
        )
    duplicates = sorted({
        name for name in request_params if request_params.count(name) > 1
    })
    if ds_name != "realtime" and duplicates:
        raise ValueError(
            f"{ds_name}: duplicate request parameter names: {', '.join(duplicates)}"
        )
    return out


# ================================================================
# 内置可选插件 (plugins/ 目录) 的发现与注册
# ================================================================

def _load_builtin_plugins() -> None:
    """扫描 plugins/ 目录下每个含 plugin.yaml 的子目录, 动态加载。

    缺依赖时记录 "不可用" 状态, 不抛异常, 不影响主流程。
    每次调用重建 _PLUGIN_STATUS, 并把可用的插件注册进 _PROVIDERS。
    """
    global _PLUGIN_STATUS
    _PLUGIN_STATUS = {name: info for name, info in _PLUGIN_STATUS.items()
                      if info.get("origin") == "external"}
    pdir = plugins_dir()
    for plugin_dir in sorted(pdir.iterdir()) if pdir.exists() else []:
        if not plugin_dir.is_dir():
            continue
        manifest_path = plugin_dir / "plugin.yaml"
        if not manifest_path.exists():
            continue
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            _register_one_plugin(manifest)
        except Exception as e:  # noqa: BLE001
            logger.warning("插件 %s 清单解析失败: %s", plugin_dir.name, e)
    from . import packages
    packages.discover()


def _manifest_status(manifest, available, reason, origin="builtin"):
    name = manifest["name"]
    runtime = str(manifest.get("runtime", "none")).lower()
    return {
        "name": name,
        "origin": origin,
        "display_name": manifest.get("display_name", name),
        "datasets": list(manifest.get("datasets", []) or []),
        "runtime": runtime,
        "available": available,
        "status": reason,
        "description": manifest.get("description", ""),
        "install_hint": manifest.get("install_hint", ""),
        "homepage": manifest.get("homepage", ""),
        "api_key_env": manifest.get("api_key_env", ""),
        "api_key_masked": _plugin_key_masked(name, manifest.get("api_key_env", "")),
        # 计费标识 (可选): 如 "points" → 设置页能力芯片/适配行标注「积分」
        "billing": manifest.get("billing", ""),
        # 计费提示 (可选): 积分消耗与轮询频率/拉取范围的关系, 设置页按警示样式展示
        "billing_note": manifest.get("billing_note", ""),
        # 使用要点 (可选): 各能力的规模/节奏说明, 设置页详情卡以列表展示
        "usage_notes": list(manifest.get("usage_notes", []) or []),
        # Key/账务管理页 (可选): 设置页 Key 配置区展示入口
        "key_manage_url": manifest.get("key_manage_url", ""),
    }


def _register_one_plugin(manifest: dict) -> None:
    """注册单个插件: 委托自检 → 可用则动态 import entry 注册进 _PROVIDERS。"""
    name = manifest.get("name")
    if not name or not _NAME_RE.match(name):
        logger.warning("插件清单缺少合法 name: %r", name)
        return
    # hidden: 已加载但对 UI 隐藏 (功能未完成/暂不开放), 不注册不展示
    if manifest.get("hidden"):
        logger.info("插件 %s 标记为 hidden, 跳过注册", name)
        return
    runtime = str(manifest.get("runtime", "none")).lower()
    # 委托检测: 调用插件自己的 check 函数 (node 型/python 型各自实现)
    available, reason = _call_check(manifest.get("check"))
    _PLUGIN_STATUS[name] = _manifest_status(manifest, available, reason)
    if not available:
        old = _PROVIDERS.pop(name, None)
        if old is not None:
            old.close()
        return  # 不就绪时撤下旧实例，避免旧凭据继续生效
    # 可用 → 动态加载 provider 类并实例化
    try:
        provider_cls = _load_entry(manifest["entry"])
        provider = provider_cls() if isinstance(provider_cls, type) else provider_cls
        provider.builtin = True  # 标记为内置 (list_sources 过滤, 不可被用户编辑/删除)
        old = _PROVIDERS.get(name)
        if isinstance(old, ManagedProvider):
            old.replace(provider)
        else:
            _PROVIDERS[name] = ManagedProvider(provider)
        logger.info("内置插件 %s 已注册 (runtime=%s)", name, runtime)
    except Exception as e:  # noqa: BLE001
        # 声称可用但 import 失败 → 标记不可用, 避免启动崩溃
        _PLUGIN_STATUS[name]["available"] = False
        _PLUGIN_STATUS[name]["status"] = f"加载失败: {e}"
        logger.warning("插件 %s provider 加载失败: %s", name, e)


def _call_check(check_ref: str | None) -> tuple[bool, str]:
    """调用插件清单里指定的可用性检测函数, 返回 (是否可用, 原因)。

    check_ref 格式 "module.path:func_name"。无 check 字段时视为可用。
    """
    if not check_ref:
        return True, "ok"
    try:
        func = _load_entry(check_ref)
        result = func()
        # 兼容两种返回: (bool, str) 或 bool
        if isinstance(result, tuple):
            return bool(result[0]), str(result[1])
        return bool(result), "ok" if result else "不可用"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _load_entry(entry_ref: str):
    """动态加载 'module.path:attr' 形式的引用, 返回属性对象 (类或函数)。"""
    if ":" not in entry_ref:
        raise ValueError(f"entry 格式应为 'module.path:attr', 得到: {entry_ref!r}")
    module_path, attr = entry_ref.split(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, attr)


class _UnavailableProvider:
    """Keep installed identities so preferences cannot silently switch vendors."""

    def __init__(self, doc, reason):
        self.name = doc["name"]
        self.config = SimpleNamespace(datasets=dict.fromkeys(doc.get("datasets", [])))
        self.reason = reason

    def close(self):
        pass

    def __getattr__(self, name):
        if not name.startswith(("get_", "iter_")):
            raise AttributeError(name)
        def unavailable(*args, **kwargs):
            raise ProviderRequestError(f"数据源 {self.name} 不可用：{self.reason}")
        return unavailable


def record_external_error(name):
    if name in _PROVIDERS and _PLUGIN_STATUS.get(name, {}).get("origin") != "external":
        logger.warning("external plugin name conflict: %s", name)
        return
    from .packages import DATASETS
    doc = {"name": name, "datasets": sorted(DATASETS), "version": ""}
    publish_external(doc, None, "插件加载失败，请检查清单与兼容范围", metadata=False)


def publish_external(doc, provider, reason, *, metadata=True):
    name = doc["name"]
    available = provider is not None
    value = provider if available else _UnavailableProvider(doc, reason)
    old = _PROVIDERS.get(name)
    if isinstance(old, ManagedProvider):
        old.replace(value)
    else:
        _PROVIDERS[name] = ManagedProvider(value)
    _PLUGIN_STATUS[name] = {
        **_manifest_status(doc, available, reason, origin="external"),
        "version": doc.get("version", ""), "author": doc.get("author", ""),
        "can_test": available and callable(getattr(provider, "test_dataset", None)),
    }
    if metadata:
        _PROVIDERS[name]._external_signature = _external_signature(doc)
        refresh_external_metadata(name)


def _external_signature(doc):
    from . import packages
    state = packages.read_state(doc["name"]) or {}
    key = secrets_store.get_env_backed_secret(f"{doc['name']}_api_key", doc.get("api_key_env", ""))
    return (doc.get("version"), state.get("enabled", True), hashlib.sha256(key.encode()).digest())


def refresh_external_metadata(name):
    from . import packages
    state = packages.read_state(name)
    if state and name in _PLUGIN_STATUS:
        _PLUGIN_STATUS[name].update({
            "pending_version": state.get("pending"), "previous_version": state.get("previous"),
            "enabled": state.get("enabled", True), "pending_remove": state.get("remove", False),
            "update_error": state.get("error"),
        })


def refresh_plugin(name, *, manifest_doc=None, strict=False):
    """Refresh one plugin without closing other providers or in-flight requests."""
    from . import packages
    doc = manifest_doc or plugin_manifest(name)
    if doc is None:
        raise ValueError("插件不存在")
    state = packages.read_state(name)
    if state is None:
        _register_one_plugin(doc)
        return
    if (manifest_doc is not None and name in _PROVIDERS
            and _PLUGIN_STATUS.get(name, {}).get("origin") == "external"
            and getattr(_PROVIDERS[name], "_external_signature", None) == _external_signature(doc)):
        refresh_external_metadata(name)
        return
    if not state.get("enabled", True):
        publish_external(doc, None, "待重启卸载" if state.get("remove") else "已停用")
        return
    try:
        provider, reason = packages.build_provider(doc)
        if strict and provider is None:
            raise ValueError("插件配置未就绪")
    except Exception:  # noqa: BLE001 — uploaded exceptions can contain credentials
        if strict:
            raise ValueError("插件初始化失败，原配置保持不变") from None
        publish_external(doc, None, "插件加载失败，请检查依赖与 Provider 实现")
        return
    publish_external(doc, provider, reason)


# 模块导入时即扫描一次, 保证 names()/_allowed_data_providers() 在 startup 前可用。
_load_builtin_plugins()
