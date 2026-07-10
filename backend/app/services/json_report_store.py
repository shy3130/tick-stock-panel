"""AI 报告类 JSON 存储的共享底座。

财务分析 / 个股分析 / 大盘复盘三类报告的持久化机制此前是近乎逐字的三份拷贝
(_path / list_reports / _save_all / save_report / delete_report / _now_iso 完全一致,
只差文件名、上限、id 前缀)。这里抽出唯一实现, 三个模块各自实例化并委托, 对外仍保持
原有函数签名不变。

差异通过构造参数固化: filename(存储文件名) / max_reports(保留上限) / id_prefix(id 前缀) /
id_with_symbol(id 是否带 symbol 后缀 —— 大盘复盘无 symbol)。

存储文件: data/user_data/{filename} (数组, 按 created_at 降序), 保留最近 max_reports 条,
超出自动裁剪最旧的。写入走临时文件 + os.replace 原子替换, 避免进程中断留下半截 JSON。
读写同时可能来自请求线程与调度线程, 故加实例锁串行化写路径。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class JsonReportStore:
    """一类 AI 报告的 JSON 存储 (原子写 + 实例锁)。"""

    def __init__(
        self,
        filename: str,
        max_reports: int,
        id_prefix: str,
        id_with_symbol: bool = True,
    ) -> None:
        self.filename = filename
        self.max_reports = max_reports
        self.id_prefix = id_prefix
        self.id_with_symbol = id_with_symbol
        # 请求线程 + 调度线程可能并发写, 用实例锁串行化读-改-写
        self._lock = threading.Lock()

    def _path(self) -> Path:
        from app.config import settings
        p = settings.data_dir / "user_data" / self.filename
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def list_reports(self) -> list[dict]:
        """返回全部报告(按 created_at 降序)。"""
        p = self._path()
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return sorted(data, key=lambda r: r.get("created_at", ""), reverse=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("%s malformed: %s", self.filename, e)
        return []

    def _save_all(self, reports: list[dict]) -> None:
        """全量写入(裁剪到 max_reports, 原子替换)。"""
        # 保持降序
        reports.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        if len(reports) > self.max_reports:
            reports = reports[:self.max_reports]
        self._atomic_write(reports)

    def _atomic_write(self, reports: list[dict]) -> None:
        """先写临时文件再 os.replace 原子替换, 避免进程中断留下损坏的 JSON。"""
        p = self._path()
        text = json.dumps(reports, indent=2, ensure_ascii=False)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)

    def _make_id(self, report: dict) -> str:
        base = f"{self.id_prefix}_{int(time.time() * 1000)}"
        if self.id_with_symbol:
            return f"{base}_{report.get('symbol', 'x')}"
        return base

    def save_report(self, report: dict) -> dict:
        """新增一条报告并持久化。返回保存后的报告(含 id / created_at)。

        自动补全 id 与 created_at(若缺),并裁剪到上限。
        """
        with self._lock:
            reports = self.list_reports()
            if not report.get("id"):
                report["id"] = self._make_id(report)
            if not report.get("created_at"):
                report["created_at"] = self._now_iso()
            reports.append(report)
            self._save_all(reports)
            total = min(len(reports), self.max_reports)
        logger.info("report saved: %s → %s, total %d", self.filename, report.get("id"), total)
        return report

    def delete_report(self, report_id: str) -> bool:
        """删除指定报告。返回是否删除成功。"""
        with self._lock:
            reports = self.list_reports()
            before = len(reports)
            reports = [r for r in reports if r.get("id") != report_id]
            if len(reports) < before:
                self._save_all(reports)
                return True
        return False

    def clear_reports(self) -> int:
        """清空全部报告。返回删除数量。"""
        with self._lock:
            reports = self.list_reports()
            n = len(reports)
            if n > 0:
                self._save_all([])
        return n

    @staticmethod
    def _now_iso() -> str:
        """当前本地时间 ISO 字符串(带秒精度,前端 toLocaleString 友好)。"""
        from datetime import datetime
        return datetime.now().isoformat(timespec="seconds")
