"""前端资源使用固定 MIME，避免 Windows 注册表中的文件关联影响页面执行。"""
from __future__ import annotations

import os
from pathlib import Path

from starlette.datastructures import Headers
from starlette.responses import FileResponse, Response
from starlette.staticfiles import NotModifiedResponse, StaticFiles
from starlette.types import Scope

_MIME_TYPES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".wasm": "application/wasm",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


def frontend_file_response(
    path: str | os.PathLike[str], *, status_code: int = 200,
    stat_result: os.stat_result | None = None,
) -> FileResponse:
    return FileResponse(
        path, media_type=_MIME_TYPES.get(Path(path).suffix.lower()),
        status_code=status_code, stat_result=stat_result,
    )


class FrontendStaticFiles(StaticFiles):
    def file_response(
        self, full_path: str | os.PathLike[str], stat_result: os.stat_result,
        scope: Scope, status_code: int = 200,
    ) -> Response:
        response = frontend_file_response(
            full_path, status_code=status_code, stat_result=stat_result,
        )
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return NotModifiedResponse(response.headers)
        return response
