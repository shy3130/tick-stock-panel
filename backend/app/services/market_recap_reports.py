"""AI 大盘复盘报告持久化存储。

与 stock_reports.py(个股分析报告)/ ai_reports.py(财务分析报告)完全独立 ——
单独的文件、字段、上限,互不影响。存储机制委托给共享的 JsonReportStore
(原子写 + 实例锁), 本模块只固化 大盘复盘报告 的文件名 / 上限 / id 前缀
(复盘无 symbol, id 不带 symbol 后缀), 对外保持原有函数签名不变。

存储位置: data/user_data/ai_market_recaps.json (数组,按 created_at 降序)
保留最近 MAX_REPORTS 条;超出自动裁剪最旧的。

每条报告结构:
{
  "id": "mkr_xxx",            # 唯一 id(market-recap-report)
  "as_of": "2026-06-27",      # 复盘日期
  "focus": "",                # 用户追加的关心点(可为空)
  "content": "# ...markdown", # 报告正文
  "summary": "三大指数齐涨...",  # 一句话摘要
  "emotion_score": 68,        # 情绪分(0-100, 复盘生成时的市场情绪雷达均分)
  "emotion_label": "偏暖",     # 情绪标签(强势/偏暖/震荡/偏冷/冰点)
  "created_at": "2026-06-27T15:35:00"
}
"""
from __future__ import annotations

from app.services.json_report_store import JsonReportStore

MAX_REPORTS = 20

_store = JsonReportStore(
    "ai_market_recaps.json", MAX_REPORTS, id_prefix="mkr", id_with_symbol=False,
)


def list_reports() -> list[dict]:
    """返回全部报告(按 created_at 降序)。"""
    return _store.list_reports()


def save_report(report: dict) -> dict:
    """新增一条报告并持久化。返回保存后的报告(含 id / created_at)。"""
    return _store.save_report(report)


def delete_report(report_id: str) -> bool:
    """删除指定报告。返回是否删除成功。"""
    return _store.delete_report(report_id)
