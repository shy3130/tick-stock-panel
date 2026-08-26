"""扩展数据 HTTP 客户端构造 — 代理隔离 + 显式 CA 环境变量。

httpx 的 trust_env=False 会同时忽略代理环境变量 (HTTP(S)_PROXY / ALL_PROXY)
和 CA 环境变量 (SSL_CERT_FILE / SSL_CERT_DIR)。扩展数据源是用户可配置的,
企业内网私有 CA 是合理场景, 因此在禁用代理继承的同时显式构造 CA context:

  - trust_env 固定 False: 不继承系统代理 (与 eastmoney_client 等既有惯例一致)
  - 设置了 SSL_CERT_FILE / SSL_CERT_DIR 时显式传 verify=SSLContext,
    在禁用 trust_env 的同时保留自定义 CA 配置
  - 未设置 CA 环境变量时不传 verify, 维持 httpx 默认 (certifi) 行为
"""
from __future__ import annotations

import os
import ssl
from typing import Any

import httpx

_DEFAULT_TIMEOUT = 30


def build_ext_ssl_context() -> ssl.SSLContext | None:
    """按 SSL_CERT_FILE / SSL_CERT_DIR 构造显式 CA context。

    未设置任何 CA 环境变量时返回 None, 由调用方维持 httpx 默认 verify。
    """
    cafile = os.environ.get("SSL_CERT_FILE")
    capath = os.environ.get("SSL_CERT_DIR")
    if not cafile and not capath:
        return None
    return ssl.create_default_context(cafile=cafile or None, capath=capath or None)


def ext_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """构造扩展数据通用的 AsyncClient。

    默认 timeout 30s、trust_env=False; 存在 CA 环境变量时附带显式 verify。
    """
    kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
    kwargs.setdefault("trust_env", False)
    if "verify" not in kwargs:
        ctx = build_ext_ssl_context()
        if ctx is not None:
            kwargs["verify"] = ctx
    return httpx.AsyncClient(**kwargs)
