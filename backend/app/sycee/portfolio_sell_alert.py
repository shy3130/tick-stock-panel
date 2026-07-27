"""Sycee-owned configuration for portfolio-scoped sell alerts."""

from __future__ import annotations

import json
import os
import threading
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import settings
from app.services.user_storage import path_for
from app.sycee.portfolio import get_portfolio

router = APIRouter(
    prefix="/api/sycee/portfolio/sell-alert",
    tags=["sycee-portfolio"],
)

WebhookChannel = Literal["feishu", "wecom"]
_lock = threading.RLock()


class PortfolioSellAlertUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    enabled: bool
    strategy_id: str = Field(default="", max_length=120, pattern=r"^[A-Za-z0-9_.-]*$")
    webhook_channels: list[WebhookChannel] = Field(default_factory=list)

    @field_validator("webhook_channels")
    @classmethod
    def unique_channels(cls, value: list[WebhookChannel]) -> list[WebhookChannel]:
        channels = list(dict.fromkeys(value))
        if len(channels) > 2:
            raise ValueError("最多允许两个推送渠道")
        return channels

    @model_validator(mode="after")
    def enabled_requires_strategy(self):
        if self.enabled and not self.strategy_id:
            raise ValueError("启用持仓卖出提醒前必须选择策略")
        return self


def _path():
    path = path_for(settings.data_dir, "sycee/portfolio_sell_alert.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _default_config() -> dict:
    return {
        "enabled": False,
        "strategy_id": "",
        "webhook_channels": [],
        "rule_id": "",
    }


def _read_config_unlocked() -> dict:
    path = _path()
    if not path.exists():
        return _default_config()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("持仓卖出提醒配置无法读取,请检查数据文件") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise RuntimeError("持仓卖出提醒配置版本无效")
    config = payload.get("config")
    if not isinstance(config, dict):
        raise RuntimeError("持仓卖出提醒配置内容无效")
    return {
        "enabled": config.get("enabled") is True,
        "strategy_id": str(config.get("strategy_id") or ""),
        "webhook_channels": [
            channel
            for channel in config.get("webhook_channels", [])
            if channel in {"feishu", "wecom"}
        ],
        "rule_id": str(config.get("rule_id") or ""),
    }


def _write_config_unlocked(config: dict) -> None:
    path = _path()
    temp = path.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps({"version": 1, "config": config}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp, path)


def get_config() -> dict:
    with _lock:
        return _read_config_unlocked()


def update_config(update: PortfolioSellAlertUpdate) -> dict:
    with _lock:
        existing = _read_config_unlocked()
        rule_id = existing.get("rule_id") or f"sycee_pf_sell_{uuid4().hex[:20]}"
        config = {
            **update.model_dump(),
            "rule_id": rule_id,
        }
        _write_config_unlocked(config)
    return config


def _status(config: dict) -> dict:
    portfolio = get_portfolio()
    symbols = sorted(position["symbol"] for position in portfolio["positions"])
    desired_rule = None
    state = "disabled"
    if config["enabled"]:
        state = "waiting_for_positions"
        if symbols:
            state = "ready"
            desired_rule = {
                "id": config["rule_id"],
                "name": "持仓卖出提醒",
                "enabled": True,
                "type": "strategy",
                "asset_type": "stock",
                "scope": "symbols",
                "symbols": symbols,
                "sector": None,
                "strategy_id": config["strategy_id"],
                "direction": "exit",
                "notify_events": ["sell_signal"],
                "conditions": [],
                "logic": "and",
                "cooldown_seconds": 3600,
                "severity": "warn",
                "webhook_channels": config["webhook_channels"],
                "message": "",
            }
    return {
        "config": config,
        "state": state,
        "position_count": len(symbols),
        "symbols": symbols,
        "desired_rule": desired_rule,
    }


@router.get("")
def read_portfolio_sell_alert():
    try:
        return _status(get_config())
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put("")
def save_portfolio_sell_alert(update: PortfolioSellAlertUpdate):
    try:
        return _status(update_config(update))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
