"""Webhook 推送适配器 — 把告警事件推送到外部 IM / 量化软件。

职责: 把后端产生的告警事件, 通过用户配置的 Webhook 地址推送到外部。
     目前支持飞书群机器人; QMT / ptrade 等量化通道为待定。

飞书自定义机器人接入:
  1. 飞书群 → 群设置 → 群机器人 → 添加「自定义机器人」
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
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# 单次推送最长字符 (飞书单条文本消息上限 30KB, 这里保守截断避免刷屏)
_MAX_LEN = 500

# 卡片消息正文最长字符 (飞书 interactive 卡片上限 30KB, 保守留余量给标题/结构)
_CARD_MAX_LEN = 28000

# 飞书自定义机器人 Webhook 前缀 (用于 URL 合法性校验)
FEISHU_HOOK_PREFIX = "https://open.feishu.cn/open-apis/bot/v2/hook/"
DINGTALK_HOOK_PREFIX = "https://oapi.dingtalk.com/robot/send"
WECOM_HOOK_PREFIX = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"


def _truncate(text: str) -> str:
    """截断超长文本。"""
    text = (text or "").strip()
    return text[:_MAX_LEN] + ("…" if len(text) > _MAX_LEN else "")


def is_valid_feishu_url(url: str) -> bool:
    """校验是否为合法的飞书自定义机器人 Webhook 地址。"""
    return bool(url) and url.startswith(FEISHU_HOOK_PREFIX)


def is_valid_dingtalk_url(url: str) -> bool:
    parts = urlsplit(url or "")
    return (
        parts.scheme == "https"
        and parts.netloc == "oapi.dingtalk.com"
        and parts.path == "/robot/send"
        and "access_token=" in parts.query
    )


def is_valid_wecom_url(url: str) -> bool:
    parts = urlsplit(url or "")
    return (
        parts.scheme == "https"
        and parts.netloc == "qyapi.weixin.qq.com"
        and parts.path == "/cgi-bin/webhook/send"
        and "key=" in parts.query
    )


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


def _post_feishu(webhook_url: str, payload: dict, secret: str) -> bool:
    """发送一次飞书 webhook 请求并判定成败 (供 text / card 共用)。

    成功响应: HTTP 200 且业务 code=0 (或非 JSON 的 200)。失败静默返回 False。
    """
    try:
        import httpx

        # 启用签名校验时, 请求体须带 timestamp + sign (秒级时间戳)
        if secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = _gen_sign(timestamp, secret)

        resp = httpx.post(webhook_url, json=payload, timeout=5.0, trust_env=False)
        # 飞书成功响应: {"code":0,"msg":"success"} (或 StatusCode 200 + Extra)
        if resp.status_code == 200:
            try:
                data = resp.json()
                # code=0 表示飞书业务侧成功; 部分版本无 code 字段则按 msg 判断
                if isinstance(data, dict):
                    code = data.get("code", data.get("StatusCode", 0))
                    if code == 0:
                        return True
                    logger.debug("飞书推送业务失败: %s", data)
                    return False
            except ValueError:
                # 非 JSON 响应但 HTTP 200, 视为成功
                return True
        logger.debug("飞书推送 HTTP %s: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:  # noqa: BLE001
        logger.debug("飞书 Webhook 推送失败: %s", e)
        return False


def send_feishu(webhook_url: str, title: str, body: str, secret: str = "") -> bool:
    """推送一条文本消息到飞书群机器人。

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


def _signed_dingtalk_url(webhook_url: str, secret: str) -> str:
    if not secret:
        return webhook_url
    timestamp = str(int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    sign = base64.b64encode(
        hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    parts = urlsplit(webhook_url)
    query = parts.query + ("&" if parts.query else "") + urlencode({"timestamp": timestamp, "sign": sign})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def send_dingtalk(webhook_url: str, title: str, body: str, secret: str = "") -> bool:
    if not is_valid_dingtalk_url(webhook_url):
        return False
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": f"### {title}\n\n{_truncate_card(body)}"},
    }
    try:
        import httpx

        resp = httpx.post(_signed_dingtalk_url(webhook_url, secret), json=payload, timeout=5.0, trust_env=False)
        return resp.status_code == 200 and resp.json().get("errcode", 0) == 0
    except Exception as e:  # noqa: BLE001
        logger.debug("钉钉 Webhook 推送失败: %s", e)
        return False


def send_wecom(webhook_url: str, title: str, body: str, secret: str = "") -> bool:  # noqa: ARG001
    if not is_valid_wecom_url(webhook_url):
        return False
    payload = {"msgtype": "markdown", "markdown": {"content": f"**{title}**\n\n{_truncate_card(body)}"}}
    try:
        import httpx

        resp = httpx.post(webhook_url, json=payload, timeout=5.0, trust_env=False)
        return resp.status_code == 200 and resp.json().get("errcode", 0) == 0
    except Exception as e:  # noqa: BLE001
        logger.debug("企微 Webhook 推送失败: %s", e)
        return False


def send_meow(nickname: str, title: str, body: str) -> bool:
    nickname = (nickname or "").strip()
    if not nickname:
        return False
    url = "https://api.chuckfang.com/{}/{}/{}".format(
        quote(nickname, safe=""),
        quote(title or "TickFlow", safe=""),
        quote(_truncate_card(body), safe=""),
    )
    try:
        import httpx

        resp = httpx.post(url, timeout=5.0, trust_env=False)
        return 200 <= resp.status_code < 300
    except Exception as e:  # noqa: BLE001
        logger.debug("MeoW 推送失败: %s", e)
        return False


def send_channel(channel: str, config: dict, title: str, body: str) -> bool:
    channel = str(channel or "").lower()
    if channel == "feishu":
        return send_feishu(config.get("url", ""), title, body, config.get("secret", ""))
    if channel == "dingtalk":
        return send_dingtalk(config.get("url", ""), title, body, config.get("secret", ""))
    if channel == "wecom":
        return send_wecom(config.get("url", ""), title, body)
    if channel == "meow":
        return send_meow(config.get("nickname", ""), title, body)
    return False


def send_configured_channels(title: str, body: str) -> int:
    """推送到所有已配置 webhook 通道，返回成功数量。"""
    from app.services import preferences

    sent = 0
    for channel, config in preferences.get_configured_webhook_channels().items():
        if send_channel(channel, config, title, body):
            sent += 1
    return sent


def send_feishu_card(webhook_url: str, title: str, subtitle: str, body_md: str, secret: str = "") -> bool:
    """推送一条 interactive 卡片消息到飞书群机器人 —— 用 lark_md 渲染完整 markdown 报告。

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
