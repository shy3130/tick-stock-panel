"""Sina/Tencent batch realtime quote client.

Outputs plain rows that are normalized by ``normalizer.normalize_realtime``.
Units: price=CNY, volume=shares, amount=CNY, change_pct=percentage points.
"""
from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

TENCENT_URL = "https://qt.gtimg.cn/q="
SINA_URL = "https://hq.sinajs.cn/list="
TENCENT_BATCH = 60
SINA_BATCH = 100

_SUFFIX_TO_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj", "HK": "hk"}


def _to_exch_code(symbol: str) -> str:
    code, _, suffix = symbol.strip().upper().partition(".")
    return f"{_SUFFIX_TO_PREFIX.get(suffix, 'sh')}{code}"


def _to_symbol(exch_code: str) -> str:
    prefix = exch_code[:2].lower()
    suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ", "hk": "HK"}.get(prefix, prefix.upper())
    return f"{exch_code[2:]}.{suffix}"


def _f(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _n(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_tencent(text: str) -> list[dict]:
    rows: list[dict] = []
    for line in text.strip().split(";"):
        if "=" not in line:
            continue
        key, _, payload = line.partition("=")
        exch_code = key.split("v_")[-1].strip()
        parts = payload.strip().strip('"').split("~")
        if len(parts) < 7:
            continue
        vol_hand = _f(parts[6])
        row = {
            "symbol": _to_symbol(exch_code),
            "name": parts[1] if len(parts) > 1 else None,
            "last_price": _f(parts[3]) if len(parts) > 3 else None,
            "prev_close": _f(parts[4]) if len(parts) > 4 else None,
            "open": _f(parts[5]) if len(parts) > 5 else None,
            "volume": vol_hand * 100 if vol_hand is not None else None,
            "timestamp": None,
            "source": "tencent",
            "ext": {},
        }
        if len(parts) > 33:
            row["high"] = _f(parts[33])
        if len(parts) > 34:
            row["low"] = _f(parts[34])
        if len(parts) > 37:
            amount_10k = _f(parts[37])
            row["amount"] = amount_10k * 10000 if amount_10k is not None else None
        rows.append(row)
    return rows


def parse_tencent_depth(text: str) -> dict:
    rows: dict = {}
    for line in text.strip().split(";"):
        if "=" not in line:
            continue
        key, _, payload = line.partition("=")
        exch_code = key.split("v_")[-1].strip()
        parts = payload.strip().strip('"').split("~")
        if len(parts) < 29:
            continue

        bid_prices: list[float | None] = []
        bid_volumes: list[int | None] = []
        ask_prices: list[float | None] = []
        ask_volumes: list[int | None] = []
        for i in range(5):
            bid_prices.append(_n(parts[9 + i * 2]))
            bid_vol = _n(parts[10 + i * 2])
            bid_volumes.append(int(bid_vol) if bid_vol is not None else None)
            ask_prices.append(_n(parts[19 + i * 2]))
            ask_vol = _n(parts[20 + i * 2])
            ask_volumes.append(int(ask_vol) if ask_vol is not None else None)

        rows[_to_symbol(exch_code)] = {
            "bid_prices": bid_prices,
            "bid_volumes": bid_volumes,
            "ask_prices": ask_prices,
            "ask_volumes": ask_volumes,
            "timestamp": None,
            "source": "tencent",
        }
    return rows


def parse_sina(text: str, exch_codes: list[str] | None = None) -> list[dict]:  # noqa: ARG001
    rows: list[dict] = []
    for line in text.strip().split(";"):
        if "hq_str_" not in line:
            continue
        key, _, payload = line.partition("=")
        exch_code = key.split("hq_str_")[-1].strip()
        parts = payload.strip().strip('"').split(",")
        if len(parts) < 10:
            continue
        row = {
            "symbol": _to_symbol(exch_code),
            "name": parts[0],
            "open": _f(parts[1]),
            "prev_close": _f(parts[2]),
            "last_price": _f(parts[3]),
            "high": _f(parts[4]),
            "low": _f(parts[5]),
            "volume": _f(parts[8]),
            "amount": _f(parts[9]),
            "timestamp": None,
            "source": "sina",
            "ext": {},
        }
        if len(parts) > 31 and parts[30] and parts[31]:
            row["timestamp"] = f"{parts[30]}T{parts[31]}"
        rows.append(row)
    return rows


class SinaTencentClient:
    def __init__(self, timeout: float = 4.0) -> None:
        self.timeout = timeout
        self._failures: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}

    def _source_available(self, source: str) -> bool:
        until = self._cooldown_until.get(source, 0)
        if until > time.monotonic():
            logger.debug("%s quote source cooling down for %.1fs", source, until - time.monotonic())
            return False
        return True

    def _record_success(self, source: str) -> None:
        self._failures[source] = 0
        self._cooldown_until.pop(source, None)

    def _record_failure(self, source: str) -> None:
        failures = self._failures.get(source, 0) + 1
        self._failures[source] = failures
        if failures >= 3:
            self._cooldown_until[source] = time.monotonic() + 60
            logger.warning("%s quote source disabled for 60s after %d failures", source, failures)

    def _http_get(self, url: str, source: str, headers: dict | None = None) -> str | None:
        if not self._source_available(source):
            return None
        try:
            resp = httpx.get(url, headers=headers or {}, timeout=self.timeout, trust_env=False)
            resp.raise_for_status()
        except Exception as e:  # noqa: BLE001
            self._record_failure(source)
            logger.warning("%s quote GET failed: %s", source, e)
            return None
        self._record_success(source)
        return resp.text

    def get_quotes(self, symbols: list[str], prefer: str = "tencent") -> list[dict]:
        codes = [_to_exch_code(s) for s in symbols if str(s).strip()]
        if not codes:
            return []

        source = "sina" if prefer == "sina" else "tencent"
        batch = SINA_BATCH if source == "sina" else TENCENT_BATCH
        rows: list[dict] = []

        for start in range(0, len(codes), batch):
            chunk = codes[start:start + batch]
            if source == "sina":
                text = self._http_get(
                    SINA_URL + ",".join(chunk),
                    "sina",
                    headers={"Referer": "https://finance.sina.com.cn"},
                )
                if text:
                    rows.extend(parse_sina(text, chunk))
                continue

            text = self._http_get(TENCENT_URL + ",".join(chunk), "tencent")
            if text:
                rows.extend(parse_tencent(text))
        return rows

    def get_depth(self, symbols: list[str]) -> dict:
        codes = [_to_exch_code(s) for s in symbols if str(s).strip()]
        if not codes:
            return {}

        rows: dict = {}
        for start in range(0, len(codes), TENCENT_BATCH):
            chunk = codes[start:start + TENCENT_BATCH]
            text = self._http_get(TENCENT_URL + ",".join(chunk), "tencent")
            if text:
                rows.update(parse_tencent_depth(text))
        return rows
