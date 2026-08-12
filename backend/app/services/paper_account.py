"""本地模拟账户.

只记录用户手工填写的模拟成交, 不连接券商、不读取券商凭据, 也不发出真实委托.
持久化金额统一使用整数分, 读写由线程锁与操作系统文件锁保护, 并通过同目录临时文件
加 ``os.replace`` 原子替换.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, DecimalException, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.services import strategy_cache

SCHEMA_VERSION = 1
DEFAULT_INITIAL_CASH = 10_000
ALLOWED_INITIAL_CASH = {5_000, 10_000}
COMMISSION_RATE = Decimal("0.0003")
MINIMUM_COMMISSION_CENTS = 500
SELL_STAMP_TAX_RATE = Decimal("0.0005")

_ACCOUNT_FILENAME = "paper_account.json"
_LOCK_TIMEOUT_SECONDS = 5.0
_STOCK_SYMBOL = re.compile(
    r"^(?:(?:600|601|603|605|688|689)\d{3}\.SH|"
    r"(?:000|001|002|003|300|301)\d{3}\.SZ|"
    r"(?:4\d{5}|8\d{5}|92\d{4})\.BJ)$"
)
_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


class PaperAccountValidationError(ValueError):
    """用户输入不符合模拟账户规则。"""


class PaperAccountStorageError(RuntimeError):
    """本地模拟账户文件无法安全读取或保存。"""


def _account_path(data_dir: Path) -> Path:
    return Path(data_dir) / "user_data" / _ACCOUNT_FILENAME


def _thread_lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _thread_locks_guard:
        return _thread_locks.setdefault(key, threading.Lock())


def _try_os_lock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_os_lock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _locked_account(data_dir: Path) -> Iterator[Path]:
    """Serialize read-modify-write across threads and local processes."""
    path = _account_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    thread_lock = _thread_lock_for(lock_path)
    with thread_lock, lock_path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                _try_os_lock(handle)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise PaperAccountStorageError(
                        "模拟账户正在被另一个进程使用, 请稍后重试。"
                    ) from exc
                time.sleep(0.02)
        try:
            yield path
        finally:
            _release_os_lock(handle)


def _new_state(initial_cash: int = DEFAULT_INITIAL_CASH) -> dict[str, Any]:
    initial_cash_cents = initial_cash * 100
    return {
        "schema_version": SCHEMA_VERSION,
        "initial_cash_cents": initial_cash_cents,
        "cash_cents": initial_cash_cents,
        "realized_pnl_cents": 0,
        "lots": [],
        "journal": [],
    }


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        state = _new_state()
        _write_state(path, state)
        return state
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PaperAccountStorageError(
            "模拟账户文件损坏或无法读取, 请先备份该文件后再重置账户。"
        ) from exc
    try:
        _validate_state(value)
    except PaperAccountStorageError:
        raise
    except Exception as exc:
        # schema-v1 文件即使嵌套值极端或类型恶意, 也必须统一转成可恢复的存储错误,
        # 不能把 KeyError / DecimalException 等底层异常直接泄漏给接口调用方。
        raise _invalid_state("嵌套字段无法解析") from exc
    return value


def _invalid_state(reason: str) -> PaperAccountStorageError:
    return PaperAccountStorageError(
        f"模拟账户文件{reason}, 已停止读取以防错误扩大。"
        "下一步: 请先备份 paper_account.json, 再重置模拟账户。"
    )


def _state_int(
    value: Any,
    *,
    field: str,
    minimum: int | None = None,
) -> int:
    if type(value) is not int:
        raise _invalid_state(f"字段 {field} 不是整数")
    if minimum is not None and value < minimum:
        raise _invalid_state(f"字段 {field} 小于允许值")
    return value


def _state_date(value: Any, *, field: str) -> date:
    if not isinstance(value, str):
        raise _invalid_state(f"字段 {field} 不是日期文字")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise _invalid_state(f"字段 {field} 不是有效日期") from exc
    if parsed.isoformat() != value:
        raise _invalid_state(f"字段 {field} 不是标准日期")
    return parsed


def _state_text(
    value: Any,
    *,
    field: str,
    maximum: int,
    allow_empty: bool,
) -> str:
    if not isinstance(value, str):
        raise _invalid_state(f"字段 {field} 不是文字")
    if not allow_empty and not value.strip():
        raise _invalid_state(f"字段 {field} 为空")
    if len(value) > maximum:
        raise _invalid_state(f"字段 {field} 超过长度上限")
    return value


def _validate_state(state: Any) -> None:
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise _invalid_state("版本不受支持")
    for field in ("initial_cash_cents", "cash_cents", "realized_pnl_cents"):
        _state_int(state.get(field), field=field)
    if state["initial_cash_cents"] not in {500_000, 1_000_000}:
        raise _invalid_state("初始资金无效")
    if state["cash_cents"] < 0:
        raise _invalid_state("现金为负数")
    if not isinstance(state.get("lots"), list) or not isinstance(state.get("journal"), list):
        raise _invalid_state("持仓或日志结构无效")

    for index, lot in enumerate(state["lots"]):
        _validate_lot(lot, index=index)
    for index, entry in enumerate(state["journal"]):
        _validate_journal_entry(entry, index=index)
    _validate_state_reconciliation(state)


def _validate_lot(lot: Any, *, index: int) -> None:
    required = {
        "id",
        "symbol",
        "name",
        "trade_date",
        "quantity",
        "remaining_quantity",
        "price_cents",
        "buy_commission_cents",
        "remaining_buy_commission_cents",
    }
    if not isinstance(lot, dict) or not required <= set(lot):
        raise _invalid_state(f"持仓批次 {index + 1} 缺少必需字段")
    _state_text(lot["id"], field=f"lots[{index}].id", maximum=100, allow_empty=False)
    symbol = _state_text(
        lot["symbol"],
        field=f"lots[{index}].symbol",
        maximum=20,
        allow_empty=False,
    )
    if not _STOCK_SYMBOL.fullmatch(symbol):
        raise _invalid_state(f"持仓批次 {index + 1} 的股票代码无效")
    _state_text(
        lot["name"],
        field=f"lots[{index}].name",
        maximum=80,
        allow_empty=False,
    )
    _state_date(lot["trade_date"], field=f"lots[{index}].trade_date")
    quantity = _state_int(
        lot["quantity"],
        field=f"lots[{index}].quantity",
        minimum=1,
    )
    remaining = _state_int(
        lot["remaining_quantity"],
        field=f"lots[{index}].remaining_quantity",
        minimum=0,
    )
    if remaining > quantity:
        raise _invalid_state(f"持仓批次 {index + 1} 的剩余数量超过原数量")
    price_cents = _state_int(
        lot["price_cents"],
        field=f"lots[{index}].price_cents",
        minimum=1,
    )
    commission = _state_int(
        lot["buy_commission_cents"],
        field=f"lots[{index}].buy_commission_cents",
        minimum=0,
    )
    remaining_commission = _state_int(
        lot["remaining_buy_commission_cents"],
        field=f"lots[{index}].remaining_buy_commission_cents",
        minimum=0,
    )
    if commission != _commission_cents(price_cents * quantity):
        raise _invalid_state(f"持仓批次 {index + 1} 的买入佣金无法对账")
    if remaining_commission > commission:
        raise _invalid_state(f"持仓批次 {index + 1} 的剩余佣金无效")
    if remaining == quantity and remaining_commission != commission:
        raise _invalid_state(f"持仓批次 {index + 1} 的完整佣金无法对账")
    if remaining == 0 and remaining_commission != 0:
        raise _invalid_state(f"持仓批次 {index + 1} 的已结清佣金不为零")


def _validate_journal_entry(entry: Any, *, index: int) -> None:
    required = {
        "id",
        "timestamp",
        "side",
        "symbol",
        "name",
        "trade_date",
        "quantity",
        "price_cents",
        "gross_amount_cents",
        "cash_before_cents",
        "cash_after_cents",
        "commission_cents",
        "stamp_tax_cents",
        "total_fees_cents",
        "fifo_cost_basis_cents",
        "realized_pnl_cents",
        "plan_note",
        "invalidation_note",
    }
    if not isinstance(entry, dict) or not required <= set(entry):
        raise _invalid_state(f"日志 {index + 1} 缺少必需字段")
    _state_text(entry["id"], field=f"journal[{index}].id", maximum=100, allow_empty=False)
    timestamp = _state_text(
        entry["timestamp"],
        field=f"journal[{index}].timestamp",
        maximum=50,
        allow_empty=False,
    )
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise _invalid_state(f"日志 {index + 1} 的时间戳无效") from exc
    if parsed_timestamp.tzinfo is None:
        raise _invalid_state(f"日志 {index + 1} 的时间戳缺少时区")
    side = entry["side"]
    if side not in {"BUY", "SELL"}:
        raise _invalid_state(f"日志 {index + 1} 的方向无效")
    symbol = _state_text(
        entry["symbol"],
        field=f"journal[{index}].symbol",
        maximum=20,
        allow_empty=False,
    )
    if not _STOCK_SYMBOL.fullmatch(symbol):
        raise _invalid_state(f"日志 {index + 1} 的股票代码无效")
    _state_text(
        entry["name"],
        field=f"journal[{index}].name",
        maximum=80,
        allow_empty=False,
    )
    _state_date(entry["trade_date"], field=f"journal[{index}].trade_date")
    quantity = _state_int(
        entry["quantity"],
        field=f"journal[{index}].quantity",
        minimum=1,
    )
    price_cents = _state_int(
        entry["price_cents"],
        field=f"journal[{index}].price_cents",
        minimum=1,
    )
    gross = _state_int(
        entry["gross_amount_cents"],
        field=f"journal[{index}].gross_amount_cents",
        minimum=1,
    )
    if gross != price_cents * quantity:
        raise _invalid_state(f"日志 {index + 1} 的成交金额无法对账")
    cash_before = _state_int(
        entry["cash_before_cents"],
        field=f"journal[{index}].cash_before_cents",
        minimum=0,
    )
    cash_after = _state_int(
        entry["cash_after_cents"],
        field=f"journal[{index}].cash_after_cents",
        minimum=0,
    )
    commission = _state_int(
        entry["commission_cents"],
        field=f"journal[{index}].commission_cents",
        minimum=0,
    )
    stamp_tax = _state_int(
        entry["stamp_tax_cents"],
        field=f"journal[{index}].stamp_tax_cents",
        minimum=0,
    )
    total_fees = _state_int(
        entry["total_fees_cents"],
        field=f"journal[{index}].total_fees_cents",
        minimum=0,
    )
    fifo_basis = _state_int(
        entry["fifo_cost_basis_cents"],
        field=f"journal[{index}].fifo_cost_basis_cents",
        minimum=1,
    )
    realized = _state_int(
        entry["realized_pnl_cents"],
        field=f"journal[{index}].realized_pnl_cents",
    )
    if commission != _commission_cents(gross):
        raise _invalid_state(f"日志 {index + 1} 的佣金无法对账")
    if stamp_tax != _stamp_tax_cents(gross, side):
        raise _invalid_state(f"日志 {index + 1} 的印花税无法对账")
    if total_fees != commission + stamp_tax:
        raise _invalid_state(f"日志 {index + 1} 的总费用无法对账")
    if side == "BUY":
        if fifo_basis != gross + commission or realized != 0:
            raise _invalid_state(f"日志 {index + 1} 的买入成本无法对账")
        expected_cash_after = cash_before - gross - commission
    else:
        if realized != gross - total_fees - fifo_basis:
            raise _invalid_state(f"日志 {index + 1} 的已实现盈亏无法对账")
        expected_cash_after = cash_before + gross - total_fees
    if cash_after != expected_cash_after:
        raise _invalid_state(f"日志 {index + 1} 的现金变化无法对账")
    _state_text(
        entry["plan_note"],
        field=f"journal[{index}].plan_note",
        maximum=500,
        allow_empty=True,
    )
    _state_text(
        entry["invalidation_note"],
        field=f"journal[{index}].invalidation_note",
        maximum=500,
        allow_empty=True,
    )


def _validate_state_reconciliation(state: dict[str, Any]) -> None:
    expected_cash = state["initial_cash_cents"]
    realized = 0
    net_quantities: dict[str, int] = defaultdict(int)
    for index, entry in enumerate(state["journal"]):
        if entry["cash_before_cents"] != expected_cash:
            raise _invalid_state(f"日志 {index + 1} 与上一条现金记录不连续")
        expected_cash = entry["cash_after_cents"]
        direction = 1 if entry["side"] == "BUY" else -1
        net_quantities[entry["symbol"]] += direction * entry["quantity"]
        realized += entry["realized_pnl_cents"]
    if expected_cash != state["cash_cents"]:
        raise _invalid_state("日志末笔现金与账户现金不一致")
    if realized != state["realized_pnl_cents"]:
        raise _invalid_state("日志已实现盈亏与账户汇总不一致")

    remaining_by_symbol: dict[str, int] = defaultdict(int)
    remaining_cost = 0
    for lot in state["lots"]:
        remaining_by_symbol[lot["symbol"]] += lot["remaining_quantity"]
        remaining_cost += (
            lot["price_cents"] * lot["remaining_quantity"]
            + lot["remaining_buy_commission_cents"]
        )
    symbols = set(net_quantities) | set(remaining_by_symbol)
    if any(net_quantities[symbol] != remaining_by_symbol[symbol] for symbol in symbols):
        raise _invalid_state("持仓数量与不可变日志无法对账")
    if (
        state["cash_cents"] + remaining_cost
        != state["initial_cash_cents"] + state["realized_pnl_cents"]
    ):
        raise _invalid_state("现金、持仓成本与已实现盈亏无法对账")


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise PaperAccountStorageError("模拟账户保存失败, 本次操作未确认写入。") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _parse_initial_cash(value: Any) -> int:
    if isinstance(value, bool):
        raise PaperAccountValidationError(
            "初始资金只能选择 5000 或 10000 元。下一步: 请重新选择后再提交。"
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PaperAccountValidationError(
            "初始资金只能选择 5000 或 10000 元。下一步: 请重新选择后再提交。"
        ) from exc
    if parsed not in ALLOWED_INITIAL_CASH or parsed != value:
        raise PaperAccountValidationError(
            "初始资金只能选择 5000 或 10000 元。下一步: 请重新选择后再提交。"
        )
    return parsed


def _parse_trade_date(value: Any) -> date:
    if isinstance(value, datetime):
        raise PaperAccountValidationError(
            "模拟成交日期格式无效。下一步: 请填写 YYYY-MM-DD 日期。"
        )
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise PaperAccountValidationError(
                "模拟成交日期格式无效。下一步: 请填写 YYYY-MM-DD 日期。"
            ) from exc
    raise PaperAccountValidationError(
        "模拟成交日期不能为空。下一步: 请填写 YYYY-MM-DD 日期。"
    )


def _parse_price_cents(value: Any) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PaperAccountValidationError(
            "模拟成交价必须是大于 0 的有限数值。下一步: 请核对成交价。"
        ) from exc
    if not amount.is_finite() or amount <= 0:
        raise PaperAccountValidationError(
            "模拟成交价必须是大于 0 的有限数值。下一步: 请核对成交价。"
        )
    try:
        cents = int(
            (amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
    except (DecimalException, OverflowError, ValueError) as exc:
        raise PaperAccountValidationError(
            "模拟成交价超出可计算范围。下一步: 请核对成交价。"
        ) from exc
    if cents <= 0:
        raise PaperAccountValidationError(
            "模拟成交价精确到分后必须大于 0。下一步: 请核对成交价。"
        )
    return cents


def _parse_quantity(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PaperAccountValidationError(
            "模拟数量必须是大于 0 的整数股数。下一步: 请重新填写数量。"
        )
    return value


def _parse_side(value: Any) -> str:
    if not isinstance(value, str) or value.strip().upper() not in {"BUY", "SELL"}:
        raise PaperAccountValidationError(
            "模拟方向只能是 BUY 或 SELL。下一步: 请重新选择模拟方向。"
        )
    return value.strip().upper()


def _parse_symbol(value: Any) -> str:
    symbol = value.strip().upper() if isinstance(value, str) else ""
    if not _STOCK_SYMBOL.fullmatch(symbol):
        raise PaperAccountValidationError(
            "仅支持带交易所后缀的中国内地股票, 不支持 ETF、可转债、港股或美股。"
            "下一步: 请核对六位股票代码和 .SH/.SZ/.BJ 后缀。"
        )
    return symbol


def _parse_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PaperAccountValidationError(
            "股票名称不能为空。下一步: 请填写当前模拟标的名称。"
        )
    if len(value) > 80:
        raise PaperAccountValidationError(
            "股票名称不能超过 80 个字符。下一步: 请缩短名称后再提交。"
        )
    return value


def _parse_note(value: Any, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PaperAccountValidationError(
            f"{label}必须是文字。下一步: 请重新填写后再提交。"
        )
    if len(value) > 500:
        raise PaperAccountValidationError(
            f"{label}不能超过 500 个字符。下一步: 请缩短内容后再提交。"
        )
    return value


def _validate_buy_lot(symbol: str, quantity: int) -> None:
    if symbol.startswith(("688", "689")) and quantity < 200:
        raise PaperAccountValidationError(
            "科创板模拟买入至少 200 股。下一步: 请把数量改为 200 股或以上。"
        )
    if quantity % 100:
        raise PaperAccountValidationError(
            "模拟买入数量必须是 100 股的整数倍。下一步: 请按整手重新填写数量。"
        )


def _commission_cents(gross_cents: int) -> int:
    variable = int(
        (Decimal(gross_cents) * COMMISSION_RATE).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )
    return max(MINIMUM_COMMISSION_CENTS, variable)


def _stamp_tax_cents(gross_cents: int, side: str) -> int:
    if side != "SELL":
        return 0
    return int(
        (Decimal(gross_cents) * SELL_STAMP_TAX_RATE).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )


def _money(cents: int) -> float:
    return round(cents / 100, 2)


def calculate_trade_fees(*, price: Any, quantity: Any, side: Any) -> dict[str, float]:
    """Calculate the documented fixed simulation fees without placing any order."""
    parsed_side = _parse_side(side)
    price_cents = _parse_price_cents(price)
    parsed_quantity = _parse_quantity(quantity)
    gross_cents = price_cents * parsed_quantity
    commission_cents = _commission_cents(gross_cents)
    stamp_tax_cents = _stamp_tax_cents(gross_cents, parsed_side)
    return {
        "gross_amount": _money(gross_cents),
        "commission": _money(commission_cents),
        "stamp_tax": _money(stamp_tax_cents),
        "total_fees": _money(commission_cents + stamp_tax_cents),
    }


def reset_account(
    data_dir: Path,
    *,
    initial_cash: Any,
    confirmation: Any,
    as_of: date | None = None,
) -> dict[str, Any]:
    if confirmation != "RESET":
        raise PaperAccountValidationError(
            "重置会清空全部模拟持仓和日志。下一步: 请准确输入 RESET 后再提交。"
        )
    parsed_cash = _parse_initial_cash(initial_cash)
    with _locked_account(data_dir) as path:
        state = _new_state(parsed_cash)
        _write_state(path, state)
    return _build_response(data_dir, state, as_of=as_of or date.today())


def get_account(
    data_dir: Path,
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    with _locked_account(data_dir) as path:
        state = deepcopy(_read_state(path))
    return _build_response(data_dir, state, as_of=as_of or date.today())


def record_trade(
    data_dir: Path,
    *,
    symbol: Any,
    name: Any,
    side: Any,
    quantity: Any,
    price: Any,
    trade_date: Any,
    plan_note: Any,
    invalidation_note: Any,
) -> dict[str, Any]:
    """Persist one manual simulated fill; this function never contacts a broker."""
    parsed_symbol = _parse_symbol(symbol)
    parsed_name = _parse_name(name)
    parsed_side = _parse_side(side)
    parsed_quantity = _parse_quantity(quantity)
    price_cents = _parse_price_cents(price)
    parsed_date = _parse_trade_date(trade_date)
    parsed_plan = _parse_note(plan_note, "模拟计划")
    parsed_invalidation = _parse_note(invalidation_note, "失效条件")
    if parsed_side == "BUY":
        _validate_buy_lot(parsed_symbol, parsed_quantity)

    with _locked_account(data_dir) as path:
        state = _read_state(path)
        cash_before_cents = state["cash_cents"]
        gross_cents = price_cents * parsed_quantity
        commission_cents = _commission_cents(gross_cents)
        stamp_tax_cents = _stamp_tax_cents(gross_cents, parsed_side)
        total_fees_cents = commission_cents + stamp_tax_cents

        if parsed_side == "BUY":
            realized_pnl_cents = 0
            cash_needed_cents = gross_cents + commission_cents
            if cash_needed_cents > cash_before_cents:
                raise PaperAccountValidationError(
                    "模拟账户现金不足, 无法覆盖成交金额和佣金。"
                    "下一步: 请降低数量或成交价后重新计算。"
                )
            state["cash_cents"] -= cash_needed_cents
            lot_id = uuid4().hex
            state["lots"].append(
                {
                    "id": lot_id,
                    "symbol": parsed_symbol,
                    "name": parsed_name,
                    "trade_date": parsed_date.isoformat(),
                    "quantity": parsed_quantity,
                    "remaining_quantity": parsed_quantity,
                    "price_cents": price_cents,
                    "buy_commission_cents": commission_cents,
                    "remaining_buy_commission_cents": commission_cents,
                }
            )
            fifo_cost_basis_cents = gross_cents + commission_cents
        else:
            cash_received_cents = gross_cents - total_fees_cents
            if cash_before_cents + cash_received_cents < 0:
                raise PaperAccountValidationError(
                    "本次模拟卖出收入不足以覆盖费用, 会导致现金透支。"
                    "下一步: 请提高模拟成交价或调整数量后重试。"
                )
            fifo_cost_basis_cents = _consume_fifo(
                state,
                symbol=parsed_symbol,
                quantity=parsed_quantity,
                sell_date=parsed_date,
            )
            realized_pnl_cents = (
                cash_received_cents - fifo_cost_basis_cents
            )
            state["cash_cents"] += cash_received_cents
            state["realized_pnl_cents"] += realized_pnl_cents

        timestamp = datetime.now(UTC).isoformat(timespec="seconds")
        state["journal"].append(
            {
                "id": uuid4().hex,
                "timestamp": timestamp,
                "side": parsed_side,
                "symbol": parsed_symbol,
                "name": parsed_name,
                "trade_date": parsed_date.isoformat(),
                "quantity": parsed_quantity,
                "price_cents": price_cents,
                "gross_amount_cents": gross_cents,
                "cash_before_cents": cash_before_cents,
                "cash_after_cents": state["cash_cents"],
                "commission_cents": commission_cents,
                "stamp_tax_cents": stamp_tax_cents,
                "total_fees_cents": total_fees_cents,
                "fifo_cost_basis_cents": fifo_cost_basis_cents,
                "realized_pnl_cents": realized_pnl_cents,
                "plan_note": parsed_plan,
                "invalidation_note": parsed_invalidation,
            }
        )
        _write_state(path, state)
        state_snapshot = deepcopy(state)
    return _build_response(data_dir, state_snapshot, as_of=date.today())


def _consume_fifo(
    state: dict[str, Any],
    *,
    symbol: str,
    quantity: int,
    sell_date: date,
) -> int:
    matching = [
        (index, lot)
        for index, lot in enumerate(state["lots"])
        if lot.get("symbol") == symbol and int(lot.get("remaining_quantity", 0)) > 0
    ]
    owned_quantity = sum(int(lot["remaining_quantity"]) for _, lot in matching)
    if owned_quantity == 0:
        raise PaperAccountValidationError(
            "当前没有可模拟卖出的持仓, 已阻止卖空。下一步: 请先核对持仓。"
        )
    if quantity > owned_quantity:
        raise PaperAccountValidationError(
            "模拟卖出数量超过持仓, 已阻止卖空。下一步: 请降低数量后重试。"
        )
    eligible = sorted(
        (
            (index, lot)
            for index, lot in matching
            if date.fromisoformat(str(lot["trade_date"])) < sell_date
        ),
        key=lambda item: (str(item[1]["trade_date"]), item[0]),
    )
    sellable_quantity = sum(int(lot["remaining_quantity"]) for _, lot in eligible)
    if quantity > sellable_quantity:
        raise PaperAccountValidationError(
            "可卖数量不足: 当日买入部分受 T+1 规则限制。"
            "下一步: 请等待下一交易日或降低模拟卖出数量。"
        )

    remaining_to_sell = quantity
    consumed_cost_cents = 0
    for _, lot in eligible:
        if remaining_to_sell == 0:
            break
        old_quantity = int(lot["remaining_quantity"])
        take = min(old_quantity, remaining_to_sell)
        remaining_fee = int(lot["remaining_buy_commission_cents"])
        if take == old_quantity:
            allocated_fee = remaining_fee
        else:
            allocated_fee = int(
                (
                    Decimal(remaining_fee)
                    * Decimal(take)
                    / Decimal(old_quantity)
                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
        consumed_cost_cents += int(lot["price_cents"]) * take + allocated_fee
        lot["remaining_quantity"] = old_quantity - take
        lot["remaining_buy_commission_cents"] = remaining_fee - allocated_fee
        remaining_to_sell -= take
    return consumed_cost_cents


def _load_strategy_marks(data_dir: Path) -> dict[str, int]:
    try:
        cache = strategy_cache.read_cache(Path(data_dir))
        if not isinstance(cache, dict):
            return {}
        as_of = cache.get("as_of")
        if not isinstance(as_of, str) or not as_of:
            return {}
        if date.fromisoformat(as_of).isoformat() != as_of:
            return {}
        results = cache.get("results")
        if not isinstance(results, dict):
            return {}

        marks: dict[str, int] = {}
        strategy_ids = sorted(
            strategy_id
            for strategy_id in results
            if isinstance(strategy_id, str)
        )
        for strategy_id in strategy_ids:
            result = results.get(strategy_id)
            if not isinstance(result, dict) or result.get("as_of") != as_of:
                continue
            rows = result.get("rows")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                symbol = row.get("symbol")
                if not isinstance(symbol, str) or not symbol or symbol in marks:
                    continue
                try:
                    cents = _parse_price_cents(row.get("close"))
                except PaperAccountValidationError:
                    continue
                marks[symbol] = cents
        return marks
    except Exception:
        # 估值缓存是只读辅助信息, 任意读取/结构异常都只能触发成本回退,
        # 不能让已经原子写入的模拟成交以响应错误的形式制造重复提交歧义。
        return {}


def _build_response(
    data_dir: Path,
    state: dict[str, Any],
    *,
    as_of: date,
) -> dict[str, Any]:
    marks = _load_strategy_marks(data_dir)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lot in state["lots"]:
        if int(lot.get("remaining_quantity", 0)) > 0:
            grouped[str(lot["symbol"])].append(lot)

    positions: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    total_cost_cents = 0
    total_marked_cents = 0
    marked_cents_by_symbol: dict[str, int] = {}
    for symbol in sorted(grouped):
        lots = grouped[symbol]
        quantity = sum(int(lot["remaining_quantity"]) for lot in lots)
        sellable = sum(
            int(lot["remaining_quantity"])
            for lot in lots
            if date.fromisoformat(str(lot["trade_date"])) < as_of
        )
        cost_cents = sum(
            int(lot["price_cents"]) * int(lot["remaining_quantity"])
            + int(lot["remaining_buy_commission_cents"])
            for lot in lots
        )
        mark_cents = marks.get(symbol)
        if mark_cents is None:
            mark_source = "COST_FALLBACK"
            marked_cents = cost_cents
            mark_price = round(cost_cents / quantity / 100, 4)
            warnings.append(
                {
                    "code": "COST_FALLBACK",
                    "symbol": symbol,
                    "message": "策略缓存没有可用价格, 当前按持仓成本估值",
                }
            )
        else:
            mark_source = "STRATEGY_CACHE"
            marked_cents = mark_cents * quantity
            mark_price = _money(mark_cents)
        total_cost_cents += cost_cents
        total_marked_cents += marked_cents
        marked_cents_by_symbol[symbol] = marked_cents
        positions.append(
            {
                "symbol": symbol,
                "name": str(lots[-1].get("name") or symbol),
                "quantity": quantity,
                "sellable_quantity": sellable,
                "average_cost": round(cost_cents / quantity / 100, 4),
                "cost_basis": _money(cost_cents),
                "mark_price": mark_price,
                "marked_value": _money(marked_cents),
                "market_value": _money(marked_cents),
                "unrealized_pnl": _money(marked_cents - cost_cents),
                "mark_source": mark_source,
            }
        )

    cash_cents = int(state["cash_cents"])
    initial_cash_cents = int(state["initial_cash_cents"])
    realized_cents = int(state["realized_pnl_cents"])
    unrealized_cents = total_marked_cents - total_cost_cents
    equity_cents = cash_cents + total_marked_cents
    total_pnl_cents = equity_cents - initial_cash_cents
    if total_pnl_cents != realized_cents + unrealized_cents:
        raise PaperAccountStorageError(
            "模拟账户金额无法对账, 已停止返回结果。下一步: 请先备份账户文件。"
        )

    for position in positions:
        marked_cents = marked_cents_by_symbol[position["symbol"]]
        position["portfolio_weight_pct"] = _percent(marked_cents, equity_cents)
        position["invested_weight_pct"] = _percent(
            marked_cents,
            total_marked_cents,
        )
    portfolio_risk = _build_portfolio_risk(
        positions=positions,
        marked_cents_by_symbol=marked_cents_by_symbol,
        cash_cents=cash_cents,
        total_marked_cents=total_marked_cents,
        equity_cents=equity_cents,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "valuation_date": as_of.isoformat(),
        "initial_cash": _money(initial_cash_cents),
        "cash": _money(cash_cents),
        "cost_basis": _money(total_cost_cents),
        "marked_value": _money(total_marked_cents),
        "market_value": _money(total_marked_cents),
        "total_equity": _money(equity_cents),
        "realized_pnl": _money(realized_cents),
        "unrealized_pnl": _money(unrealized_cents),
        "total_pnl": _money(total_pnl_cents),
        "positions": positions,
        "portfolio_risk": portfolio_risk,
        "fee_assumptions": {
            "commission_rate": float(COMMISSION_RATE),
            "commission_rate_label": "0.03%",
            "minimum_commission": _money(MINIMUM_COMMISSION_CENTS),
            "sell_stamp_tax_rate": float(SELL_STAMP_TAX_RATE),
            "sell_stamp_tax_rate_label": "0.05%",
            "slippage": "用户填写的模拟成交价视为已经包含自行判断的滑点",
            "disclaimer": "费用仅为模拟假设, 实际费用以券商为准",
        },
        "valuation_warnings": warnings,
        "journal": [_journal_response(entry) for entry in state["journal"]],
    }


def _percent(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator * 100, 2)


def _build_portfolio_risk(
    *,
    positions: list[dict[str, Any]],
    marked_cents_by_symbol: dict[str, int],
    cash_cents: int,
    total_marked_cents: int,
    equity_cents: int,
) -> dict[str, Any]:
    position_count = len(positions)
    cash_pct = _percent(cash_cents, equity_cents)
    invested_pct = _percent(total_marked_cents, equity_cents)
    if position_count == 0 or total_marked_cents <= 0:
        return {
            "position_count": 0,
            "cash_pct": cash_pct,
            "invested_pct": invested_pct,
            "largest_position_pct": 0.0,
            "largest_invested_position_pct": 0.0,
            "concentration_hhi": 0.0,
            "concentration_level": "NONE",
            "warnings": [],
        }

    invested_weights = [
        value / total_marked_cents
        for value in marked_cents_by_symbol.values()
    ]
    largest_invested_pct = round(max(invested_weights) * 100, 2)
    largest_position_pct = max(
        float(position["portfolio_weight_pct"])
        for position in positions
    )
    concentration_hhi = round(sum(weight * weight for weight in invested_weights), 4)
    risk_warnings: list[dict[str, str]] = []

    if position_count == 1:
        concentration_level = "EXTREME"
        risk_warnings.append(
            {
                "code": "SINGLE_POSITION_CONCENTRATION",
                "message": "当前持仓内部100%集中于单一股票",
            }
        )
    elif largest_invested_pct >= 50:
        concentration_level = "HIGH"
        risk_warnings.append(
            {
                "code": "POSITION_CONCENTRATION",
                "message": (
                    "最大单一持仓占已投资资产"
                    f"{largest_invested_pct:.2f}%"
                ),
            }
        )
    elif largest_invested_pct >= 35:
        concentration_level = "MODERATE"
        risk_warnings.append(
            {
                "code": "POSITION_CONCENTRATION",
                "message": (
                    "最大单一持仓占已投资资产"
                    f"{largest_invested_pct:.2f}%"
                ),
            }
        )
    else:
        concentration_level = "LOW"

    return {
        "position_count": position_count,
        "cash_pct": cash_pct,
        "invested_pct": invested_pct,
        "largest_position_pct": largest_position_pct,
        "largest_invested_position_pct": largest_invested_pct,
        "concentration_hhi": concentration_hhi,
        "concentration_level": concentration_level,
        "warnings": risk_warnings,
    }


def _journal_response(entry: dict[str, Any]) -> dict[str, Any]:
    response = {
        key: deepcopy(entry.get(key))
        for key in (
            "id",
            "timestamp",
            "side",
            "symbol",
            "name",
            "trade_date",
            "quantity",
            "plan_note",
            "invalidation_note",
        )
    }
    for target, source in (
        ("price", "price_cents"),
        ("gross_amount", "gross_amount_cents"),
        ("cash_before", "cash_before_cents"),
        ("cash_after", "cash_after_cents"),
        ("commission", "commission_cents"),
        ("stamp_tax", "stamp_tax_cents"),
        ("total_fees", "total_fees_cents"),
        ("fifo_cost_basis", "fifo_cost_basis_cents"),
        ("realized_pnl", "realized_pnl_cents"),
    ):
        response[target] = _money(int(entry.get(source, 0)))
    return response
