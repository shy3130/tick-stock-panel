"""ext_http 共享客户端契约:
  - 无 CA 环境变量: trust_env=False, 不传 verify (维持 httpx 默认)
  - SSL_CERT_FILE / SSL_CERT_DIR: 显式构造 SSLContext 传入 verify
  - timeout 默认 30s, 可显式覆盖
"""
import ssl

import certifi

from app.services import ext_http


def test_no_ca_env_keeps_default_verify(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)

    assert ext_http.build_ext_ssl_context() is None


def test_ssl_cert_file_builds_context(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", certifi.where())
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)

    ctx = ext_http.build_ext_ssl_context()

    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_ssl_cert_dir_builds_context(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("SSL_CERT_DIR", certifi.where().rsplit("/", 1)[0])

    ctx = ext_http.build_ext_ssl_context()

    assert isinstance(ctx, ssl.SSLContext)


def test_client_defaults_and_verify_injection(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", certifi.where())

    created = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            created.update(kwargs)

    monkeypatch.setattr(ext_http.httpx, "AsyncClient", FakeAsyncClient)

    ext_http.ext_async_client()
    assert created["timeout"] == 30
    assert created["trust_env"] is False
    assert isinstance(created["verify"], ssl.SSLContext)

    created.clear()
    ext_http.ext_async_client(timeout=5, verify="/custom/verify")
    assert created == {"timeout": 5, "trust_env": False, "verify": "/custom/verify"}
