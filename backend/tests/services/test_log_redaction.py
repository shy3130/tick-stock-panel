from __future__ import annotations

import io
import logging

from app.log_redaction import (
    SecretRedactionFilter,
    install_secret_redaction_filter,
    redact_mapping,
    redact_secret,
    redact_text,
)


def test_redact_secret_never_leaks_short_or_long_values():
    assert redact_secret("abc") == "***"
    masked = redact_secret("sk-1234567890")
    assert "1234567890" not in masked
    assert masked.startswith("sk-") and masked.endswith("90")


def test_nested_mapping_and_text_redact_without_damaging_context():
    payload = {
        "symbol": "600519.SH",
        "request_id": "req_abcd",
        "authorization": "Bearer top-secret-token",
        "nested": [
            {"api_key": "sk-1234567890"},
            "https://api.example.com/v1?q=600519.SH&access_token=secret-token",
        ],
    }
    redacted = redact_mapping(payload)
    rendered = repr(redacted)
    assert "top-secret-token" not in rendered
    assert "sk-1234567890" not in rendered
    assert "secret-token" not in rendered
    assert "600519.SH" in rendered
    assert "req_abcd" in rendered
    assert "api.example.com" in rendered


def test_webhook_path_bearer_and_key_value_are_redacted():
    raw = (
        "Authorization: Bearer abc.def.ghi "
        "api_key=sk-abcdefgh "
        "https://open.feishu.cn/open-apis/bot/v2/hook/very-secret-token"
    )
    result = redact_text(raw)
    assert "abc.def.ghi" not in result
    assert "sk-abcdefgh" not in result
    assert "very-secret-token" not in result
    assert "open.feishu.cn" in result


def test_filter_redacts_args_and_exception_text():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretRedactionFilter())
    logger = logging.getLogger("test.secret-redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("payload=%s", {"api_key": "sk-1234567890", "symbol": "600519.SH"})
    try:
        raise RuntimeError("token=secret-token")
    except RuntimeError:
        logger.exception("provider failed")

    output = stream.getvalue()
    assert "sk-1234567890" not in output
    assert "secret-token" not in output
    assert "600519.SH" in output
    assert "RuntimeError" in output


def test_install_filter_is_idempotent():
    logger = logging.Logger("isolated")
    logger.addHandler(logging.StreamHandler(io.StringIO()))
    assert install_secret_redaction_filter(logger) == 2
    assert install_secret_redaction_filter(logger) == 0
