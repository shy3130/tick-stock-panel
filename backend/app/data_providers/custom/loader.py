"""Load custom data source definitions from user data files."""
from __future__ import annotations

import importlib
import logging
import re
import shutil
import subprocess
from pathlib import Path

import yaml

from app.config import settings
from app.data_providers.custom.config import CustomSourceConfig, load_config
from app.data_providers.custom.provider import GenericHTTPProvider

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
    for provider in _PROVIDERS.values():
        provider.close()
    _PROVIDERS = {}
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
    return list(_PLUGIN_STATUS.values())


def plugin_manifest(name: str) -> dict | None:
    """读取指定插件的 plugin.yaml 清单。"""
    plugin_dir = plugins_dir() / (name or "")
    manifest_path = plugin_dir / "plugin.yaml"
    if not manifest_path.exists():
        return None
    return yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}


def plugin_dir_of(name: str) -> Path:
    """返回插件目录路径。"""
    return plugins_dir() / (name or "")


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
            # Python 型插件: 优先用 uv pip install (uv 管理的 venv 无 pip 模块),
            # 回退 python -m pip。都装进当前后端虚拟环境。
            # uv 容错: 用户全局 uv.toml 配置错误时 exit 2, 回退 --no-config 重试。
            # UV_HTTP_TIMEOUT=300: akshare 等含大包(如 mini-racer 14MB), 默认 30s 不够。
            req = pdir / "requirements.txt"
            if not req.exists():
                return False, "Python 型插件需要 requirements.txt"
            uv_bin = shutil.which("uv")
            if uv_bin:
                result = subprocess.run(
                    [uv_bin, "pip", "install", "-r", str(req)],
                    capture_output=True, text=True, timeout=300,
                    env={**__import__("os").environ, "UV_HTTP_TIMEOUT": "300"},
                )
                # exit 2 通常是配置文件解析错误, 绕过配置重试
                # --no-config 会丢镜像, 显式传国内镜像加速 (与用户 uv.toml 意图一致)
                if result.returncode == 2:
                    result = subprocess.run(
                        [uv_bin, "pip", "install", "--no-config",
                         "--index-url", "https://pypi.tuna.tsinghua.edu.cn/simple",
                         "-r", str(req)],
                        capture_output=True, text=True, timeout=300,
                        env={**__import__("os").environ, "UV_HTTP_TIMEOUT": "300"},
                    )
            else:
                import sys
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
        uv_bin = _shutil.which("uv")
        cmd = [uv_bin, "pip", "uninstall", *pkgs] if uv_bin else None
        if cmd is None:
            import sys
            cmd = [sys.executable, "-m", "pip", "uninstall", "-y", *pkgs]
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
    return (name or "").lower() in _PLUGIN_STATUS


def names() -> set[str]:
    return set(_PROVIDERS)


def errors() -> list[dict]:
    return list(_LOAD_ERRORS)


def get_provider(name: str) -> GenericHTTPProvider:
    provider = _PROVIDERS.get((name or "").lower())
    if provider is None:
        raise ValueError(f"Custom data source not found or invalid: {name}")
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
    if is_builtin(name):
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
            "response_path": ds.response_path,
            "field_map": dict(ds.field_map),
            **({"transforms": dict(ds.transforms)} if ds.transforms else {}),
            "symbols_param": ds.symbols_param,
            "start_param": ds.start_param,
            "end_param": ds.end_param,
            **({"asset_type_param": ds.asset_type_param} if ds.asset_type_param else {}),
            **({"freq_param": ds.freq_param} if ds.freq_param else {}),
        }
    return out


def save_config(name: str, config: dict) -> Path:
    """把一份配置 dict 写成 data/data_sources/{name}.yaml, 返回写入路径。"""
    if is_builtin(name):
        raise ValueError(f"'{name}' 是内置插件, 不可编辑")
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"invalid data source name: {name!r} (only lowercase a-z 0-9 _ allowed)")
    base = data_sources_dir()
    base.mkdir(parents=True, exist_ok=True)
    path = (base / f"{name}.yaml").resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError("invalid data source name: path escape detected")
    cleaned = _sanitize_for_yaml(config)
    path.write_text(yaml.safe_dump(cleaned, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def delete_config(name: str) -> bool:
    """删除 data/data_sources/{name}.yaml。返回是否真的删除了。"""
    if is_builtin(name):
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
        if ds_name not in {"daily", "adj_factor", "realtime", "minute", "financial"}:
            continue
        if not isinstance(ds_cfg, dict):
            continue
        ds = _sanitize_dataset(ds_cfg)
        if ds:
            datasets_out[ds_name] = ds
    out["datasets"] = datasets_out
    return out


def _sanitize_dataset(ds_cfg: dict) -> dict:
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
    if ds_cfg.get("symbols_param"):
        out["symbols_param"] = str(ds_cfg["symbols_param"])
    if ds_cfg.get("start_param"):
        out["start_param"] = str(ds_cfg["start_param"])
    if ds_cfg.get("end_param"):
        out["end_param"] = str(ds_cfg["end_param"])
    if ds_cfg.get("asset_type_param"):
        out["asset_type_param"] = str(ds_cfg["asset_type_param"])
    if ds_cfg.get("freq_param"):
        out["freq_param"] = str(ds_cfg["freq_param"])
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
    _PLUGIN_STATUS = {}
    pdir = plugins_dir()
    if not pdir.exists():
        return
    for plugin_dir in sorted(pdir.iterdir()):
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


def _register_one_plugin(manifest: dict) -> None:
    """注册单个插件: 委托自检 → 可用则动态 import entry 注册进 _PROVIDERS。"""
    name = manifest.get("name")
    if not name or not _NAME_RE.match(name):
        logger.warning("插件清单缺少合法 name: %r", name)
        return
    runtime = str(manifest.get("runtime", "none")).lower()
    # 委托检测: 调用插件自己的 check 函数 (node 型/python 型各自实现)
    available, reason = _call_check(manifest.get("check"))
    _PLUGIN_STATUS[name] = {
        "name": name,
        "display_name": manifest.get("display_name", name),
        "datasets": list(manifest.get("datasets", []) or []),
        "runtime": runtime,
        "available": available,
        "status": reason,
        "description": manifest.get("description", ""),
        "install_hint": manifest.get("install_hint", ""),
    }
    if not available:
        return  # 依赖没装: 不注册, 但状态已记录供 UI 显示
    # 可用 → 动态加载 provider 类并实例化
    try:
        provider_cls = _load_entry(manifest["entry"])
        provider = provider_cls() if isinstance(provider_cls, type) else provider_cls
        provider.builtin = True  # 标记为内置 (list_sources 过滤, 不可被用户编辑/删除)
        _PROVIDERS[name] = provider
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


# 模块导入时即扫描一次, 保证 names()/_allowed_data_providers() 在 startup 前可用。
_load_builtin_plugins()
