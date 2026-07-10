"""Webhook 推送适配器 — 把告警事件推送到外部 IM / 量化软件。

职责: 把后端产生的告警事件, 通过用户配置的 Webhook 地址推送到外部。
     目前支持飞书群推送 Webhook; QMT / ptrade 等量化通道为待定。

飞书自定义机器人接入:
  1. 飞书群 → 群设置 → 群推送 Webhook → 添加「自定义机器人」
  2. 复制生成的 Webhook 地址 (形如 https://open.feishu.cn/open-apis/bot/v2/hook/xxx)
  3. (可选) 安全设置 → 启用「签名校验」, 记录签名密钥(secret)
  4. 填入设置页「飞书 Webhook」配置

设计: 失败静默降级, 绝不因推送失败阻断告警主流程 (落盘 / SSE 推送)。
     去重不在本层做, 复用 MonitorRuleEngine 的 cooldown。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

logger = logging.getLogger(__name__)

# 单次推送最长字符 (飞书单条文本消息上限 30KB, 这里保守截断避免刷屏)
_MAX_LEN = 500

# 卡片消息正文最长字符 (飞书 interactive 卡片上限 30KB, 保守留余量给标题/结构)
_CARD_MAX_LEN = 28000

# 企业微信群推送 Webhook markdown 消息上限 4096 字节 (非字符; 中文每字 3 字节),
# 留余量给标题、格式符及截断提示行。
_WECOM_MD_MAX_BYTES = 4000

# 截断提示行: 正文超长被截断时追加, 引导用户回应用内查看完整内容。
_WECOM_TRUNCATED_HINT = "\n\n…内容较长已截断，更多详情请回到 TickFlow 应用内查看。"

# 飞书自定义机器人 Webhook 前缀 (用于 URL 合法性校验)
FEISHU_HOOK_PREFIX = "https://open.feishu.cn/open-apis/bot/v2/hook/"


def _truncate(text: str) -> str:
    """截断超长文本。"""
    text = (text or "").strip()
    return text[:_MAX_LEN] + ("…" if len(text) > _MAX_LEN else "")


def is_valid_feishu_url(url: str) -> bool:
    """校验是否为合法的飞书自定义机器人 Webhook 地址。"""
    return bool(url) and url.startswith(FEISHU_HOOK_PREFIX)


def _gen_sign(timestamp: str, secret: str) -> str:
    """计算飞书自定义机器人签名。

    算法 (官方): 把 `timestamp + "\\n" + secret` 作为签名字符串 (key),
    用 HmacSHA256 计算空字符串的签名结果, 再 Base64 编码。
    """
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def _truncate_card(text: str) -> str:
    """截断卡片正文 (留余量给标题与卡片结构)。"""
    text = (text or "").strip()
    return text[:_CARD_MAX_LEN] + ("…" if len(text) > _CARD_MAX_LEN else "")


def _truncate_to_bytes(text: str, max_bytes: int, suffix: str = "…") -> str:
    """按 UTF-8 字节数安全截断 (不截断在多字节字符中间, 末尾补 suffix)。

    企业微信 markdown 按**字节**计长 (上限 4096), 中文每字 3 字节,
    不能用字符数截断 (len(str) 算的是字符数, 会超字节上限)。

    Args:
        max_bytes: 含 suffix 在内的总字节上限。
        suffix:    被截断时追加的结尾 (如省略号或提示行)。
    """
    text = (text or "").strip()
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    suffix_bytes = suffix.encode("utf-8")
    # 为 suffix 留出字节, 再用 errors='ignore' 丢弃被截断的尾字节, 避免半个字符
    cut = encoded[:max_bytes - len(suffix_bytes)]
    return cut.decode("utf-8", errors="ignore") + suffix


_FEISHU_MAX_ATTEMPTS = 3


def _post_feishu(webhook_url: str, payload: dict, secret: str) -> bool:
    """发送飞书 webhook 请求并判定成败 (供 text / card 共用)。

    成功响应: HTTP 200 且业务 code=0 (或非 JSON/非 dict 的 200)。

    瞬时失败 (网络/超时/HTTP 5xx) 会**带退避重试** —— 告警冷却在事件生成时即打戳,
    一次瞬时 5xx/timeout 若不重试, 该告警会被冷却窗口(默认 1h)压掉, 离屏用户彻底
    收不到推送。永久失败 (4xx / 业务 code≠0, 如签名错、URL 失效) 不重试。最终失败
    记 WARNING (而非之前的 debug), 保证「推送丢了」在日志里可见。
    """
    import httpx

    last_err = ""
    for attempt in range(1, _FEISHU_MAX_ATTEMPTS + 1):
        try:
            # 启用签名校验时, 请求体须带 timestamp + sign (每次重试都重算, 防时间戳过期)
            if secret:
                timestamp = str(int(time.time()))
                payload["timestamp"] = timestamp
                payload["sign"] = _gen_sign(timestamp, secret)

            resp = httpx.post(webhook_url, json=payload, timeout=5.0)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    return True  # 非 JSON 的 200, 视为成功
                if isinstance(data, dict):
                    code = data.get("code", data.get("StatusCode", 0))
                    if code == 0:
                        return True
                    # 业务失败(签名错/格式错等): 重试无益, 直接失败
                    logger.warning("飞书推送业务失败(不重试): %s", data)
                    return False
                return True  # 200 且 JSON 非 dict, 视为成功
            # 4xx 客户端错误(URL 失效等): 不重试; 5xx: 落入重试
            last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if resp.status_code < 500:
                logger.warning("飞书推送失败(不重试, 客户端错误): %s", last_err)
                return False
        except Exception as e:  # noqa: BLE001 — 网络/超时, 可重试
            last_err = str(e)

        if attempt < _FEISHU_MAX_ATTEMPTS:
            time.sleep(min(2 ** (attempt - 1), 3))  # 退避: 1s, 2s

    logger.warning("飞书 Webhook 推送最终失败(已重试 %d 次): %s", _FEISHU_MAX_ATTEMPTS, last_err)
    return False


def send_feishu(webhook_url: str, title: str, body: str, secret: str = "") -> bool:
    """推送一条文本消息到飞书群推送 Webhook。

    Args:
        webhook_url: 飞书自定义机器人 Webhook 地址
        title:       消息标题 (与正文拼接为一条文本)
        body:        消息正文
        secret:      签名密钥 (机器人启用了「签名校验」时必填; 留空则不带签名)

    Returns:
        True=成功送达, False=失败或 URL 非法。
        失败静默, 不抛异常 (Webhook 是辅助通道, 不能阻断告警主流程)。
    """
    if not is_valid_feishu_url(webhook_url):
        return False

    text = _truncate(f"{title}\n{body}".strip())
    if not text:
        return False

    payload: dict = {"msg_type": "text", "content": {"text": text}}
    return _post_feishu(webhook_url, payload, secret)


def send_feishu_card(webhook_url: str, title: str, subtitle: str, body_md: str, secret: str = "") -> bool:
    """推送一条 interactive 卡片消息到飞书群推送 Webhook —— 用 lark_md 渲染完整 markdown 报告。

    飞书「自定义机器人」webhook 不支持文件附件, 但 interactive 卡片的 lark_md 元素
    可渲染 markdown, 能承载完整复盘报告(通常 2-5KB, 远小于卡片 30KB 上限)。

    Args:
        webhook_url: 飞书自定义机器人 Webhook 地址
        title:       卡片标题 (显示在蓝色 header)
        subtitle:    副标题 (加粗显示, 如日期/情绪标签; 留空则省略)
        body_md:     卡片正文 markdown (报告全文)
        secret:      签名密钥 (启用签名校验时必填)

    Returns:
        True=成功送达, False=失败或 URL 非法。
        失败静默, 不抛异常 (与 send_feishu 一致, 不阻断告警主流程)。
    """
    if not is_valid_feishu_url(webhook_url):
        return False

    body = _truncate_card(body_md)
    elements: list[dict] = []
    if subtitle.strip():
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**{subtitle.strip()}**"},
        })
        elements.append({"tag": "hr"})
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": body},
    })

    payload: dict = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": elements,
        },
    }
    return _post_feishu(webhook_url, payload, secret)


# ================================================================
# 企业微信群推送 Webhook
# ================================================================
#
# 与飞书自定义机器人几乎同构: 同样是"群推送 Webhook + POST JSON"。
# 关键差异:
#   1. Webhook 形态: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
#   2. 无需签名校验 (key 本身即凭证; 企业微信群推送 Webhook 可选"签名校验"但极少用)
#   3. Markdown 原生支持 (msgtype=markdown), 不必像飞书那样包进 interactive 卡片
#   4. 成功响应: {"errcode":0,"errmsg":"ok"}
#
# 限制: 每个机器人每分钟最多 20 条消息 (超出会被限流 460min 内不可用),
#      依赖 MonitorRuleEngine 的 cooldown 去重即可应对告警场景。
# 企业微信群的消息可在绑定的个人微信接收, 实现"微信推送"体验。

WECOM_HOOK_PREFIX = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"


def is_valid_wecom_url(url: str) -> bool:
    """校验是否为合法的企业微信群推送 Webhook 地址。

    允许两种写法:
      - 完整: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
      - 仅 key: xxx (企业微信群推送 Webhook 的 key 为 36 位 UUID 样式, 保存时自动补全)
    """
    if not url:
        return False
    if url.startswith(WECOM_HOOK_PREFIX):
        return True
    # 纯 key: 企业微信 key 形如 12345678-1234-1234-1234-1234567890ab (36 位),
    # 但用户可能截断, 放宽到 >= 20 位的无空格无斜杠字符串。
    url = url.strip()
    if " " in url or "/" in url or "?" in url:
        return False
    return len(url) >= 20


def normalize_wecom_url(url: str) -> str:
    """把纯 key 补全为完整 Webhook URL。已是完整 URL 则原样返回。"""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith(WECOM_HOOK_PREFIX):
        return url
    return f"{WECOM_HOOK_PREFIX}?key={url}"


def _post_wecom(webhook_url: str, payload: dict) -> bool:
    """发送一次企业微信 webhook 请求并判定成败。

    成功响应: HTTP 200 且 errcode=0。失败静默返回 False。
    """
    try:
        import httpx

        resp = httpx.post(webhook_url, json=payload, timeout=5.0)
        if resp.status_code == 200:
            try:
                data = resp.json()
                if isinstance(data, dict):
                    # errcode=0 表示成功; 45009=频率限制, 其它非零=业务失败
                    if data.get("errcode") == 0:
                        return True
                    logger.warning("企业微信推送业务失败: %s", data)
                    return False
            except ValueError:
                return True
        logger.warning("企业微信推送 HTTP %s: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("企业微信 Webhook 推送失败: %s", e)
        return False


def send_wecom(webhook_url: str, title: str, body: str) -> bool:
    """推送一条文本消息到企业微信群推送 Webhook。

    Args:
        webhook_url: 企业微信群推送 Webhook 地址 (或纯 key, 会自动补全)
        title:       消息标题 (与正文拼接为一条文本)
        body:        消息正文

    Returns:
        True=成功送达, False=失败或 URL 非法。
        失败静默, 不抛异常 (与 send_feishu 一致)。
    """
    webhook_url = normalize_wecom_url(webhook_url)
    if not is_valid_wecom_url(webhook_url):
        return False

    text = _truncate(f"{title}\n{body}".strip())
    if not text:
        return False

    payload: dict = {"msgtype": "text", "text": {"content": text}}
    return _post_wecom(webhook_url, payload)


def send_wecom_markdown(webhook_url: str, title: str, body_md: str) -> bool:
    """推送一条 Markdown 消息到企业微信群推送 Webhook —— 承载完整复盘报告。

    企业微信群推送 Webhook 原生支持 markdown 类型 (比飞书 interactive 卡片简单),
    支持 # ## **粗体** >引用 - 列表 等基础语法, 单条上限 4096 字节。

    Args:
        webhook_url: 企业微信群推送 Webhook 地址 (或纯 key)
        title:       标题 (作为一级标题 ## 拼到正文前)
        body_md:     markdown 正文

    Returns:
        True=成功送达, False=失败或 URL 非法。
    """
    webhook_url = normalize_wecom_url(webhook_url)
    if not is_valid_wecom_url(webhook_url):
        return False

    # 企业微信 markdown 上限 4096 字节 (含标题), 用按字节截断避免超限被拒。
    # 超长时追加提示行, 引导用户回应用内查看完整报告。
    raw = f"## {title}\n\n{body_md}".strip()
    content = _truncate_to_bytes(raw, _WECOM_MD_MAX_BYTES, suffix=_WECOM_TRUNCATED_HINT)
    if not content.strip():
        return False

    payload: dict = {"msgtype": "markdown", "markdown": {"content": content}}
    return _post_wecom(webhook_url, payload)

