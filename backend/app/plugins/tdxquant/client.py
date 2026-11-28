"""TdxQuant — tdxaidata (通达信量化数据) 原生库封装。

tdxaidata 是 ctypes 包装的私有 TCP 协议客户端(aihs/aids.tdx.com.cn),
DLL 与运行时 ini 随 wheel 分发。两个实测约束决定本模块的形状:
  - 原生层每进程加载一次、token 写在 DLL 旁的 ini 里 → Key 物化到私有
    lib 目录 + TDX_AI_DATA_LIB 指向, 不污染 site-packages、换 Key 只重写 ini;
  - 空闲后首次调用会 "Invalid server response" → 统一重试包装。
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import threading
import time
from contextlib import nullcontext
from datetime import date, datetime
from pathlib import Path

# Keep the module imports used by the TSP probe, plugins and their callers.
from tivelo_data.legacy.tdx_runtime import (  # noqa: F401
    LIB_DLL_NAME,
    LIB_ENV,
    TdxQuantAuthError,
    TdxQuantError,
    _raise_response_error,
    historical_arguments,
    materialize_lib,
    package_lib_dir,
    shutil,
    write_token_ini,
)

logger = logging.getLogger(__name__)

# 全 A 股票池: 五个市场名实测(2026-10)合计约 5579 只; ETF/指数不在枚举内
_MARKETS = ("上证主板", "科创板", "深证主板", "创业板", "北交所")

# 快照批量字段(实测有效名; 注意昨收叫 LastClose、高低叫 Max/Min, 快照无 Name)
SNAPSHOT_FIELDS = (
    "Now", "Open", "Max", "Min", "LastClose", "Volume", "Amount", "RefreshTime",
    "Buyp", "Buyv", "Sellp", "Sellv",
)

_RETRIES = 3
_NONE_RETRIES = 2  # 空返回(可能是永久性坏代码)只补试一次
_RETRY_SLEEP_S = 0.6
_UNIVERSE_TTL_S = 12 * 3600.0

_SESSION_LOCK = threading.RLock()
_ACTIVE_SESSION_FP: str | None = None


def _session_fingerprint(dll_path: Path, api_key: str) -> str:
    return hashlib.sha256(f"{dll_path.resolve()}\0{api_key}".encode()).hexdigest()


def _bind_native_session(native_cls, dll_path: Path, api_key: str) -> None:
    """将进程级 _NativeTdxAiData 单例绑定到当前 (dll_path, api_key)。

    tdxaidata 原生层为进程级单例，DLL 仅在 start() 时读取一次 TdxAiData.ini。
    进程内更换或重存 Key 时必须先 stop() 并复位 _is_started，否则后续请求仍沿用旧 Token。
    """
    global _ACTIVE_SESSION_FP

    fp = _session_fingerprint(dll_path, api_key)
    with _SESSION_LOCK:
        os.environ[LIB_ENV] = str(dll_path)
        native = native_cls.get_instance()
        changed = fp != _ACTIVE_SESSION_FP
        if changed and getattr(native, "_is_started", False):
            start_lock = getattr(native, "_start_lock", None)
            with start_lock if start_lock is not None else nullcontext():
                native._dll_path = str(dll_path)
                dll = getattr(native, "_dll", None)
                if dll is not None and hasattr(dll, "stop"):
                    try:
                        dll.stop()
                    except Exception as exc:  # noqa: BLE001 — 原生层关闭异常不得阻塞重建
                        logger.warning("TdxQuant 原生会话停止失败(将继续重置): %s", exc)
                native._is_started = False
            logger.info("TdxQuant Key 已变更，已重置原生会话，下一次请求将加载新凭据")
        elif changed and hasattr(native, "_dll_path"):
            native._dll_path = str(dll_path)
        _ACTIVE_SESSION_FP = fp


class TdxQuantClient:
    """原生 tqs 的薄封装: 重试、股票池枚举、快照/日K/除权三个取数面。"""

    def __init__(self, api_key: str, lib_root: Path | None = None) -> None:
        from app.config import settings

        self.api_key = api_key
        self._lib_root = lib_root or (settings.data_dir / "user_data" / "tdxquant_lib")
        dll = materialize_lib(api_key, self._lib_root)
        os.environ[LIB_ENV] = str(dll)
        from tdxaidata import tqs
        from tdxaidata.tdxaidata import _NativeTdxAiData

        self._tqs = tqs
        self._native_cls = _NativeTdxAiData
        _bind_native_session(self._native_cls, dll, api_key)
        self._universe: list[str] = []
        self._universe_at = 0.0
        self._warmup()

    def _warmup(self, attempts: int = 5) -> None:
        """DLL 冷启动后有一段"未就绪"窗口, 业务调用持续失败(实测首批整批丢弃)。

        用最小快照轮询到就绪; 鉴权/额度被服务端明确拒绝时立刻中止预热，不再重试刷屏；
        其它瞬态失败不抛 — 后续调用可能自愈, 只留告警。
        """
        for _ in range(attempts):
            try:
                native = self._native_cls.get_instance()
                if hasattr(native, "get_market_snapshot_batch"):
                    with _SESSION_LOCK:
                        raw = native.get_market_snapshot_batch("600519.SH", "Now")
                    _raise_response_error(raw, getattr(self, "api_key", ""))
                    if self._tqs._clean(raw):
                        return
                else:
                    rows = self._tqs.get_market_snapshot_batch(
                        stock_list=["600519.SH"], field_list=["Now"]
                    )
                    if isinstance(rows, dict) and rows:
                        return
            except TdxQuantAuthError as exc:
                logger.warning("TdxQuant 原生层预热遇到鉴权/额度异常，已中止预热: %s", exc)
                return
            except Exception:  # noqa: BLE001 — 就绪探测, 任何异常都重试
                pass
            time.sleep(1.2)
        logger.warning("TdxQuant 原生层预热未成功(%d 次), 首批业务调用可能失败重试", attempts)

    def close(self) -> None:  # 原生 DLL 常驻进程, 换 Key 由 _bind_native_session 负责热重载
        self._universe.clear()

    # ---- 原生调用(重试包装) ----
    def _raw(self, method: str, *args, retries: int = _RETRIES):
        """直接调原生层并 _clean 解包; 异常重试 retries 次、空返回补试 1 次。

        空闲后首连 "Invalid server response" 实测重试即恢复; 坏代码等
        永久错误重试也只会快失败(返回 None), 不至于长阻塞。
        原生调用本身受 _SESSION_LOCK 保护，避免换 Key 时 dll.stop() 与在途请求并发冲突。
        """
        last: Exception = TdxQuantError(f"{method} 未知失败")
        max_attempts = max(1, retries)
        for attempt in range(max_attempts):
            try:
                with _SESSION_LOCK:
                    raw = getattr(self._native_cls.get_instance(), method)(*args)
                _raise_response_error(raw, getattr(self, "api_key", ""))
                out = self._tqs._clean(raw)
                if out is not None:
                    return out
                last = TdxQuantError(f"{method} 返回空数据(第 {attempt + 1} 次)")
                if attempt + 1 >= min(_NONE_RETRIES, max_attempts):
                    break
            except TdxQuantAuthError:
                raise
            except Exception as exc:  # noqa: BLE001 — 原生 ctypes 层异常类型不可枚举
                last = TdxQuantError(f"{method}: {exc}")
            if attempt + 1 < max_attempts:
                time.sleep(_RETRY_SLEEP_S)
        raise last

    # ---- 股票池 ----
    def stock_universe(self) -> list[str]:
        """五市场全 A 代码(带交易所后缀)。12h 缓存, 避免每轮快照重复枚举。"""
        now = time.time()
        if self._universe and now - self._universe_at < _UNIVERSE_TTL_S:
            return list(self._universe)
        native = self._native_cls.get_instance()
        use_raw = hasattr(native, "get_stock_list")
        last: TdxQuantError = TdxQuantError("股票列表为空(五个市场均无返回)")
        for _attempt in range(_RETRIES):
            out: list[str] = []
            try:
                for market in _MARKETS:
                    if use_raw:
                        cleaned = self._raw("get_stock_list", market, 0, retries=1)
                        lst = (
                            cleaned["Value"]
                            if isinstance(cleaned, dict) and isinstance(cleaned.get("Value"), list)
                            else cleaned
                        )
                    else:
                        lst = self._tqs.get_stock_list(market=market, list_type=0)
                    out.extend(code for code in (lst or []) if isinstance(code, str) and code)
            except TdxQuantAuthError:
                raise
            except Exception as exc:  # noqa: BLE001 — SDK 内部异常类型不可枚举
                last = TdxQuantError(f"get_stock_list: {exc}")
                out = []
            if out:
                self._universe, self._universe_at = out, now
                return list(out)
            time.sleep(_RETRY_SLEEP_S)
        raise last

    # ---- 快照 ----
    def snapshot_batch(self, codes: list[str]) -> dict[str, dict]:
        """批量快照 → {code: row(字段见 SNAPSHOT_FIELDS)}。

        优先经 _raw() 在 _clean 之前检查服务端错误，确保鉴权/额度错误立刻以
        TdxQuantAuthError 上抛、不再静默重试与刷屏；无原生方法时回退 tqs 兼容测试替身。
        """
        if not codes:
            return {}
        stocks = list(codes)
        native = self._native_cls.get_instance()
        if hasattr(native, "get_market_snapshot_batch") and hasattr(self._tqs, "_batch_records"):
            cleaned = self._raw(
                "get_market_snapshot_batch", ",".join(stocks), ",".join(SNAPSHOT_FIELDS)
            )
            rows = self._tqs._batch_records(cleaned, stocks)
            if isinstance(rows, dict) and rows:
                return rows
            raise TdxQuantError(f"snapshot_batch({len(stocks)} 只): 空返回")
        last: TdxQuantError = TdxQuantError(f"snapshot_batch({len(stocks)} 只): 未知失败")
        for _attempt in range(_RETRIES):
            try:
                rows = self._tqs.get_market_snapshot_batch(
                    stock_list=stocks, field_list=list(SNAPSHOT_FIELDS)
                )
            except TdxQuantAuthError:
                raise
            except Exception as exc:  # noqa: BLE001 — SDK 内部异常类型不可枚举
                last = TdxQuantError(f"snapshot_batch({len(stocks)} 只): {exc}")
                time.sleep(_RETRY_SLEEP_S)
                continue
            if isinstance(rows, dict) and rows:
                return rows
            last = TdxQuantError(f"snapshot_batch({len(stocks)} 只): 空返回")
            time.sleep(_RETRY_SLEEP_S)
        raise last

    # ---- K 线(count 模式, 原始列) ----
    def his_dats(self, code: str, end_d: date | datetime, count: int, period: str = "1d") -> dict:
        """单标的 K 线原生 dict: Date 'YYYYMMDD' / Open / High / Low / Close /
        Volume(股) / Amount(万元) 等。count 模式下 start 恒空(服务端只用 end)。

        传 datetime 时保留时刻 — 分钟周期把 end 截到 00:00:00 会整日丢当根
        (#509); 传 date 时沿用日K惯例的当日零点。部分环境的原生返回会把
        数据整体包在 "Value" 键里, 这里统一解包 (SDK get_market_data 同款)。
        """
        if count <= 0:
            return {}
        out = self._raw("get_his_dats", *historical_arguments(code, end_d, count, period))
        if isinstance(out, dict):
            inner = out.get("Value")
            if isinstance(inner, dict):
                out = inner
        return out if isinstance(out, dict) else {}

    # ---- 除权事件 ----
    def divid_factors(
        self, code: str, start_d: date | None = None, end_d: date | None = None
    ) -> list[dict]:
        """单标的除权事件, 每行 {Date 'YYYYMMDD', Type, Bonus, ShareBonus,
        AllotPrice, Allotment} — 金额/比例均为**每 10 股**口径(Bonus=10派X元)。
        """
        start = f"{start_d:%Y-%m-%d} 00:00:00" if start_d else ""
        end = f"{end_d:%Y-%m-%d} 00:00:00" if end_d else ""
        # SDK get_divid_factors 会把 _clean 拒绝的服务端错误和正常无事件
        # 都变成空 DataFrame。与日K共用原生重试/错误检查，避免丢失原因；
        # 使用同一 get_cw_data 端点及 SDK 1.2.2 的四列顺序，单位不变。
        out = self._raw("get_cw_data", str(code).strip(), start, end)
        if not isinstance(out, dict):
            raise TdxQuantError(f"get_cw_data({code}) 返回异常结构: {type(out)!r}")
        if not out:
            return []
        dates, values, types = out.get("Date"), out.get("Value"), out.get("Type", [])
        if (not all(isinstance(v, list) for v in (dates, values, types))
                or len(dates) != len(values)):
            raise TdxQuantError(f"get_cw_data({code}) Date/Value/Type 列结构异常")
        records = []
        fields = ("Bonus", "AllotPrice", "ShareBonus", "Allotment")
        for i, item in enumerate(values):
            date_key = "".join(c for c in str(dates[i]).strip() if c.isdigit())[:8]
            try:
                event_date = datetime.strptime(date_key, "%Y%m%d").date() if len(date_key) == 8 else None
            except ValueError:
                event_date = None
            if event_date is None or (start_d and event_date < start_d) or (end_d and event_date > end_d):
                continue  # SDK 同样过滤无效日期及服务端未裁剪的窗口外事件。
            if item is None:
                item = []
            if not isinstance(item, list):
                raise TdxQuantError(f"get_cw_data({code}) Value[{i}] 结构异常")
            padded = item + [0] * max(0, 4 - len(item))
            try:
                amounts = [float(v or 0) for v in padded[:4]]
            except (TypeError, ValueError, OverflowError) as exc:
                raise TdxQuantError(f"get_cw_data({code}) Value[{i}] 含无效数值") from exc
            if not all(math.isfinite(v) for v in amounts):
                raise TdxQuantError(f"get_cw_data({code}) Value[{i}] 含非有限数值")
            records.append({"Date": date_key, "Type": types[i] if i < len(types) else "",
                            **dict(zip(fields, amounts, strict=True))})
        return records
