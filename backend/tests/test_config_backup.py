"""配置备份 (导出/导入) — 数据契约与安全边界。

覆盖: 导出打包与脱敏、inspect 预览与冲突统计、恶意 ZIP 拒绝、
apply 的冲突/merge/逐条容错语义、策略安全校验 fail-closed。
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import polars as pl
import pytest

from app.services import config_backup

SAFE_STRATEGY = '''\
META = {"id": "custom_demo", "name": "demo", "description": "", "tags": []}

import polars as pl

def compute(df: pl.DataFrame) -> pl.DataFrame:
    return df
'''

DEP_STRATEGY_HELPER = "def helper(x):\n    return x\n"

STRATEGY_WITH_DEP = '''\
META = {"id": "custom_withdep", "name": "wd", "description": "", "tags": []}

import _helper

def compute(df):
    return _helper.helper(df)
'''

UNSAFE_STRATEGY = '''\
META = {"id": "custom_evil", "name": "evil", "description": "", "tags": []}

import os

def compute(df):
    return os.system("rm -rf /")
'''


def _write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture()
def src_env(tmp_path, monkeypatch):
    """源环境: 把全局 data_dir 指到 tmp, 造一份各类别齐全的用户配置。"""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "data_dir", tmp_path)
    root = tmp_path
    _write(root / "strategies" / "custom" / "custom_demo.py", SAFE_STRATEGY)
    _write(root / "strategies" / "custom" / "_helper.py", DEP_STRATEGY_HELPER)
    _write(root / "strategies" / "custom" / "custom_withdep.py", STRATEGY_WITH_DEP)
    _write(root / "user_data" / "strategy_overrides" / "custom_demo.json", '{"params": {"n": 5}}')
    _write(root / "user_data" / "custom_signals" / "cs_demo.json", '{"id": "cs_demo", "name": "s"}')
    _write(root / "user_data" / "custom_factors" / "uf_demo.json", '{"id": "uf_demo", "formula": "close"}')
    _write(root / "user_data" / "monitor_rules" / "mr_demo.json",
           json.dumps({"id": "mr_demo", "name": "r", "webhook_url": "https://hook/secret",
                       "webhook_channels": ["feishu"]}))
    _write(root / "user_data" / "watchlist_groups.json", '[{"name": "g1", "symbols": ["000001.SZ"]}]')
    pl.DataFrame({"symbol": ["000001.SZ"], "name": ["平安银行"]}).write_parquet(
        root / "user_data" / "watchlist.parquet"
    )
    _write(root / "data_sources" / "my_src.yaml", "name: my_src\nbase_url: https://x\n")
    _write(root / "analysis_menus" / "am_demo.json", '{"id": "am_demo", "title": "m"}')
    _write(root / "ext_data" / "ext1" / "config.json", '{"id": "ext1", "url": "https://y"}')
    _write(root / "user_data" / "preferences.json", json.dumps({
        "quote_interval": 5,
        "feishu_webhook_url": "https://open.feishu.cn/hook/tok",
        "feishu_webhook_secret": "s3cret",
        "wecom_bot_secret": "bot-s3cret",
    }))
    _write(root / "user_data" / "secrets.json", json.dumps({"ai_api_key": "sk-1", "tickflow_api_key": "tf-1"}))
    # 彻底迁移范围内的其余类别
    _write(root / "user_data" / "lots" / "lot_a.json", '{"id": "lot_a", "symbol": "000001.SZ"}')
    _write(root / "paper" / "accounts" / "acc1" / "account.json", '{"id": "acc1", "cash": 100000}')
    _write(root / "paper" / "accounts" / "acc1" / "fills.jsonl", '{"seq": 1}\n{"seq": 2}\n')
    _write(root / "paper" / "accounts" / "acc1" / "nav" / "daily.jsonl", '{"nav": 100000}\n')
    _write(root / "user_data" / "research_candidates.json", '[{"id": "rc1"}]')
    _write(root / "ext_pages" / "mypage" / "page.json", '{"id": "mypage", "title": "p"}')
    _write(root / "ext_pages" / "mypage" / "bundle.js", "export default function P() { return null }")
    _write(root / "sound_packs" / "ding.mp3", b"ID3fakeaudio")
    _write(root / "user_data" / "alerts.jsonl", '{"msg": "m"}\n')
    _write(root / "user_data" / "assistant_sessions.json", '{"sessions": []}')
    _write(root / "user_data" / "auth.json", json.dumps({"password_hash": "h", "sessions": {}}))
    # 派生/临时数据, 任何情况下都不应进入备份
    _write(root / "user_data" / "strategy_cache.json", '{"derived": 1}')
    _write(root / "user_data" / "task_registry.json", '{"tasks": []}')
    yield root


@pytest.fixture(autouse=True)
def _reset_store_caches():
    """preferences/secrets 是 mtime 签名缓存, 换 data_dir 前后都要清, 防串台。"""
    from app.services import preferences

    preferences._invalidate_cache()
    yield
    preferences._invalidate_cache()
    import app.secrets_store as secrets_store

    secrets_store._cache = None
    secrets_store._cache_sig = None


def _zip_entries(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def test_export_contains_all_categories_and_manifest(src_env):
    data, filename = config_backup.build_backup()
    assert filename.startswith("tsp-config-backup-") and filename.endswith(".zip")
    entries = _zip_entries(data)
    assert "manifest.json" in entries
    expected = {
        "strategies/custom/custom_demo.py",
        "strategies/custom/_helper.py",
        "strategies/custom/custom_withdep.py",
        "user_data/strategy_overrides/custom_demo.json",
        "user_data/custom_signals/cs_demo.json",
        "user_data/custom_factors/uf_demo.json",
        "user_data/monitor_rules/mr_demo.json",
        "user_data/watchlist.parquet",
        "user_data/watchlist_groups.json",
        "user_data/lots/lot_a.json",
        "paper/accounts/acc1/account.json",
        "paper/accounts/acc1/fills.jsonl",
        "paper/accounts/acc1/nav/daily.jsonl",
        "user_data/research_candidates.json",
        "data_sources/my_src.yaml",
        "analysis_menus/am_demo.json",
        "ext_pages/mypage/page.json",
        "ext_pages/mypage/bundle.js",
        "ext_data/ext1/config.json",
        "sound_packs/ding.mp3",
        "user_data/alerts.jsonl",
        "user_data/assistant_sessions.json",
        "user_data/preferences.json",
    }
    assert expected <= set(entries)
    # 敏感与派生数据默认不打包
    assert "user_data/auth.json" not in entries
    assert "user_data/secrets.json" not in entries
    assert "user_data/strategy_cache.json" not in entries
    assert "user_data/task_registry.json" not in entries

    manifest = json.loads(entries["manifest.json"])
    assert manifest["format"] == config_backup.BACKUP_FORMAT
    assert manifest["version"] == config_backup.BACKUP_VERSION
    assert manifest["include_secrets"] is False
    counts = {c["key"]: c["count"] for c in manifest["categories"]}
    assert counts["strategies"] == 3
    assert counts["watchlist"] == 2
    assert counts["paper"] == 3
    assert counts["ext_pages"] == 2
    assert "auth" not in counts and "secrets" not in counts


def test_export_sanitizes_secrets_by_default(src_env):
    entries = _zip_entries(config_backup.build_backup()[0])
    prefs = json.loads(entries["user_data/preferences.json"])
    assert prefs["quote_interval"] == 5
    for key in config_backup.SENSITIVE_PREFERENCE_KEYS:
        assert key not in prefs
    rule = json.loads(entries["user_data/monitor_rules/mr_demo.json"])
    assert "webhook_url" not in rule
    assert rule["webhook_channels"] == ["feishu"]


def test_export_with_secrets_keeps_plaintext(src_env):
    entries = _zip_entries(config_backup.build_backup(categories=config_backup.CATEGORY_KEYS)[0])
    assert "user_data/secrets.json" in entries
    prefs = json.loads(entries["user_data/preferences.json"])
    assert prefs["feishu_webhook_secret"] == "s3cret"
    rule = json.loads(entries["user_data/monitor_rules/mr_demo.json"])
    assert rule["webhook_url"] == "https://hook/secret"
    manifest = json.loads(entries["manifest.json"])
    assert manifest["include_secrets"] is True


def test_inspect_reports_counts_conflicts_and_secrets(src_env):
    data, _ = config_backup.build_backup(categories=config_backup.CATEGORY_KEYS)
    summary = config_backup.inspect_archive(data)
    assert summary["has_secrets"] is True
    # 23 个常规配置文件 + secrets + auth
    assert summary["total_files"] == 25
    assert summary["conflict_files"] == 25 - 2  # 除偏好/密钥 (merge 语义) 外全部同名冲突
    cats = {c["key"]: c for c in summary["categories"]}
    assert cats["strategies"]["count"] == 3
    assert cats["strategies"]["conflicts"] == 3
    assert cats["preferences"]["conflicts"] == 0
    assert cats["auth"]["count"] == 1


def test_reject_invalid_archives(src_env):
    with pytest.raises(config_backup.ConfigBackupError):
        config_backup.inspect_archive(b"not a zip at all")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("user_data/monitor_rules/x.json", "{}")
    with pytest.raises(config_backup.ConfigBackupError, match="manifest"):
        config_backup.inspect_archive(buf.getvalue())

    def _zip_with_manifest(filename: str, content: bytes, manifest: object) -> bytes:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            if not isinstance(manifest, bytes):
                manifest = json.dumps(manifest)
            zf.writestr("manifest.json", manifest)
            zf.writestr(filename, content)
        return out.getvalue()

    good_manifest = {
        "format": config_backup.BACKUP_FORMAT,
        "version": config_backup.BACKUP_VERSION,
    }
    with pytest.raises(config_backup.ConfigBackupError, match="路径"):
        config_backup.inspect_archive(_zip_with_manifest("../evil.py", "x", good_manifest))
    with pytest.raises(config_backup.ConfigBackupError, match="路径"):
        config_backup.inspect_archive(_zip_with_manifest("..\\evil.py", "x", good_manifest))
    with pytest.raises(config_backup.ConfigBackupError, match="路径"):
        config_backup.inspect_archive(_zip_with_manifest("/etc/passwd", "x", good_manifest))
    with pytest.raises(config_backup.ConfigBackupError, match="版本"):
        config_backup.inspect_archive(_zip_with_manifest(
            "user_data/monitor_rules/x.json", "{}",
            {"format": config_backup.BACKUP_FORMAT, "version": 99},
        ))
    with pytest.raises(config_backup.ConfigBackupError, match="不是本应用"):
        config_backup.inspect_archive(_zip_with_manifest(
            "user_data/monitor_rules/x.json", "{}", {"format": "other", "version": 1},
        ))
    # 防炸弹: 声明尺寸超单文件上限直接拒绝 (重复字节压缩后很小, 测试开销低)
    with pytest.raises(config_backup.ConfigBackupError, match="单文件"):
        config_backup.inspect_archive(_zip_with_manifest(
            "user_data/monitor_rules/big.json",
            b"x" * (config_backup.MAX_FILE_BYTES + 1),
            good_manifest,
        ))


def test_inspect_counts_out_of_scope_entries(src_env):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "format": config_backup.BACKUP_FORMAT,
            "version": config_backup.BACKUP_VERSION,
        }))
        zf.writestr("user_data/unknown_store/x.json", "{}")
        zf.writestr("strategies/builtin/x.py", "y = 1\n")
    summary = config_backup.inspect_archive(out.getvalue())
    assert summary["out_of_scope_files"] == 2
    assert summary["total_files"] == 0


def _fresh_env(tmp_path_factory, monkeypatch):
    """全新环境 (模拟新设备): 只重定向 data_dir, 不造任何文件。"""
    from app.config import settings as app_settings

    root = tmp_path_factory.mktemp("fresh")
    monkeypatch.setattr(app_settings, "data_dir", root)
    return root


def test_apply_into_fresh_env_roundtrip(src_env, tmp_path_factory, monkeypatch):
    data, _ = config_backup.build_backup(categories=config_backup.CATEGORY_KEYS)
    root = _fresh_env(tmp_path_factory, monkeypatch)
    result = config_backup.apply_archive(data)

    imported = {c["key"]: c["imported"] for c in result["categories"]}
    assert imported["strategies"] == 3
    assert imported["monitor_rules"] == 1
    assert imported["watchlist"] == 2
    assert imported["preferences"] == 1
    assert imported["secrets"] == 1
    assert imported["auth"] == 1
    assert imported["lots"] == 1
    assert imported["paper"] == 3
    assert imported["research_candidates"] == 1
    assert imported["ext_pages"] == 2
    assert imported["sound_packs"] == 1
    assert imported["history"] == 2
    assert all(not c["failed"] for c in result["categories"])
    assert result["reload_hints"] == {
        "strategies": True, "monitor_rules": True, "data_sources": True, "watchlist": True,
    }

    assert (root / "strategies" / "custom" / "custom_demo.py").read_text(encoding="utf-8") == SAFE_STRATEGY
    assert (root / "user_data" / "watchlist.parquet").is_file()
    # 嵌套路径与二进制类别按原样落盘
    assert (root / "paper" / "accounts" / "acc1" / "nav" / "daily.jsonl").is_file()
    assert (root / "sound_packs" / "ding.mp3").read_bytes() == b"ID3fakeaudio"

    # 回环: 新环境再导出, 条目集合一致
    data2, _ = config_backup.build_backup(categories=config_backup.CATEGORY_KEYS)
    assert set(_zip_entries(data2)) == set(_zip_entries(data))


def test_apply_conflict_default_skip_and_overwrite(src_env):
    data, _ = config_backup.build_backup()
    local = src_env / "strategies" / "custom" / "custom_demo.py"
    local.write_text("META = {'id': 'local'}\n", encoding="utf-8")
    (src_env / "strategies" / "custom" / "_helper.py").unlink()
    (src_env / "strategies" / "custom" / "custom_withdep.py").unlink()

    result = config_backup.apply_archive(data)
    strategies = next(c for c in result["categories"] if c["key"] == "strategies")
    assert strategies["skipped_conflict"] == 1
    assert strategies["imported"] == 2  # 另两个策略已删, 无冲突
    assert local.read_text(encoding="utf-8") == "META = {'id': 'local'}\n"  # 未被覆盖

    result2 = config_backup.apply_archive(data, allow_overwrite=True)
    strategies2 = next(c for c in result2["categories"] if c["key"] == "strategies")
    assert strategies2["imported"] == 3
    assert local.read_text(encoding="utf-8") == SAFE_STRATEGY


def test_apply_preferences_merges_not_replaces(src_env):
    from app.services import preferences

    data, _ = config_backup.build_backup()  # 备份内 quote_interval=5, 无 local_only_key
    preferences.save({"local_only_key": "keep", "quote_interval": 1})
    result = config_backup.apply_archive(data)
    assert result["preferences_merged"] is True

    after = preferences.load()
    assert after["quote_interval"] == 5       # 备份键覆盖
    assert after["local_only_key"] == "keep"  # 本地新增键保留 (merge 而非整替)
    # 备份导出时已脱敏 → 导入不清掉本地已有敏感键, 也不写入新的
    assert after["feishu_webhook_secret"] == "s3cret"


def test_apply_secrets_via_store(src_env, tmp_path_factory, monkeypatch):
    data, _ = config_backup.build_backup(categories=config_backup.CATEGORY_KEYS)
    root = _fresh_env(tmp_path_factory, monkeypatch)
    (root / "user_data").mkdir(parents=True)
    (root / "user_data" / "secrets.json").write_text(
        json.dumps({"local_key": "lv"}), encoding="utf-8"
    )
    result = config_backup.apply_archive(data)
    assert result["secrets_merged"] is True

    import app.secrets_store as secrets_store

    secrets_store._cache = None
    secrets_store._cache_sig = None
    merged = secrets_store.load()
    assert merged["ai_api_key"] == "sk-1"
    assert merged["local_key"] == "lv"


def test_apply_skips_unsafe_strategy(src_env):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "format": config_backup.BACKUP_FORMAT,
            "version": config_backup.BACKUP_VERSION,
        }))
        zf.writestr("strategies/custom/custom_evil.py", UNSAFE_STRATEGY)
        zf.writestr("user_data/custom_signals/cs_ok.json", '{"id": "cs_ok"}')
    result = config_backup.apply_archive(out.getvalue())
    strategies = next(c for c in result["categories"] if c["key"] == "strategies")
    assert strategies["imported"] == 0
    assert len(strategies["failed"]) == 1 and "custom_evil.py" in strategies["failed"][0]["file"]
    assert not (src_env / "strategies" / "custom" / "custom_evil.py").exists()
    # 单条失败不影响其他条目
    signals = next(c for c in result["categories"] if c["key"] == "custom_signals")
    assert signals["imported"] == 1


def test_apply_strategy_with_dependency_passes_safety(src_env):
    data, _ = config_backup.build_backup()
    # 本地删掉策略后重新导入: 依赖文件参与白名单, 不应被误杀
    for name in ("custom_demo.py", "_helper.py", "custom_withdep.py"):
        (src_env / "strategies" / "custom" / name).unlink()
    result = config_backup.apply_archive(data)
    strategies = next(c for c in result["categories"] if c["key"] == "strategies")
    assert strategies["imported"] == 3
    assert strategies["failed"] == []


def test_apply_invalid_payload_reports_and_continues(src_env):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "format": config_backup.BACKUP_FORMAT,
            "version": config_backup.BACKUP_VERSION,
        }))
        zf.writestr("user_data/monitor_rules/broken.json", "{not json")
        zf.writestr("data_sources/bad.yaml", "name: [unclosed")
        zf.writestr("analysis_menus/ok.json", '{"id": "am_ok"}')
    result = config_backup.apply_archive(out.getvalue())
    by_key = {c["key"]: c for c in result["categories"]}
    assert by_key["monitor_rules"]["failed"] and by_key["monitor_rules"]["imported"] == 0
    assert by_key["data_sources"]["failed"] and by_key["data_sources"]["imported"] == 0
    assert by_key["analysis_menus"]["imported"] == 1


def test_export_category_selection(src_env):
    data, _ = config_backup.build_backup(categories={"custom_signals", "custom_factors", "非法键"})
    entries = _zip_entries(data)
    assert set(entries) == {
        "manifest.json",
        "user_data/custom_signals/cs_demo.json",
        "user_data/custom_factors/uf_demo.json",
    }
    manifest = json.loads(entries["manifest.json"])
    assert manifest["include_secrets"] is False
    counts = {c["key"]: c["count"] for c in manifest["categories"]}
    assert set(counts) == {"custom_signals", "custom_factors"}


def test_apply_category_selection(src_env, tmp_path_factory, monkeypatch):
    from app.config import settings as app_settings

    data, _ = config_backup.build_backup()  # 全量备份 (不含密钥)
    root = _fresh_env(tmp_path_factory, monkeypatch)
    result = config_backup.apply_archive(data, categories={"monitor_rules", "preferences"})

    assert result["skipped_unselected"] > 0
    assert result["preferences_merged"] is True
    assert result["secrets_merged"] is False
    by_key = {c["key"]: c for c in result["categories"]}
    assert set(by_key) == {"monitor_rules", "preferences"}
    assert by_key["monitor_rules"]["imported"] == 1
    assert result["reload_hints"] == {
        "strategies": False, "monitor_rules": True, "data_sources": False, "watchlist": False,
    }
    # 未勾选的类别确实没有落盘
    assert (root / "user_data" / "monitor_rules" / "mr_demo.json").is_file()
    assert not (root / "strategies" / "custom" / "custom_demo.py").exists()
    assert not (root / "user_data" / "watchlist.parquet").exists()

    # 空选择 = fail-closed 什么都不写, 不回退成全量
    monkeypatch.setattr(app_settings, "data_dir", root)
    empty = config_backup.apply_archive(data, categories=frozenset())
    assert empty["categories"] == []
    assert empty["preferences_merged"] is False


def test_sensitive_categories_require_explicit_selection(src_env):
    """密钥/访问凭证只在显式勾选时导出; 显式勾选 auth 时带上 auth.json。"""
    default = _zip_entries(config_backup.build_backup()[0])
    assert "user_data/auth.json" not in default
    assert "user_data/secrets.json" not in default

    data, _ = config_backup.build_backup(categories={"auth"})
    entries = _zip_entries(data)
    assert set(entries) == {"manifest.json", "user_data/auth.json"}

    # 导入端: 嵌套白名单外的伪造条目仍被拒绝
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "format": config_backup.BACKUP_FORMAT,
            "version": config_backup.BACKUP_VERSION,
        }))
        zf.writestr("paper/accounts/acc1/evil.sh", "x")
        zf.writestr("paper/accounts/../escape.json", "{}")
    with pytest.raises(config_backup.ConfigBackupError, match="路径"):
        config_backup.inspect_archive(out.getvalue())
    out2 = io.BytesIO()
    with zipfile.ZipFile(out2, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "format": config_backup.BACKUP_FORMAT,
            "version": config_backup.BACKUP_VERSION,
        }))
        zf.writestr("paper/accounts/acc1/evil.sh", "x")
    summary = config_backup.inspect_archive(out2.getvalue())
    assert summary["out_of_scope_files"] == 1
