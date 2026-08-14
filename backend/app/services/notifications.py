"""多通道通知格式化与路由 — 纯函数, 不发送, 不含密钥。

移植自 daily_stock_analysis 通知系统的格式化 / 路由层, 但只保留 payload 构建:
  - 把通知事件格式化为各渠道 payload (feishu / email / ntfy / slack / pushover / gotify)
  - 按启用的渠道路由, 返回结构化结果
  - 默认 dry-run, 不执行网络发送
  - 脱敏敏感字段, 失败 channel 不阻断其它 payload

设计原则:
  纯函数 / 无副作用 / 无 IO / 无密钥 / JSON 可序列化 / 确定性输出

每渠道 payload 结构对齐 DSA sender 的 wire format:
  feishu   → interactive card (msg_type + card.header + card.elements)
  email    → subject + body_text + body_html
  ntfy     → topic + title + message + markdown
  slack    → text + mrkdwn + blocks (Block Kit)
  pushover → title + message + priority (纯文本, 剥离 Markdown)
  gotify   → title + message + extras (text/markdown)
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

# ── 渠道 / 路由定义 ─────────────────────────────────────────

#: 支持的通知渠道 (DSA notification_routing.py ROUTABLE_NOTIFICATION_CHANNELS 子集)
SUPPORTED_CHANNELS: tuple[str, ...] = (
    "feishu",
    "email",
    "ntfy",
    "slack",
    "pushover",
    "gotify",
)
_SUPPORTED_SET = frozenset(SUPPORTED_CHANNELS)

#: 路由类型 (report / alert / system_error)
ROUTE_TYPES: tuple[str, ...] = ("report", "alert", "system_error")
_ROUTE_SET = frozenset(ROUTE_TYPES)

# ── 长度上限 (对齐各平台 API 限制) ──────────────────────────

_TRUNCATE_SUFFIX = "…"

# 飞书 interactive 卡片正文 (webhook_adapter 保守 28000)
FEISHU_MESSAGE_MAX = 28000
FEISHU_TITLE_MAX = 100

# Email (RFC 5322 subject 建议 ≤ 78; 实际放宽)
EMAIL_SUBJECT_MAX = 200
EMAIL_BODY_MAX = 30000

# ntfy (ntfy publish API, 无硬上限但保守)
NTFY_TITLE_MAX = 256
NTFY_MESSAGE_MAX = 4096

# Slack (Block Kit section text 上限 3000; text fallback ~40000)
SLACK_BLOCK_TEXT_MAX = 3000
SLACK_TEXT_MAX = 39000

# Pushover (title ≤ 250, message ≤ 1024)
PUSHOVER_TITLE_MAX = 250
PUSHOVER_MESSAGE_MAX = 1024

# Gotify (保守)
GOTIFY_TITLE_MAX = 200
GOTIFY_MESSAGE_MAX = 4096

# ── 敏感字段脱敏 ──────────────────────────────────────────

_REDACTED = "[REDACTED]"

#: 敏感 key 片段集合 (DSA sanitize.py + tickflow log_redaction.py 合并去重)
#: 敏感 key 单词片段 (拆分后匹配; 对齐 DSA sanitize._SENSITIVE_KEY_PARTS)
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "authorization",
        "cookie",
        "password",
        "secret",
        "sendkey",
        "token",
        "webhook",
        "sign",
    }
)

#: 敏感 key compact (去分隔符) 模式 — 匹配复合 key 如 api_key→apikey, user_key→userkey
#: 对齐 DSA sanitize._SENSITIVE_COMPACT_KEY_PATTERN + _SENSITIVE_KEY_PHRASES
_SENSITIVE_COMPACT_RE = re.compile(
    r"(?:"
    r"authorization|cookie|password|secret|sendkey|token(?!s)|webhook|sign|"
    r"apikey|apitoken|accesstoken|refreshtoken|authtoken|sessiontoken|"
    r"licensekey|privatekey|secretkey|userkey|bottoken|appsecret|"
    r"appid|chatid"
    r")"
)

#: 文本中的密钥赋值模式 (token=xxx, api_key: xxx, secret=xxx …)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b("
    r"token|secret|password|sendkey|api[_-]?key|apikey|api[_-]?token|"
    r"auth[_-]?token|access[_-]?token|refresh[_-]?token|session[_-]?token|"
    r"license[_-]?key|private[_-]?key|secret[_-]?key|webhook[_-]?url|"
    r"authorization|cookie|sign|app[_-]?secret|user[_-]?key|bot[_-]?token"
    r")\s*[:=]\s*([^\s,;\"'}\]]+)"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]+)")
#: token-like pattern (sk-xxx, xox[baprs]-xxx, ghp_xxx)
_TOKEN_LIKE_RE = re.compile(
    r"(?i)\b(?:sk-[a-z0-9_\-]{16,}|xox[baprs]-[a-z0-9\-]{16,}|"
    r"gh[pousr]_[a-z0-9_]{20,})\b"
)


def _normalize_key(key: Any) -> str:
    """归一化 key: camelCase 拆分 → 小写 → 非字母数字转下划线。"""
    text = str(key).strip()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _is_sensitive_key(key: Any) -> bool:
    """判断映射 key 是否敏感。

    策略 (对齐 DSA sanitize):
      1. 拆分 key 为单词片段, 命中 _SENSITIVE_KEY_PARTS 任一片段即敏感
      2. 对去分隔符的 compact 形式做正则匹配 (处理 api_key→apikey 等复合 key)
    """
    normalized = _normalize_key(key)
    if not normalized:
        return False
    parts = set(normalized.split("_"))
    if parts & _SENSITIVE_KEY_PARTS:
        return True
    compact = normalized.replace("_", "")
    return bool(_SENSITIVE_COMPACT_RE.search(compact))


def redact_sensitive(obj: Any) -> Any:
    """递归脱敏 mapping / sequence 中的敏感值 (纯函数, 不修改输入)。

    - dict: 敏感 key 的值替换为 ``[REDACTED]``; 其余递归
    - list / tuple: 递归每个元素
    - 标量: 原样返回
    """
    if isinstance(obj, Mapping):
        result: dict[Any, Any] = {}
        for k, v in obj.items():
            if _is_sensitive_key(k):
                result[k] = _REDACTED
            else:
                result[k] = redact_sensitive(v)
        return result
    if isinstance(obj, list):
        return [redact_sensitive(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(redact_sensitive(item) for item in obj)
    return obj


def redact_text(text: Any) -> str:
    """脱敏文本中的密钥赋值模式与 Bearer / token-like 串。"""
    s = str(text) if text is not None else ""
    s = _BEARER_RE.sub(r"\1***", s)
    s = _SECRET_ASSIGNMENT_RE.sub(r"\1=***", s)
    s = _TOKEN_LIKE_RE.sub("***", s)
    return s


# ── 截断工具 ──────────────────────────────────────────────


def _truncate(text: Any, limit: int, *, suffix: str = _TRUNCATE_SUFFIX) -> str:
    """截断文本到指定字符数; 超限时预留 suffix 空间再追加省略号。"""
    s = str(text or "").strip()
    if len(s) <= limit:
        return s
    cut = max(0, limit - len(suffix))
    return s[:cut] + suffix


# ── 轻量 Markdown 工具 (无外部依赖) ────────────────────────

_FENCED_CODE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


def markdown_to_plain(md: str) -> str:
    """剥离常见 Markdown 语法到纯文本 (供 Pushover 等纯文本渠道)。"""
    text = md or ""
    # code blocks → 保留内容
    text = _FENCED_CODE_RE.sub(r"\1", text)
    # inline code
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # bold / italic
    text = re.sub(r"\*{1,3}([^*\n]+)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}([^_\n]+)_{1,3}", r"\1", text)
    # links [text](url) → text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # headers
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # horizontal rules
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    # blockquote markers
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.MULTILINE)
    return text.strip()


def markdown_to_simple_html(md: str) -> str:
    """轻量 Markdown → HTML 转换 (供 Email body_html, 无 markdown2 依赖)。"""
    text = html.escape(md or "")
    # bold
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", text)
    # italic (在 bold 之后)
    text = re.sub(r"\*([^*\n]+)\*", r"<em>\1</em>", text)
    # inline code
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # headers
    text = re.sub(r"^#{6}\s+(.+)$", r"<h6>\1</h6>", text, flags=re.MULTILINE)
    text = re.sub(r"^#{5}\s+(.+)$", r"<h5>\1</h5>", text, flags=re.MULTILINE)
    text = re.sub(r"^#{4}\s+(.+)$", r"<h4>\1</h4>", text, flags=re.MULTILINE)
    text = re.sub(r"^#{3}\s+(.+)$", r"<h3>\1</h3>", text, flags=re.MULTILINE)
    text = re.sub(r"^#{2}\s+(.+)$", r"<h2>\1</h2>", text, flags=re.MULTILINE)
    text = re.sub(r"^#{1}\s+(.+)$", r"<h1>\1</h1>", text, flags=re.MULTILINE)
    # line breaks
    text = text.replace("\n", "<br>\n")
    return f"<div>\n{text}\n</div>"


# ── 事件 / 结果模型 ────────────────────────────────────────


@dataclass(frozen=True)
class NotificationEvent:
    """通知事件 — 纯数据, 可 JSON 序列化。

    Attributes:
        title: 通知标题 (各渠道会用不同上限截断)。
        message: 通知正文 (Markdown; pushover 会剥离为纯文本)。
        route_type: 路由类型 report / alert / system_error; 未知值归一为 report。
        priority: 优先级 (-2 ~ 2, Pushover 语义; 超范围 clamp)。
        extra: 额外元数据 (可能含敏感字段, 路由时自动脱敏)。
    """

    title: str
    message: str
    route_type: str = "report"
    priority: int = 0
    extra: Mapping[str, Any] = field(default_factory=dict)

    def normalized_route(self) -> str:
        """返回归一化路由类型; 未知值降级为 report。"""
        rt = (self.route_type or "").strip().lower()
        return rt if rt in _ROUTE_SET else "report"

    def clamped_priority(self) -> int:
        """返回 clamp 到 [-2, 2] 的优先级。"""
        return max(-2, min(2, int(self.priority)))

    def clean_title(self) -> str:
        """脱敏后的标题。"""
        return redact_text(self.title)

    def clean_message(self) -> str:
        """脱敏后的正文。"""
        return redact_text(self.message)


@dataclass(frozen=True)
class ChannelPayload:
    """单个渠道的格式化结果。

    Attributes:
        channel: 渠道名 (feishu / email / ntfy / slack / pushover / gotify)。
        payload: 渠道原生 payload dict (JSON 可序列化, 截断 + 脱敏)。
        meta: 已脱敏的 extra 元数据 (供调试, 不进入 wire payload)。
        ok: 格式化是否成功。
        error: 失败原因 (ok=True 时为 None)。
    """

    channel: str
    payload: dict[str, Any]
    meta: dict[str, Any]
    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class DispatchResult:
    """路由结果 — 聚合所有渠道的格式化产物。

    Attributes:
        payloads: 成功格式化的渠道 payload 列表 (ok=True)。
        errors: 格式化失败的渠道信息列表 (ok=False)。
        skipped_unknown: 未识别的渠道名 (去重保序)。
        route_type: 归一化路由类型。
        dry_run: 是否为 dry-run 模式 (始终 True, 不执行网络发送)。
    """

    payloads: list[ChannelPayload]
    errors: list[ChannelPayload]
    skipped_unknown: list[str]
    route_type: str
    dry_run: bool = True

    @property
    def channels(self) -> list[str]:
        """已成功格式化的渠道名列表。"""
        return [p.channel for p in self.payloads]


# ── 每渠道格式化函数 (纯函数) ───────────────────────────────


def format_feishu(event: NotificationEvent) -> dict[str, Any]:
    """格式化飞书 interactive 卡片 payload。

    结构对齐 DSA FeishuSender._build_card_body + _send_feishu_message:
      msg_type=interactive, card.config/header/elements, lark_md 正文。
    """
    title = _truncate(event.clean_title(), FEISHU_TITLE_MAX)
    message = _truncate(event.clean_message(), FEISHU_MESSAGE_MAX)
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title or "通知"},
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": message},
                },
            ],
        },
    }


def format_email(event: NotificationEvent) -> dict[str, Any]:
    """格式化 Email payload。

    DSA EmailSender 使用 markdown_to_html_document (markdown2);
    本函数用轻量 markdown_to_simple_html 替代, 同时提供纯文本 body_text。
    """
    subject = _truncate(event.clean_title(), EMAIL_SUBJECT_MAX)
    body_md = _truncate(event.clean_message(), EMAIL_BODY_MAX)
    return {
        "subject": subject,
        "body_text": body_md,
        "body_html": markdown_to_simple_html(body_md),
    }


def format_ntfy(event: NotificationEvent) -> dict[str, Any]:
    """格式化 ntfy JSON publish payload。

    结构对齐 DSA NtfySender.send_to_ntfy:
      topic + title + message + markdown=True。
    topic 在发送时由 ntfy_url 解析, dry-run 留空。
    """
    return {
        "topic": "",
        "title": _truncate(event.clean_title(), NTFY_TITLE_MAX),
        "message": _truncate(event.clean_message(), NTFY_MESSAGE_MAX),
        "markdown": True,
    }


def format_slack(event: NotificationEvent) -> dict[str, Any]:
    """格式化 Slack Block Kit payload。

    结构对齐 DSA SlackSender._build_blocks + _send_slack_webhook:
      text (fallback) + mrkdwn=True + blocks (section, mrkdwn)。
    正文超 SLACK_BLOCK_TEXT_MAX 时拆分为多个 section block。
    """
    text = _truncate(event.clean_message(), SLACK_TEXT_MAX)
    title = event.clean_title()

    blocks: list[dict[str, Any]] = []
    pos = 0
    while pos < len(text):
        segment = text[pos : pos + SLACK_BLOCK_TEXT_MAX]
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": segment},
            }
        )
        pos += SLACK_BLOCK_TEXT_MAX

    if not blocks:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            }
        )

    payload: dict[str, Any] = {
        "text": text or title,
        "mrkdwn": True,
        "blocks": blocks,
    }
    return payload


def format_pushover(event: NotificationEvent) -> dict[str, Any]:
    """格式化 Pushover payload。

    结构对齐 DSA PushoverSender._send_pushover_message:
      title + message + priority (纯文本, 剥离 Markdown)。
    不含 token / user_key — dry-run 不携带密钥。
    """
    plain = markdown_to_plain(event.clean_message())
    return {
        "title": _truncate(event.clean_title(), PUSHOVER_TITLE_MAX),
        "message": _truncate(plain, PUSHOVER_MESSAGE_MAX),
        "priority": event.clamped_priority(),
    }


def format_gotify(event: NotificationEvent) -> dict[str, Any]:
    """格式化 Gotify message payload。

    结构对齐 DSA GotifySender.send_to_gotify:
      title + message + extras.client::display.contentType=text/markdown。
    认证 header (X-Gotify-Key) 在发送时注入, dry-run 不携带。
    """
    return {
        "title": _truncate(event.clean_title(), GOTIFY_TITLE_MAX),
        "message": _truncate(event.clean_message(), GOTIFY_MESSAGE_MAX),
        "extras": {
            "client::display": {"contentType": "text/markdown"},
        },
    }


#: 渠道 → 格式化函数 映射表
_CHANNEL_FORMATTERS: dict[str, Any] = {
    "feishu": format_feishu,
    "email": format_email,
    "ntfy": format_ntfy,
    "slack": format_slack,
    "pushover": format_pushover,
    "gotify": format_gotify,
}

#: 每渠道字段长度上限 (供测试验证截断)
CHANNEL_LIMITS: dict[str, dict[str, int]] = {
    "feishu": {"title": FEISHU_TITLE_MAX, "message": FEISHU_MESSAGE_MAX},
    "email": {"subject": EMAIL_SUBJECT_MAX, "message": EMAIL_BODY_MAX},
    "ntfy": {"title": NTFY_TITLE_MAX, "message": NTFY_MESSAGE_MAX},
    "slack": {"text": SLACK_TEXT_MAX, "block_text": SLACK_BLOCK_TEXT_MAX},
    "pushover": {"title": PUSHOVER_TITLE_MAX, "message": PUSHOVER_MESSAGE_MAX},
    "gotify": {"title": GOTIFY_TITLE_MAX, "message": GOTIFY_MESSAGE_MAX},
}


# ── 渠道解析 / 路由 ────────────────────────────────────────


def parse_channels(raw: Any) -> list[str]:
    """解析逗号分隔的渠道字符串或可迭代列表, 返回规范化小写去空列表。

    保留输入顺序; 不去重 (去重在 ``split_valid_channels`` 完成)。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        items: Sequence[Any] = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        items = [raw]

    channels: list[str] = []
    for item in items:
        token = str(item).strip().lower()
        if token:
            channels.append(token)
    return channels


def split_valid_channels(
    channels: Sequence[str],
) -> tuple[list[str], list[str]]:
    """把渠道拆分为有效 / 无效两组, 各自去重保序。

    Returns:
        (valid_channels, invalid_channels)
    """
    valid: list[str] = []
    invalid: list[str] = []
    seen_valid: set[str] = set()
    seen_invalid: set[str] = set()

    for ch in parse_channels(channels):
        if ch in _SUPPORTED_SET:
            if ch not in seen_valid:
                valid.append(ch)
                seen_valid.add(ch)
        elif ch not in seen_invalid:
            invalid.append(ch)
            seen_invalid.add(ch)
    return valid, invalid


def route_event(
    event: NotificationEvent,
    enabled_channels: Any = None,
    *,
    dry_run: bool = True,
) -> DispatchResult:
    """按启用渠道路由通知事件, 返回各渠道格式化 payload。

    - 默认 dry_run=True: 只构建 payload, 不执行网络发送
    - 失败 channel 不阻断其它 payload (fail-soft)
    - event.extra 自动脱敏后放入各 ChannelPayload.meta
    - 未知渠道记录到 skipped_unknown, 不阻断有效渠道
    - 同一输入始终产生相同输出 (确定性)

    Args:
        event: 通知事件。
        enabled_channels: 启用渠道 (逗号字符串 / 列表 / None)。
        dry_run: 是否 dry-run (始终 True, 参数保留供未来 wire 层复用)。

    Returns:
        DispatchResult 聚合结果。
    """
    valid, invalid = split_valid_channels(enabled_channels)
    redacted_extra = redact_sensitive(dict(event.extra))
    route_type = event.normalized_route()

    payloads: list[ChannelPayload] = []
    errors: list[ChannelPayload] = []

    for ch in valid:
        formatter = _CHANNEL_FORMATTERS.get(ch)
        if formatter is None:  # pragma: no cover — _SUPPORTED_SET 保证存在
            errors.append(
                ChannelPayload(
                    channel=ch,
                    payload={},
                    meta=redacted_extra,
                    ok=False,
                    error=f"no formatter registered for channel: {ch}",
                )
            )
            continue
        try:
            payload = formatter(event)
            payloads.append(
                ChannelPayload(
                    channel=ch,
                    payload=payload,
                    meta=redacted_extra,
                    ok=True,
                    error=None,
                )
            )
        except Exception as exc:  # noqa: BLE001 — fail-soft: 记录不阻断
            logger.warning("格式化渠道 %s payload 失败: %s", ch, exc)
            errors.append(
                ChannelPayload(
                    channel=ch,
                    payload={},
                    meta=redacted_extra,
                    ok=False,
                    error=str(exc),
                )
            )

    return DispatchResult(
        payloads=payloads,
        errors=errors,
        skipped_unknown=invalid,
        route_type=route_type,
        dry_run=dry_run,
    )


__all__ = [
    "SUPPORTED_CHANNELS",
    "ROUTE_TYPES",
    "CHANNEL_LIMITS",
    "NotificationEvent",
    "ChannelPayload",
    "DispatchResult",
    "redact_sensitive",
    "redact_text",
    "markdown_to_plain",
    "markdown_to_simple_html",
    "format_feishu",
    "format_email",
    "format_ntfy",
    "format_slack",
    "format_pushover",
    "format_gotify",
    "parse_channels",
    "split_valid_channels",
    "route_event",
]
