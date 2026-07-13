"""AI 概念轮动分析 — 从概念涨幅排名矩阵提炼主线/新晋/退潮信号。

数据来源:
  - rps_rotation.build_rps_rotation: 概念涨幅排名矩阵 (N 日 × ~387 概念)
  - market_overview_builder.build_market_overview: 大盘背景 (指数/情绪/涨停)

架构 (复刻 market_recap):
  预计算轮动信号 → 拼装 prompt → stream_ai_text 流式调用 → NDJSON 协议输出
  协议事件: meta(摘要) / delta(文本片段) / error / done
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


# ================================================================
# System Prompt — 客观轮动分析 + 固定章节模板
# ================================================================

_SYSTEM_PROMPT = """你是一位专注 A 股题材轮动的研究分析师,拥有 12 年一线研究经验,擅长从概念板块的**涨幅排名矩阵**中客观识别主力资金脉络,区分机构主导的持续性主线与游资驱动的脉冲式轮动,产出一份**客观、中立、不包含任何买卖或操作建议**的轮动分析报告。

## 核心红线(务必遵守)

- **绝对不输出**"跟踪/规避/追高/低吸/观望/操作建议"等任何交易指令或倾向性措辞
- 你的角色是**客观陈述**各概念的轮动特征、资金属性(机构 vs 游资)、持续性特征
- 换成"一个中立财经记者能不能写出来"——能写就保留,不能写就删除

## 输出规范

用 **Markdown** 格式输出,严格遵循以下结构。不要输出任何 JSON 或代码块,直接输出 Markdown 正文。

### 1. 🎯 主线研判(2-3 句)
点名当前最核心的 1-2 条主线题材(连续多日霸榜的强势概念),用一句话概括其逻辑(政策/产业/业绩/事件驱动),并客观判断是**主升期/加速期/扩散期/见顶期**。结尾用【主线强度:强 / 中 / 弱】客观定性。

### 2. 🆕 新晋强势
列出排名快速跃升的概念(从榜单中后段冲进前列的),逐个给出:
- 概念名 + 近 N 日排名变化(如 `45→20→8`)
- 涨幅加速度(连日递增 = 趋势加强)
- 可能的驱动逻辑(从板块属性推断,不要编造具体消息)
- 客观判断是**主力切入**还是**消息脉冲**

### 3. 📉 退潮预警
列出从高位明显滑落的概念(连续排名下滑或涨幅骤降),逐个给出:
- 概念名 + 排名下滑轨迹
- 退潮性质(高位分歧/资金撤离/补跌)
- 是否扩散风险

### 4. 🏛️ 机构主线 vs 🎰 游资轮动
基于排名稳定性客观区分两类资金行为:
- **机构主线**:排名标准差小、长期稳居前列的概念 → 持续性特征描述
- **游资轮动**:排名剧烈波动、脉冲式冲高的概念 → 短线波动特征描述
客观给出当前市场**整体轮动节奏**(快轮动/慢轮动/主线聚焦)的判断。

### 5. 🌐 结合大盘
结合提供的大盘数据(指数涨跌/情绪/涨停数),客观判断:
- 当前大盘环境对题材轮动是助力还是阻力
- 情绪温度与轮动节奏的匹配度(如情绪冰点但题材活跃 = 抱团;情绪火热但轮动快 = 末段)

### 6. 📌 后续观察清单
- **持续性强(客观特征)**:排名标准差小、多日稳居前列的概念(列出名称+排名数据)
- **波动性大(客观特征)**:排名剧烈跳动的概念(列出名称+排名数据)
- 客观描述各概念的轮动特征,供读者自行观察
- **不输出**"跟踪/规避/追高/低吸/观望"等操作指令;可客观描述结构变化信号(如"主线概念连续 2 日跌出前 10,主线强度可能减弱")

### 7. ⚠️ 风险提示
列出需要客观关注的风险(如主线断层、情绪与轮动背离、成交萎缩)。末尾附一行:
"> ⚠️ 本内容由 AI 基于公开行情数据生成,仅客观陈述轮动特征,不构成任何投资建议或买卖指令。交易有风险,入市需谨慎。"

## 分析准则(务必遵守)

0. **只输出结论,不输出思考过程**:禁止复述你的分析步骤。不要写"我先看...""基于上述数据我认为"——直接给结论。
1. **数据说话**:每个判断引用具体排名/涨幅数值,严禁空泛套话("强势"必须改成"连续 4 日稳居前 5,均涨 +4.2%")。
2. **客观中立**:数据不支持的结论就直言"信号不足,暂无法判断",不要硬凑。
3. **区分资金性质**:这是本分析的核心价值——机构 vs 游资的判断必须基于排名稳定性(标准差),不要凭感觉。
4. **不重复数字**:正文负责解读信号含义,不要照抄罗列已提供的全部原始数据。
5. **不输出操作指令**:不写"跟踪/规避/追高/低吸/观望"等任何交易指令;客观陈述轮动特征即可。
6. **客观推断**:若无明确消息,从量价异动客观推断可能逻辑并给结论,不要标注"[推断]"或编造具体新闻。

现在请基于下方概念轮动数据进行分析。"""


# ================================================================
# 预计算: 把排名矩阵转成结构化轮动信号
# ================================================================

# 每类信号最多取多少个概念喂给 AI (控制 token)
_TOP_N = 8


def _compute_rotation_signals(dates: list[str], columns: dict) -> dict:
    """从概念涨幅排名矩阵计算轮动信号。

    Args:
        dates: 日期列表 (最新在最前, 与 columns key 一致)
        columns: {日期: [[概念, 涨幅], ...]} 每列各自降序

    Returns:
        {
          "persistent_leaders": [...],  # 连续多日稳居前列 (主线)
          "rising": [...],              # 排名快速跃升 (新晋)
          "fading": [...],              # 从高位滑落 (退潮)
          "institutional": [...],       # 排名稳定 (机构特征)
          "hot_money": [...],           # 排名波动大 (游资特征)
        }
        每项含: concept, ranks (按 dates 顺序), pcts, avg_rank, rank_std
        ranks 时间方向: ranks[0] = 最早日, ranks[-1] = 最新日 (已反转, 左老右新)
    """
    if not dates or not columns:
        return {}

    # 按时间正序 (左老右新) 处理
    dates_asc = list(reversed(dates))

    # 收集每个概念在各日期的 (排名, 涨幅)。排名 = 该日在列中的索引 + 1。
    concept_data: dict[str, list[tuple[int, float]]] = {}
    for d in dates_asc:
        col = columns.get(d) or []
        for idx, (name, pct) in enumerate(col):
            concept_data.setdefault(name, []).append((idx + 1, pct))

    n_dates = len(dates_asc)

    def _stats(ranks_pcts: list[tuple[int, float]]) -> dict:
        ranks = [r for r, _ in ranks_pcts]
        pcts = [p for _, p in ranks_pcts]
        avg = sum(ranks) / len(ranks) if ranks else 0
        var = sum((r - avg) ** 2 for r in ranks) / len(ranks) if ranks else 0
        return {
            "ranks": ranks,
            "pcts": [round(p, 4) for p in pcts],
            "avg_rank": round(avg, 1),
            "rank_std": round(math.sqrt(var), 1),
        }

    persistent: list[dict] = []
    rising: list[dict] = []
    fading: list[dict] = []
    institutional: list[dict] = []
    hot_money: list[dict] = []

    for concept, rp in concept_data.items():
        # 缺失日补 (大排名, 0 涨幅) 保持时间轴对齐
        if len(rp) < n_dates:
            rp = rp + [(999, 0.0)] * (n_dates - len(rp))
        s = _stats(rp)
        s["concept"] = concept

        ranks = s["ranks"]
        latest_rank = ranks[-1]
        earliest_rank = ranks[0]
        # 最近 3 日 (不足则全部) 均排名, 判断近期强度
        recent = ranks[-min(3, len(ranks)):]
        recent_avg = sum(recent) / len(recent)

        # 主线: 近期稳居前 10
        if recent_avg <= 10 and latest_rank <= 10:
            persistent.append(s)

        # 新晋: 早期排名靠后(>30), 最新冲进前 20, 跃升幅度大
        jump = earliest_rank - latest_rank
        if earliest_rank > 30 and latest_rank <= 20 and jump >= 20:
            rising.append(s)

        # 退潮: 早期排名靠前(<=10), 最新滑落到 30 外
        drop = latest_rank - earliest_rank
        if earliest_rank <= 10 and latest_rank > 30 and drop >= 20:
            fading.append(s)

        # 机构: 排名标准差小且平均排名靠前 (稳定强势)
        if s["rank_std"] <= 5 and s["avg_rank"] <= 20:
            institutional.append(s)

        # 游资: 排名标准差大 (波动剧烈)
        if s["rank_std"] >= 20:
            hot_money.append(s)

    # 排序: 主线按近期排名升序; 新晋按跃升幅度降序; 退潮按跌幅降序
    persistent.sort(key=lambda x: x["avg_rank"])
    rising.sort(key=lambda x: x["ranks"][0] - x["ranks"][-1], reverse=True)
    fading.sort(key=lambda x: x["ranks"][-1] - x["ranks"][0], reverse=True)
    institutional.sort(key=lambda x: (x["rank_std"], x["avg_rank"]))
    hot_money.sort(key=lambda x: x["rank_std"], reverse=True)

    return {
        "persistent_leaders": persistent[:_TOP_N],
        "rising": rising[:_TOP_N],
        "fading": fading[:_TOP_N],
        "institutional": institutional[:_TOP_N],
        "hot_money": hot_money[:_TOP_N],
    }


# ================================================================
# Prompt 构建
# ================================================================

def _fmt_pct(v) -> str:
    if v is None:
        return "—"
    return f"{v*100:+.2f}%"


def _build_market_block(overview: dict) -> str:
    """大盘背景精简块 (复用 market_overview 已算好的字段)。"""
    indices = overview.get("indices") or []
    emo = overview.get("emotion") or {}
    lim = overview.get("limit") or {}
    amt = overview.get("amount") or {}

    idx_lines = []
    for idx in indices[:4]:
        name = idx.get("name") or idx.get("symbol") or "?"
        chg = idx.get("change_pct")
        idx_lines.append(f"{name} {_fmt_pct(chg)}")
    idx_str = " / ".join(idx_lines) or "指数缺失"

    total_amount = (amt.get("total") or 0) / 1e8  # 元 → 亿

    return (
        f"- 指数: {idx_str}\n"
        f"- 情绪: {emo.get('score', 50)} ({emo.get('label', '—')})\n"
        f"- 涨停/炸板/跌停: {lim.get('limit_up', 0)} / {lim.get('broken', 0)} / {lim.get('limit_down', 0)}"
        f"  (最高连板 {lim.get('max_boards', 0)})\n"
        f"- 两市成交额: {total_amount:.0f} 亿元"
    )


def _build_signal_block(title: str, items: list[dict]) -> str:
    """轮动信号块: 把预计算的概念信号转成紧凑文本。"""
    if not items:
        return f"### {title}\n(本类无明显信号)"
    lines = [f"### {title}"]
    for it in items:
        ranks_str = "→".join(str(r) if r < 999 else "—" for r in it["ranks"])
        avg_pct = sum(it["pcts"]) / len(it["pcts"]) if it["pcts"] else 0
        lines.append(
            f"- {it['concept']}: 排名 {ranks_str} | 均排名 {it['avg_rank']} "
            f"| 排名波动σ {it['rank_std']} | 区间均涨 {_fmt_pct(avg_pct)}"
        )
    return "\n".join(lines)


def _build_user_prompt(signals: dict, overview: dict, days: int, dates: list[str], focus: str) -> str:
    """组装 user 消息: 大盘背景 + 轮动信号 + focus。"""
    dates_asc = list(reversed(dates))
    date_range = f"{dates_asc[0]} ~ {dates_asc[-1]}" if dates_asc else "—"

    parts = [
        f"# 概念涨幅轮动数据 (最近 {days} 个交易日: {date_range})",
        "",
        "## 大盘背景",
        _build_market_block(overview),
        "",
        "## 轮动信号 (排名时间方向: 左→右 = 旧→新, 排名越小越强)",
        "",
        _build_signal_block("🎯 主线 (连续霸榜)", signals.get("persistent_leaders", [])),
        "",
        _build_signal_block("🆕 新晋强势 (排名跃升)", signals.get("rising", [])),
        "",
        _build_signal_block("📉 退潮预警 (高位滑落)", signals.get("fading", [])),
        "",
        _build_signal_block("🏛️ 机构特征 (排名稳定)", signals.get("institutional", [])),
        "",
        _build_signal_block("🎰 游资特征 (排名波动大)", signals.get("hot_money", [])),
    ]

    from app.services.ai_provider import sanitize_focus
    safe_focus = sanitize_focus(focus)
    if safe_focus:
        parts.extend(["", f"本次分析请特别关注: {safe_focus}"])

    return "\n".join(parts)


def _build_summary(signals: dict) -> str:
    """meta 事件的摘要 (前端可立即展示)。"""
    leaders = signals.get("persistent_leaders", [])
    rising = signals.get("rising", [])
    fading = signals.get("fading", [])
    leader_names = "、".join(it["concept"] for it in leaders[:3]) or "暂无明确主线"
    return f"主线: {leader_names} | 新晋 {len(rising)} | 退潮 {len(fading)}"


# ================================================================
# 流式主入口
# ================================================================

async def analyze_rotation_stream(
    repo,
    days: int = 12,
    focus: str = "",
    quote_service=None,
    depth_service=None,
) -> AsyncIterator[str]:
    """流式概念轮动分析: yield 出每个 NDJSON 事件。

    Args:
        repo: KlineRepository (必填)。
        days: 分析最近 N 个交易日 (7-30)。
        focus: 用户追加的关注点。
        quote_service / depth_service: 可选, 大盘背景装配依赖。
    """
    from app.services.rps_rotation import build_rps_rotation
    from app.services.market_overview_builder import build_market_overview

    # 1. 取轮动矩阵
    rotation = build_rps_rotation(repo, days)
    dates = rotation.get("dates") or []
    columns = rotation.get("columns") or {}

    if not dates or not columns:
        yield json.dumps({
            "type": "error",
            "message": "暂无概念轮动数据,请先在「概念分析」页获取概念数据源",
        }, ensure_ascii=False)
        return

    # 2. 预计算轮动信号
    signals = _compute_rotation_signals(dates, columns)

    # 3. 大盘背景 (失败不阻断, 降级为空)
    try:
        overview = build_market_overview(repo, quote_service, depth_service)
    except Exception as e:  # noqa: BLE001
        logger.warning("rotation analyze: 大盘背景获取失败, 降级为空: %s", e)
        overview = {}

    # 4. meta 事件
    yield json.dumps({
        "type": "meta",
        "days": days,
        "summary": _build_summary(signals),
    }, ensure_ascii=False)

    # 5. 构建 prompt + 流式调用 LLM
    try:
        from app.services.ai_provider import stream_ai_text, ai_configured

        if not ai_configured():
            yield json.dumps({
                "type": "error",
                "message": "AI 未配置,请在「设置」页填写 API Key 与接口地址",
            }, ensure_ascii=False)
            return

        user_prompt = _build_user_prompt(signals, overview, days, dates, focus)
        async for delta in stream_ai_text(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.5,
            max_tokens=4000,
        ):
            yield json.dumps({"type": "delta", "content": delta}, ensure_ascii=False)

    except Exception as e:  # noqa: BLE001
        logger.exception("AI concept rotation analyze failed: %s", e)
        yield json.dumps({"type": "error", "message": f"AI 轮动分析失败: {e}"}, ensure_ascii=False)

    yield json.dumps({"type": "done"}, ensure_ascii=False)
