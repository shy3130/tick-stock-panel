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
    _validate_state(value)
    return value


def _validate_state(state: Any) -> None:
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise PaperAccountStorageError("模拟账户文件版本不受支持, 请先备份后再重置账户。")
    for field in ("initial_cash_cents", "cash_cents", "realized_pnl_cents"):
        if isinstance(state.get(field), bool) or not isinstance(state.get(field), int):
            raise PaperAccountStorageError(f"模拟账户文件字段 {field} 无效, 请先备份后再重置。")
    if state["initial_cash_cents"] not in {500_000, 1_000_000}:
        raise PaperAccountStorageError("模拟账户初始资金无效, 请先备份后再重置账户。")
    if state["cash_cents"] < 0:
        raise PaperAccountStorageError("模拟账户现金为负数, 已停止读取以防错误扩大。")
    if not isinstance(state.get("lots"), list) or not isinstance(state.get("journal"), list):
        raise PaperAccountStorageError("模拟账户持仓或日志结构无效, 请先备份后再重置。")


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
    return value.strip()[:80]


def _parse_note(value: Any, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PaperAccountValidationError(
            f"{label}必须是文字。下一步: 请重新填写后再提交。"
        )
    return value.strip()[:500]


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
            fifo_cost_basis_cents = _consume_fifo(
                state,
                symbol=parsed_symbol,
                quantity=parsed_quantity,
                sell_date=parsed_date,
            )
            cash_received_cents = gross_cents - total_fees_cents
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
    cache = strategy_cache.read_cache(Path(data_dir))
    if not isinstance(cache, dict):
        return {}
    as_of = str(cache.get("as_of") or "")
    marks: dict[str, int] = {}
    results = cache.get("results")
    if not isinstance(results, dict):
        return marks
    for strategy_id in sorted(results):
        result = results.get(strategy_id)
        if not isinstance(result, dict) or str(result.get("as_of") or "") != as_of:
            continue
        rows = result.get("rows")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbol")
            if not isinstance(symbol, str) or symbol in marks:
                continue
            try:
                close = Decimal(str(row.get("close")))
            except (InvalidOperation, ValueError, TypeError):
                continue
            if not close.is_finite() or close <= 0:
                continue
            cents = int((close * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            if cents > 0:
                marks[symbol] = cents
    return marks


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
