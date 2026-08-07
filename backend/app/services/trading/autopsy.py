"""AI 归因(复盘 autopsy)— 把单笔事件流 + 红旗喂给 AI,输出四分类归因。

四分类(照搬 YMOS P11 平仓归因):
- A 策略正常不利: 策略本身没问题,本次亏损属于正常波动
- B 执行偏离: 没按纪律执行(放宽止损/亏损加仓/情绪化操作)
- C 规则歧义冲突: 策略规则本身有歧义或互相矛盾 —— 只有 C 才允许发起策略修改
- D 数据问题: 数据缺失/错误导致决策依据失真
"""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.ai_structured import CancellationToken, build_ai_meta, run_structured_ai
from app.services.ai_budgets import resolve_budget
from app.services.ai_usage_snapshot import record_structured_usage
from app.services.ai_provider import ai_configured, generate_ai_text_with_meta, profile_configured
from app.services.trading import store
from app.services.trading.lifecycle import now_str
from app.services.trading.red_flags import scan_trade_events


async def _default_generate(messages, **kwargs):
    """P3: 默认走 metadata 路径 (真实 fallback + usage 回传)。"""
    return await generate_ai_text_with_meta(messages, **kwargs)

_SYSTEM_PROMPT = """\
你是一位严格的交易纪律复盘分析师。根据给定的交易事件流、机械红旗和计划偏差，\
对这笔交易的结果进行四分类归因分析。

四分类定义:
- A 策略正常不利: 策略逻辑本身正确,本次亏损属于策略的正常波动范围
- B 执行偏离: 交易者未严格遵守策略规则(放宽止损/亏损加仓/情绪化操作)
- C 规则歧义冲突: 策略规则本身存在歧义或互相矛盾,导致执行时无所适从
- D 数据问题: 行情/财务等数据缺失或错误导致决策依据失真

关键原则: 只有 C(规则歧义冲突)才允许发起策略修改。单笔盈亏不应改变策略内核。

## 12 种不一致模式(归因参照)
在「理由」中,必须显式引用命中模式编号(可多个);未命中任何模式写「无」:
1 裁判切换: 入场用一种裁判(信号),亏损后用另一种裁判(逻辑/估值)辩解
2 期限漂移: 时限型论点在预期事件落空后悄悄延长持有期限
3 仓位代替信念: 用加仓制造确定性或弥补亏损,而非证据改善
4 价格与论点混淆: 把价格涨跌同时当作论点的证实与证伪,未界定信号/噪音边界
5 自称与行为不符: 声称某类风格,实际进出/监控行为却是另一类
6 节奏错配: 声明长周期却高频输入、频繁制造新决策
7 隐藏共享敞口: 多笔看似分散实则依赖同一板块/因子/事件/流动性条件
8 事后改写: 结果出来后重写原始论点(需用最早带时间戳记录还原真相)
9 唯结果论: 用盈亏评判规则本身(赚钱的违规被赞、守纪律的亏损被贬)
10 数据质量伪装成判断: 看似策略失败,实为数据过期/缺失/误分类
11 门禁膨胀: 每次失误加一条检查项,直到流程不可用
12 亏损后放松规则: 亏损立即导致规则放宽(需带反证条件+复核样本+回滚)

请严格返回 JSON 对象且不要添加多余内容:
{"tradeId":"程序提供的交易ID","classification":"A|B|C|D",
"reasoning":"归因依据;引用模式编号,未命中写无",
"fix":"B/C/D 的最小可执行修复,A 写无需修复","patternIds":[1,2]}"""

class _AutopsyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    trade_id: str = Field(alias="tradeId")
    classification: Literal["A", "B", "C", "D"]
    reasoning: str = Field(min_length=1)
    fix: str = Field(min_length=1)
    pattern_ids: list[int] = Field(default_factory=list, alias="patternIds")

    @model_validator(mode="after")
    def validate_semantics(self) -> "_AutopsyOutput":
        if any(value < 1 or value > 12 for value in self.pattern_ids):
            raise ValueError("patternIds 必须在 1..12")
        if len(set(self.pattern_ids)) != len(self.pattern_ids):
            raise ValueError("patternIds 不得重复")
        if self.classification == "A" and self.fix.strip() != "无需修复":
            raise ValueError("A 类归因的 fix 必须为 无需修复")
        return self


def build_autopsy_prompt(
    trade: dict[str, Any],
    events: list[dict[str, Any]],
    red_flags: list[dict[str, Any]],
    deviation: str | None,
) -> list[dict[str, str]]:
    """构建中文 prompt messages(OpenAI 兼容格式)。"""
    parts: list[str] = []
    pos = trade.get("position") or {}
    thesis = trade.get("thesis") or {}
    parts.append(f"标的: {trade.get('symbol', '')} {trade.get('name', '')}")
    parts.append(f"策略: {trade.get('strategy') or '未指定'}")
    parts.append(f"状态: {trade.get('status', '')}")
    parts.append(
        f"持仓: {pos.get('qty', 0)} 股, 成本价 {pos.get('costPrice', 0)}, "
        f"已投入 {pos.get('invested', 0)}"
    )
    parts.append(f"已实现盈亏: {trade.get('realizedPnl', 0)}")
    parts.append(f"止损价: {trade.get('stopLoss')}")
    parts.append(f"买入论点: {thesis.get('text', '')}")
    parts.append(f"失效信号: {thesis.get('invalidation', '')}")

    if deviation:
        parts.append(f"\n计划偏差: {deviation}")

    parts.append(f"\n事件流({len(events)} 条):")
    for e in events:
        payload_str = json.dumps(e.get("payload") or {}, ensure_ascii=False)
        bypass = " [绕过门禁]" if e.get("gateBypassed") else ""
        parts.append(f"  [{e.get('ts', '')}] {e.get('kind', '')}{bypass}: {payload_str}")

    parts.append(f"\n机械红旗({len(red_flags)} 条):")
    if red_flags:
        for f in red_flags:
            detail = {k: v for k, v in f.items() if k not in ("ts", "type")}
            parts.append(
                f"  [{f.get('ts', '')}] {f.get('type', '')}: "
                f"{json.dumps(detail, ensure_ascii=False)}"
            )
    else:
        parts.append("  (无)")

    parts.append("\n请按指定格式输出归因分析。")

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]




def _autopsies_dir(data_dir: Path) -> Path:
    return store.trading_dir(data_dir) / "autopsies"


def _safe_id(trade_id: str) -> str:
    return trade_id.replace("/", "_").replace("\\", "_").replace("..", "_")


def read_autopsy(data_dir: Path, trade_id: str) -> dict[str, Any] | None:
    """读取已落盘的归因记录,无则返回 None。"""
    p = _autopsies_dir(data_dir) / f"{_safe_id(trade_id)}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


async def run_autopsy(
    data_dir: Path,
    trade_id: str,
    *,
    profile_id: str | None = None,
    cancel_token: CancellationToken | None = None,
    on_event: Any | None = None,
    generate: Any | None = None,
) -> dict[str, Any]:
    """读取事实 → 结构化 AI → 校验 → 落盘；畸形输出绝不默认成 A。

    P3: 接受 ``profile_id`` 选择实际使用的 profile; 结果 additive 追加 ``ai_meta``
    (旧字段 schemaVersion/classification/.../usage/profileId/model 全部保留)。
    """
    trade = store.read_trade(data_dir, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="单笔交易不存在")



    events = store.read_events(data_dir, trade_id)
    audit = store.read_audit(data_dir, trade_id, limit=10000)
    red_flags = scan_trade_events(events, audit)
    if not profile_configured(profile_id):
        raise HTTPException(status_code=503, detail="AI 未配置,无法执行归因分析")



    budget = resolve_budget("trading_autopsy")
    try:
        structured = await run_structured_ai(
            messages=build_autopsy_prompt(trade, events, red_flags, deviation=None),
            output_model=_AutopsyOutput,
            purpose="trading_autopsy",
            profile_id=profile_id,
            immutable_context={"tradeId": trade_id},
            cancel_token=cancel_token,
            on_event=on_event,
            generate=generate or _default_generate,
            temperature=budget.temperature,
            max_tokens=budget.max_tokens,
            timeout=budget.timeout,
        )
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI 归因调用失败: {exc}") from exc



    record_structured_usage("trading_autopsy", structured)
    if structured.status == "cancelled":
        raise asyncio.CancelledError
    if structured.status != "ok" or not isinstance(structured.data, dict):
        category = structured.error.category if structured.error is not None else "invalid"
        message = structured.error.message if structured.error is not None else "结构化输出校验失败"
        raise HTTPException(status_code=502, detail=f"AI 归因输出无效 [{category}]: {message}")



    parsed = structured.data
    ts = now_str()
    result: dict[str, Any] = {
        "schemaVersion": 1,
        "tradeId": trade_id,
        "classification": parsed["classification"],
        "reasoning": parsed["reasoning"],
        "fix": parsed["fix"],
        "patternIds": parsed.get("pattern_ids", parsed.get("patternIds", [])),
        "rawResponse": structured.raw_text,
        "redFlags": red_flags,
        "ts": ts,
        "attemptId": structured.attempt_id,
        "requestId": structured.request_id,
        "usage": structured.usage.model_dump(),
        "provider": structured.provider,
        "profileId": structured.profile_id,
        "model": structured.model,
        "ai_meta": build_ai_meta(structured),
    }
    directory = _autopsies_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{_safe_id(trade_id)}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result
