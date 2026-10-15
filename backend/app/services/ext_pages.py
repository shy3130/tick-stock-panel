"""运行时页面插件存储 — data/ext_pages/{id}/ 目录式持久化。

页面插件是前端扩展的打包产物(单文件 ES 模块): 上传后由前端在启动引导窗口
动态 import 挂载(见 frontend/src/extensions/runtime.ts)。后端只负责存储、
元数据与分发, 不解析 JS 内容 —— 清单契约(能否 import、apiVersion、路由
冲突)由前端注册表校验, 校验结果经 manifest 接口回填到 page.json。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# 与 ExtConfigStore 同口径的 ID 白名单: 防 ../ 路径穿越写删
_VALID_ID = re.compile(r"^[a-zA-Z0-9_]{1,64}$")


@dataclass
class PagePackMeta:
    id: str
    enabled: bool = False
    # 前端试载成功后回填的清单摘要(展示与排障用, 真实注册以 bundle 为准)
    label: str = ""
    path: str = ""
    size: int = 0
    sha256: str = ""
    uploaded_at: float = 0.0
    # 前端试载/注册失败的错误信息; 空串表示未出错
    error: str = ""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class PagePackStore:
    """每个页面插件一个目录: page.json(元数据) + bundle.js(产物)。

    元数据写入走 临时文件+rename, 与 ext_data 的原子写同语义, 避免半写
    状态被启动扫描读到。
    """

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir) / "ext_pages"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _dir(self, pack_id: str) -> Path:
        if not _VALID_ID.match(pack_id or ""):
            raise ValueError("页面插件 ID 仅允许字母/数字/下划线, 长度 1-64")
        return self.data_dir / pack_id

    def _write_meta(self, meta: PagePackMeta) -> None:
        d = self._dir(meta.id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "page.json.tmp"
        tmp.write_text(json.dumps(asdict(meta), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(d / "page.json")

    def save_upload(self, pack_id: str, bundle_src: Path) -> PagePackMeta:
        """把已写好的临时 bundle 文件收编入库(同 ID 重新上传 = 覆盖更新)。"""
        d = self._dir(pack_id)
        d.mkdir(parents=True, exist_ok=True)
        dest = d / "bundle.js"
        # 临时目录与数据目录可能跨盘(Windows), 用 copy 而非 rename
        dest.write_bytes(bundle_src.read_bytes())
        meta = PagePackMeta(
            id=pack_id,
            size=dest.stat().st_size,
            sha256=_sha256_file(dest),
            uploaded_at=time.time(),
        )
        self._write_meta(meta)
        return meta

    def get(self, pack_id: str) -> PagePackMeta | None:
        p = self._dir(pack_id) / "page.json"
        if not p.exists():
            return None
        try:
            return PagePackMeta(**json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError):
            # 半写/损坏的元数据按损坏页处理, 不阻断其他页面插件
            return PagePackMeta(id=pack_id, error="page.json 损坏, 请重新上传")

    def bundle_path(self, pack_id: str) -> Path | None:
        p = self._dir(pack_id) / "bundle.js"
        return p if p.exists() else None

    def list(self) -> list[PagePackMeta]:
        metas: list[PagePackMeta] = []
        for d in sorted(self.data_dir.iterdir()):
            if not d.is_dir():
                continue
            meta = self.get(d.name)
            if meta is not None:
                metas.append(meta)
        return metas

    def update_manifest(self, pack_id: str, *, label: str, path: str, error: str) -> PagePackMeta:
        """前端试载结果回填: error 非空 → 停用并记错误; 否则启用。"""
        meta = self.get(pack_id)
        if meta is None:
            raise KeyError(pack_id)
        meta.label = label
        meta.path = path
        meta.error = error
        meta.enabled = not error
        self._write_meta(meta)
        return meta

    def set_enabled(self, pack_id: str, enabled: bool) -> PagePackMeta:
        meta = self.get(pack_id)
        if meta is None:
            raise KeyError(pack_id)
        meta.enabled = enabled
        self._write_meta(meta)
        return meta

    def delete(self, pack_id: str) -> bool:
        d = self._dir(pack_id)
        if not d.exists():
            return False
        for f in d.iterdir():
            f.unlink(missing_ok=True)
        d.rmdir()
        return True
