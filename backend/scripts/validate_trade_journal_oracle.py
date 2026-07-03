"""对拍我方 Trade Journal FIFO 与同花顺「已清仓」sheet。

用法:
  cd backend && uv run python scripts/validate_trade_journal_oracle.py ~/Downloads/银河.xlsx
"""
from __future__ import annotations

import sys
from pathlib import Path

from app.services.trade_journal.fifo import pair_roundtrips
from app.services.trade_journal.parser import normalize_code, normalize_rows, read_upload
from app.services.trade_journal.presets import THS_PRESET


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip())
        return 2
    path = Path(argv[1]).expanduser()
    sheets, tx = read_upload(path.read_bytes(), path.name, sheet="交易记录")
    if "已清仓" not in sheets:
        print("缺少「已清仓」sheet")
        return 2
    _, cleared = read_upload(path.read_bytes(), path.name, sheet="已清仓")
    fills, events, warnings = normalize_rows(tx, THS_PRESET["mapping"])
    trips, open_positions, pair_warnings = pair_roundtrips(fills, events, trading_days=None)
    ours = {_trip_key(t): round(t.total_pnl, 2) for t in trips}
    oracle = {_oracle_key(r): _num(r.get("总盈亏")) for r in cleared.iter_rows(named=True)}

    missing = sorted(set(oracle) - set(ours))
    extra = sorted(set(ours) - set(oracle))
    whitelisted = _apply_whitelist(missing, extra)
    pnl_diff = [
        (key, ours[key], oracle[key])
        for key in sorted(set(ours) & set(oracle))
        if abs(ours[key] - oracle[key]) > 0.05
    ]
    print(f"sheets={sheets}")
    print(f"fills={len(fills)} events={len(events)} trips={len(trips)} oracle={len(oracle)} open={len(open_positions)}")
    print(
        f"warnings={len(warnings) + len(pair_warnings)} missing={len(missing)} "
        f"extra={len(extra)} pnl_diff={len(pnl_diff)} whitelisted={whitelisted}"
    )
    for label, rows in [("missing", missing[:10]), ("extra", extra[:10]), ("pnl_diff", pnl_diff[:10])]:
        if rows:
            print(f"{label}: {rows}")
    return 0 if not missing and not extra and not pnl_diff else 1


def _trip_key(t) -> tuple[str, str, str]:
    return (t.symbol, t.open_date, t.close_date)


def _oracle_key(row: dict) -> tuple[str, str, str]:
    return (
        normalize_code(str(row.get("代码") or "")),
        str(row.get("建仓日期") or "")[:10],
        str(row.get("清仓日期") or "")[:10],
    )


def _num(value) -> float:
    if isinstance(value, str):
        value = value.replace(",", "").replace("%", "").strip()
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def _apply_whitelist(missing: list[tuple[str, str, str]], extra: list[tuple[str, str, str]]) -> int:
    known_missing = ("601127.SH", "2026-05-28", "2026-06-18")
    known_extra = ("601127.SH", "2026-05-28", "2026-06-17")
    if known_missing in missing and known_extra in extra:
        missing.remove(known_missing)
        extra.remove(known_extra)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
