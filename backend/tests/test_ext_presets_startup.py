"""内置概念/行业 preset 启动契约。

出厂即带【完整可用的拉取配方】(URL / 鉴权 / 两级请求 fanout), 不是空壳 —
「数据」页展开表 → 拉取 就能看到一份真实可跑的配方, 供接入自有源照抄。

ensure_builtin_presets 的契约:
  - 只创建/补齐 config.json, 不拉取数据 (数据由调度器或用户手动触发)
  - 创建仅发生在首次安装 (播种标记缺失); 已播种后删除的内置表不复活
  - 空壳 / 历代出厂配方 → 补齐/替换为当前配方 (不受播种标记限制)
  - 用户自定义过 pull (URL 非出厂值) 或 description → 一律不碰
  - 失败只记 warning, 不阻断启动
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.services.ext_data import ExtConfigStore, PullConfig
from app.services.ext_presets import (
    _RETIRED_FACTORY_PULLS,
    _concept_preset,
    _industry_preset,
    ensure_builtin_presets,
)

_PRESET_IDS = ("ext_gn_ths", "ext_hy_ths")


def test_builtin_presets_ship_with_complete_pull() -> None:
    """出厂 preset 自带完整拉取配方: URL / 鉴权 / 两级请求一应俱全。"""
    for preset in (_concept_preset(), _industry_preset()):
        pull = preset.pull
        assert pull is not None, f"{preset.id} 出厂未带拉取配方"
        assert pull.url.startswith("https://"), f"{preset.id} 缺少第一级 URL"
        assert pull.response_path, f"{preset.id} 缺少响应路径"
        assert pull.auth and pull.auth.get("type") == "header", f"{preset.id} 缺少鉴权配置"
        # 凭据复用 fuyao 行情插件, 用户只需在一处填 Key
        assert pull.auth.get("key_ref") == "fuyao_api_key", f"{preset.id} 未复用 fuyao 凭据"

        fanout = pull.fanout
        assert fanout, f"{preset.id} 缺少两级拉取配方"
        assert fanout["url"].startswith("https://"), f"{preset.id} fanout 缺少第二级 URL"
        for key in ("param", "id_field", "member_id_field",
                    "symbol_col", "name_col", "value_col"):
            assert fanout.get(key), f"{preset.id} fanout 缺少 {key}"
        assert fanout.get("separator"), f"{preset.id} fanout 缺少拼接符"


def test_ensure_builtin_presets_writes_complete_configs(tmp_path: Path) -> None:
    """全新数据目录: 启动即落完整配方, 两张表都带 fanout。"""
    asyncio.run(ensure_builtin_presets(tmp_path))

    store = ExtConfigStore(tmp_path)
    for cid in _PRESET_IDS:
        config = store.get(cid)
        assert config is not None, f"{cid} 配置未创建"
        assert config.pull is not None, f"{cid} 出厂配方未写入"
        assert config.pull.url, f"{cid} 第一级 URL 为空"
        assert config.pull.fanout, f"{cid} 两级配方未写入"


# ------------------------------------------------------- 只播种一次 (不复活)
def test_seed_marker_written_on_first_install_and_survives_delete(tmp_path: Path) -> None:
    """首次安装写播种标记; 标记在 ext_data 根下, 删除单表目录不会误清。"""
    asyncio.run(ensure_builtin_presets(tmp_path))
    marker = tmp_path / "ext_data" / ".builtin_presets_seeded"
    assert marker.exists(), "首次安装未写播种标记"

    assert ExtConfigStore(tmp_path).delete("ext_gn_ths") is True
    assert marker.exists(), "删除单表误清了播种标记"


def test_deleted_builtin_preset_is_not_resurrected(tmp_path: Path) -> None:
    """已播种后删除内置表 → 重启不复活 (用户可能已接自有概念/行业源)。

    复活会让两套表并存, 内置表每天再拉 710 次成分股调用。
    """
    asyncio.run(ensure_builtin_presets(tmp_path))
    store = ExtConfigStore(tmp_path)
    assert store.delete("ext_gn_ths") is True

    asyncio.run(ensure_builtin_presets(tmp_path))

    assert store.get("ext_gn_ths") is None, "已播种后被删除的内置表又被复活"
    assert store.get("ext_hy_ths") is not None, "存活的另一张表不应受影响"


def test_partial_install_fills_missing_once_then_stops(tmp_path: Path) -> None:
    """老库升级形态: 目录里已有部分预设但无标记 → 补齐缺失 + 落标记 (仅此一次)。"""
    store = ExtConfigStore(tmp_path)
    store.upsert(_industry_preset())

    asyncio.run(ensure_builtin_presets(tmp_path))

    refreshed = ExtConfigStore(tmp_path)
    assert refreshed.get("ext_gn_ths") is not None, "缺的预设未被补齐"
    assert refreshed.delete("ext_gn_ths") is True
    asyncio.run(ensure_builtin_presets(tmp_path))
    assert refreshed.get("ext_gn_ths") is None, "播种后再次删除仍被复活"


def test_ensure_builtin_presets_fills_empty_shell(tmp_path: Path) -> None:
    """空壳配置 (无 pull, #199 之后的形态) → 补齐为完整配方。"""
    asyncio.run(ensure_builtin_presets(tmp_path))
    store = ExtConfigStore(tmp_path)
    config = store.get("ext_gn_ths")
    assert config is not None
    config.pull = None
    store.upsert(config)

    asyncio.run(ensure_builtin_presets(tmp_path))

    refreshed = ExtConfigStore(tmp_path).get("ext_gn_ths")
    assert refreshed is not None and refreshed.pull is not None
    assert refreshed.pull.fanout, "空壳配置未被补齐为完整配方"


def test_ensure_builtin_presets_keeps_custom_user_pull(tmp_path: Path) -> None:
    """用户自定义过 pull (URL 不等于历代出厂值) → 一律不动, 即使 enabled=True。"""
    asyncio.run(ensure_builtin_presets(tmp_path))
    store = ExtConfigStore(tmp_path)
    config = store.get("ext_gn_ths")
    assert config is not None

    config.pull = PullConfig(
        url="https://example.com/my-own-concepts", enabled=True
    )
    store.upsert(config)

    asyncio.run(ensure_builtin_presets(tmp_path))

    refreshed = ExtConfigStore(tmp_path).get("ext_gn_ths")
    assert refreshed is not None and refreshed.pull is not None
    assert refreshed.pull.url == "https://example.com/my-own-concepts"
    assert refreshed.pull.enabled is True, "自定义拉取配置被静默改写, 违反「绝不覆盖」原则"


def test_ensure_builtin_presets_replaces_retired_factory_pulls(tmp_path: Path) -> None:
    """pull.url 命中历代废弃出厂值 → 替换为带 fanout 的当前配方。"""
    asyncio.run(ensure_builtin_presets(tmp_path))
    store = ExtConfigStore(tmp_path)

    for cid in _PRESET_IDS:
        config = store.get(cid)
        assert config is not None
        for retired in _RETIRED_FACTORY_PULLS[cid]:
            config.pull = PullConfig(url=retired["url"], enabled=True)
            store.upsert(config)

            asyncio.run(ensure_builtin_presets(tmp_path))

            refreshed = ExtConfigStore(tmp_path).get(cid)
            assert refreshed is not None and refreshed.pull is not None
            assert refreshed.pull.fanout, (
                f"{cid} 旧出厂配方 {retired['url']} 未替换为带 fanout 的当前配方"
            )


def test_ensure_builtin_presets_syncs_retired_factory_description(tmp_path: Path) -> None:
    """description 命中出厂前缀 → 同步为当前文案; 用户自定义描述不动。"""
    asyncio.run(ensure_builtin_presets(tmp_path))
    store = ExtConfigStore(tmp_path)

    config = store.get("ext_gn_ths")
    assert config is not None
    old_factory_desc = "同花顺概念分类 (首次启动自动拉取, 可在扩展数据页手动更新)"
    config.description = old_factory_desc
    store.upsert(config)

    asyncio.run(ensure_builtin_presets(tmp_path))

    refreshed = ExtConfigStore(tmp_path).get("ext_gn_ths")
    assert refreshed is not None
    assert refreshed.description == _concept_preset().description, "旧出厂文案未同步"

    # 用户自定义描述 (非出厂前缀) → 不动
    refreshed.description = "我自己的概念表, 别动"
    store.upsert(refreshed)
    asyncio.run(ensure_builtin_presets(tmp_path))
    final = ExtConfigStore(tmp_path).get("ext_gn_ths")
    assert final is not None
    assert final.description == "我自己的概念表, 别动"
