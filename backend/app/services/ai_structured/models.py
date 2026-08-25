"""结构化 AI 运行时公共契约 — schema v1。

定义 PA_Agent 计划 P0/P1 要求的统一数据契约:

- ``AIUsage`` / ``AIErrorCategory`` / ``AIValidationIssue`` / ``AIErrorDetails``
  / ``StructuredAIResult`` / ``RetryPolicy`` / ``CancellationToken``
  / ``AnalysisArtifact`` / ``AnalysisTraceNode``;
- 统一 attempt/request ID 工厂 (进程内唯一, 无副作用);
- UTC 时间工厂。

本模块 **不** 依赖 ``ai_provider`` 或任何 provider SDK, 可独立 import / 序列化。
``runtime`` 默认调用 ``ai_provider`` 的元数据响应, 那是单向运行时依赖, 不在契约层。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

# ── 错误分类 ───────────────────────────────────────────────
# 模型输出校验类 (可有限重试): syntax / missing / invalid / plaintext
# 运行时类 (默认不内容重试): quota / cancelled / provider
AIErrorCategory = Literal[
    "syntax",
    "missing",
    "invalid",
    "plaintext",
    "quota",
    "cancelled",
    "provider",
]

# 校验 issue 只描述模型输出问题, 不含运行时类别。
AIValidationCategory = Literal["syntax", "missing", "invalid", "plaintext"]


# ── 时间 & ID 工厂 (统一入口, 无副作用) ─────────────────────
def utcnow() -> datetime:
    """带时区的 UTC now; pydantic 序列化为 ISO8601。"""
    return datetime.now(timezone.utc)


def new_request_id() -> str:
    """结构化请求 ID: ``req_<uuid4_hex>``。"""
    return f"req_{uuid4().hex}"


def new_attempt_id() -> str:
    """结构化 attempt ID: ``att_<uuid4_hex>``。"""
    return f"att_{uuid4().hex}"


def new_node_id() -> str:
    """trace 节点 ID: ``node_<uuid4_hex>``。"""
    return f"node_{uuid4().hex}"


# ── usage ─────────────────────────────────────────────────
class AIUsage(BaseModel):
    """单次或累计 token 用量。

    ``cached_prompt_tokens`` 记录 OpenAI prompt cache 命中量; Codex/未知 provider
    无法采集时为 0 并由 runtime 发出 warning。
    """

    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: AIUsage) -> AIUsage:
        """跨 attempt / fallback 累计 token, 返回新实例。"""
        return AIUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            cached_prompt_tokens=self.cached_prompt_tokens + other.cached_prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


# ── 校验 issue & 错误详情 ──────────────────────────────────
class AIValidationIssue(BaseModel):
    """单条模型输出校验问题。

    ``path`` 为点路径 (如 ``trend.direction``) 或 ``None``; ``detail`` 保留机器可读
    补充 (pydantic error type 等), 不含敏感原文。
    """

    model_config = ConfigDict(extra="ignore")

    category: AIValidationCategory
    path: str | None = None
    message: str
    detail: dict[str, Any] | None = None


class AIErrorDetails(BaseModel):
    """失败结果的结构化错误描述。

    ``category`` 覆盖全部 7 类 (含运行时类); ``issues`` 为导致失败的具体校验问题。
    """

    model_config = ConfigDict(extra="ignore")

    category: AIErrorCategory
    message: str
    issues: list[AIValidationIssue] = Field(default_factory=list)
    detail: dict[str, Any] | None = None


# ── 重试策略 ───────────────────────────────────────────────
class RetryPolicy(BaseModel):
    """格式错误最多 2 次, 语义错误最多 1 次; quota/auth/cancel/provider 不内容重试。

    ``backoff_seconds`` 仅作重试间退避; 默认 0 以便测试无延迟。
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    max_format_retries: int = 2
    max_semantic_retries: int = 1
    backoff_seconds: float = 0.0


DEFAULT_RETRY_POLICY = RetryPolicy()


# ── 取消令牌 (asyncio.Event 语义) ──────────────────────────
class CancellationToken:
    """协作式取消令牌, 复用 ``asyncio.Event`` 语义。

    runtime 在 stage / retry / provider 边界调用 ``raise_if_cancelled``; 取消后
    结果 ``status="cancelled"``, 不伪装成失败。
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise asyncio.CancelledError("structured-ai cancelled via CancellationToken")


# ── 不变量协议 ─────────────────────────────────────────────
class Invariant(Protocol):
    """业务不变量: 接收已通过 schema 校验的数据 dict, 返回 issue 或 issue 列表或 None。"""

    def __call__(self, data: dict[str, Any]) -> AIValidationIssue | list[AIValidationIssue] | None: ...


# ── 每次尝试记录 (可追溯) ──────────────────────────────────
class AttemptRecord(BaseModel):
    """单次 provider 尝试的可审计记录: 原始输出、用量、校验问题、错误分类。"""

    model_config = ConfigDict(extra="ignore")

    index: int
    raw_text: str
    usage: AIUsage = Field(default_factory=AIUsage)
    issues: list[AIValidationIssue] = Field(default_factory=list)
    error_category: AIErrorCategory | None = None
    elapsed_ms: int = 0


# ── provider 元数据响应 ────────────────────────────────────
class GenerateResponse(BaseModel):
    """可注入 ``generate`` 的结构化返回。

    旧式 ``generate`` 直接返回 ``str`` 时, runtime 自动包装为默认 usage (0)。
    P3: 增量 primary_profile_id / actual profile_id / fallback_used / fallback_reason。
    usage 在 fallback 场景下由调用方或 with_meta 累计。
    """

    model_config = ConfigDict(extra="ignore")
    text: str
    usage: AIUsage = Field(default_factory=AIUsage)
    provider: str = ""
    profile_id: str | None = None
    model: str = ""
    primary_profile_id: str | None = None
    fallback_used: bool = False
    fallback_reason: str | None = None


# ── 结果 envelope ──────────────────────────────────────────
class StructuredAIResult(BaseModel):
    """``run_structured_ai`` 的统一结果。

    ``data`` 为 output_model 的 dict 投影 (可干净序列化); ``parsed_model`` 持有原始
    pydantic 实例供调用方直接使用, 但 ``exclude=True`` 不进入序列化输出, 避免任意
    子类序列化歧义。
    P3: 增量 primary_profile_id / fallback_used / fallback_reason；profile_id 为实际使用。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="ignore")
    request_id: str
    attempt_id: str
    status: Literal["ok", "failed", "cancelled"]
    purpose: str
    provider: str = ""
    profile_id: str | None = None
    model: str = ""
    primary_profile_id: str | None = None
    fallback_used: bool = False
    fallback_reason: str | None = None
    data: Any = None
    parsed_model: BaseModel | None = Field(default=None, exclude=True)
    raw_text: str = ""
    attempts: list[AttemptRecord] = Field(default_factory=list)
    usage: AIUsage = Field(default_factory=AIUsage)
    error: AIErrorDetails | None = None
    warnings: list[str] = Field(default_factory=list)
    elapsed_ms: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class AnalysisTraceNode(BaseModel):
    """分析 trace 节点 (M12 决策节点归一化契约)。

    程序事实与门禁节点 ``locked=true``, 模型只能解释，不能改写状态；
    locked 节点状态不可变。
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    kind: Literal["fact", "program_rule", "model_assessment", "user_input"]
    label: str
    status: Literal["pass", "fail", "unknown", "skipped"]
    source_refs: list[str] = Field(default_factory=list)
    reason: str | None = None
    locked: bool = False
    # P4/M12: 节点依赖链 (DAG)；每个 final 节点必须回溯到至少一个 locked 程序节点。
    # model_assessment 节点的 depends_on 必须含至少一个 locked 节点，禁止凭空判定。
    depends_on: list[str] = Field(default_factory=list)


class AnalysisArtifact(BaseModel):
    """研究 / 审计 artifact (非订单或策略事实源)。

    是分析记录 envelope, 携带结果、trace、usage、warnings 与来源引用, 便于审计与
    跨轮 continuity (parent_attempt_id)。不含敏感完整 prompt。
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    attempt_id: str
    request_id: str
    purpose: str
    status: Literal["ok", "failed", "cancelled"]
    schema_version: str = "v1"
    prompt_version: str = ""
    program_rules_version: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    data_as_of: datetime | None = None
    symbol: str | None = None
    market: str | None = None
    adjustment: Literal["qfq", "hfq", "none"] | None = None
    source_refs: list[str] = Field(default_factory=list)
    provider: str = ""
    profile_id: str | None = None
    model: str = ""
    result: dict[str, Any] | None = None
    error: AIErrorDetails | None = None
    trace: list[AnalysisTraceNode] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    usage: AIUsage = Field(default_factory=AIUsage)
    parent_attempt_id: str | None = None
