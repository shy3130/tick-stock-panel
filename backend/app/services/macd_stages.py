"""MACD 阶段研究能力契约。

本模块只声明固定参数和当前能力状态，不读取行情、不生成派生序列。
逐日状态机与 OOS 执行器未实现前，所有请求均显式不可用。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

MACD_PARAMS = {"fast": 10, "slow": 20, "signal": 7}
SCHEMA = "tickflow.research.macd-stages.v1"
STATE_MACHINE_IMPLEMENTED = False
OOS_IMPLEMENTED = False
PIT_READER_AVAILABLE = False


@dataclass(frozen=True, slots=True)
class MacdStagesAvailability:
    schema: str
    status: str
    params: dict[str, int]
    reasons: tuple[str, ...]
    missing_capabilities: dict[str, bool]
    contract_preview: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        return payload


def macd_stages_availability() -> MacdStagesAvailability:
    """返回稳定的不可用声明；不依赖输入，不执行任何 I/O。"""
    return MacdStagesAvailability(
        schema=SCHEMA,
        status="unavailable",
        params=dict(MACD_PARAMS),
        reasons=("state_machine_not_implemented", "oos_not_implemented"),
        missing_capabilities={
            "daily_state_machine": not STATE_MACHINE_IMPLEMENTED,
            "oos_evaluation": not OOS_IMPLEMENTED,
            "pit_reader": not PIT_READER_AVAILABLE,
        },
        contract_preview={
            "required_fields": ["raw", "pit", "generation", "available_from"],
            "state_values": [
                "initial",
                "below_shrink",
                "below_expand",
                "cross_up",
                "above_expand",
                "above_shrink",
                "cross_down",
            ],
        },
    )
