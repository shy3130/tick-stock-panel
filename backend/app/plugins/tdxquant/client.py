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
import os
import shutil
import sys
import time
from datetime import date
from pathlib import Path

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

_DLL_BY_PLATFORM = {"win32": "TdxAiData.dll", "darwin": "libTdxAiData.dylib"}
LIB_DLL_NAME = _DLL_BY_PLATFORM.get(sys.platform, "libTdxAiData.so")
LIB_ENV = "TDX_AI_DATA_LIB"  # tdxaidata 读取: 指向 DLL 文件路径


class TdxQuantError(RuntimeError):
    """tdxaidata 调用失败(网络/参数/协议)。"""


def package_lib_dir() -> Path:
    """tdxaidata 包自带的 lib 目录(平台 DLL + 运行时 ini)。"""
    import tdxaidata

    return Path(tdxaidata.__file__).resolve().parent / "lib"


def write_token_ini(ini: Path, api_key: str) -> None:
    """把 [Token] token=<key> 写入 ini, 保留其余配置行。"""
    lines = (
        ini.read_text(encoding="utf-8", errors="replace").splitlines()
        if ini.exists()
        else ["[Token]"]
    )
    out: list[str] = []
    in_token = False
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower() == "[token]":
            in_token = True
            out.append(line)
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_token = False
        if in_token and "=" in line and stripped.lower().split("=", 1)[0].strip() == "token":
            if not replaced:
                out.append(f"token={api_key}")
                replaced = True
            continue
        out.append(line)
    if "[Token]" not in {line.strip() for line in out}:
        out.extend(["[Token]", f"token={api_key}"])
    elif not replaced:
        idx = next(i for i, line in enumerate(out) if line.strip().lower() == "[token]")
        out.insert(idx + 1, f"token={api_key}")
    ini.write_text("\n".join(out) + "\n", encoding="utf-8")


def materialize_lib(api_key: str, lib_root: Path, source: Path | None = None) -> Path:
    """复制包内 lib 到私有目录并写入 token, 返回 DLL 路径。

    marker 记 token 指纹 + DLL mtime/size, 未变化时直接复用(每轮 realtime
    轮询都会走到这里, 不能反复复制几十 MB 的 DLL)。
    """
    src = source or package_lib_dir()
    dll = src / LIB_DLL_NAME
    if not dll.exists():
        raise TdxQuantError(f"tdxaidata lib 缺少 {LIB_DLL_NAME}: {src}")
    lib_root.mkdir(parents=True, exist_ok=True)
    marker = lib_root / ".tdxquant_lib_marker"
    stat = dll.stat()
    want = f"{hashlib.sha256(api_key.encode()).hexdigest()}:{stat.st_mtime_ns}:{stat.st_size}"
    copied = lib_root / LIB_DLL_NAME
    if marker.exists() and marker.read_text(encoding="utf-8") == want and copied.exists():
        return copied
    for item in src.iterdir():
        if item.is_file():
            shutil.copy2(item, lib_root / item.name)
    for ini_name in ("TdxAiData.ini", "user.ini"):
        write_token_ini(lib_root / ini_name, api_key)
    marker.write_text(want, encoding="utf-8")
    return copied


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
        self._universe: list[str] = []
        self._universe_at = 0.0
        self._warmup()

    def _warmup(self, attempts: int = 5) -> None:
        """DLL 冷启动后有一段"未就绪"窗口, 业务调用持续失败(实测首批整批丢弃)。

        用最小快照轮询到就绪; 一直失败不抛 — 后续调用可能自愈, 只留告警。
        """
        for _ in range(attempts):
            try:
                rows = self._tqs.get_market_snapshot_batch(
                    stock_list=["600519.SH"], field_list=["Now"]
                )
                if isinstance(rows, dict) and rows:
                    return
            except Exception:  # noqa: BLE001 — 就绪探测, 任何异常都重试
                pass
            time.sleep(1.2)
        logger.warning("TdxQuant 原生层预热未成功(%d 次), 首批业务调用可能失败重试", attempts)

    def close(self) -> None:  # 原生 DLL 常驻进程, 无可回收句柄; 保留接口对齐 loader
        self._universe.clear()

    # ---- 原生调用(重试包装) ----
    def _raw(self, method: str, *args):
        """直接调原生层并 _clean 解包; 异常重试 3 次、空返回补试 1 次。

        空闲后首连 "Invalid server response" 实测重试即恢复; 坏代码等
        永久错误重试也只会快失败(返回 None), 不至于长阻塞。
        """
        last: Exception = TdxQuantError(f"{method} 未知失败")
        for attempt in range(_RETRIES):
            try:
                out = self._tqs._clean(getattr(self._native_cls.get_instance(), method)(*args))
                if out is not None:
                    return out
                last = TdxQuantError(f"{method} 返回空数据(第 {attempt + 1} 次)")
                if attempt + 1 >= _NONE_RETRIES:
                    break
            except Exception as exc:  # noqa: BLE001 — 原生 ctypes 层异常类型不可枚举
                last = TdxQuantError(f"{method}: {exc}")
            time.sleep(_RETRY_SLEEP_S)
        raise last

    # ---- 股票池 ----
    def stock_universe(self) -> list[str]:
        """五市场全 A 代码(带交易所后缀)。12h 缓存, 避免每轮快照重复枚举。"""
        now = time.time()
        if self._universe and now - self._universe_at < _UNIVERSE_TTL_S:
            return list(self._universe)
        last: TdxQuantError = TdxQuantError("股票列表为空(五个市场均无返回)")
        for _attempt in range(_RETRIES):
            out: list[str] = []
            try:
                for market in _MARKETS:
                    lst = self._tqs.get_stock_list(market=market, list_type=0)
                    out.extend(code for code in (lst or []) if isinstance(code, str) and code)
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
        """批量快照 → {code: row(字段见 SNAPSHOT_FIELDS)}。SDK 内部已做批处理。

        空闲后首连易 "Invalid server response"(实测重试即恢复), 批级失败必须
        重试 — provider 只做批间隔离, 这里不重试会整批丢标的。
        """
        if not codes:
            return {}
        last: TdxQuantError = TdxQuantError(f"snapshot_batch({len(codes)} 只): 未知失败")
        for _attempt in range(_RETRIES):
            try:
                rows = self._tqs.get_market_snapshot_batch(
                    stock_list=list(codes), field_list=list(SNAPSHOT_FIELDS)
                )
            except Exception as exc:  # noqa: BLE001 — SDK 内部异常类型不可枚举
                last = TdxQuantError(f"snapshot_batch({len(codes)} 只): {exc}")
                time.sleep(_RETRY_SLEEP_S)
                continue
            if isinstance(rows, dict) and rows:
                return rows
            last = TdxQuantError(f"snapshot_batch({len(codes)} 只): 空返回")
            time.sleep(_RETRY_SLEEP_S)
        raise last

    # ---- K 线(count 模式, 原始列) ----
    def his_dats(self, code: str, end_d: date, count: int, period: str = "1d") -> dict:
        """单标的 K 线原生 dict: Date 'YYYYMMDD' / Open / High / Low / Close /
        Volume(股) / Amount(万元) 等。count 模式下 start 恒空(服务端只用 end)。
        """
        if count <= 0:
            return {}
        end = f"{end_d:%Y-%m-%d} 00:00:00"
        out = self._raw("get_his_dats", code, "", end, period, "none", int(count))
        return out if isinstance(out, dict) else {}

    # ---- 除权事件 ----
    def divid_factors(
        self, code: str, start_d: date | None = None, end_d: date | None = None
    ) -> list[dict]:
        """单标的除权事件, 每行 {Date 'YYYYMMDD', Type, Bonus, ShareBonus,
        AllotPrice, Allotment} — 金额/比例均为**每 10 股**口径(Bonus=10派X元)。
        """
        start = f"{start_d:%Y-%m-%d}" if start_d else ""
        end = f"{end_d:%Y-%m-%d}" if end_d else ""
        try:
            out = self._tqs.get_divid_factors(code, start, end)
        except Exception as exc:  # SDK 内部异常类型不可枚举
            raise TdxQuantError(f"get_divid_factors({code}): {exc}") from exc
        if isinstance(out, list):
            return out
        # 环境装有 pandas 时 SDK 返回 DataFrame, 归一为 list[dict]。
        # 注意 Date 会成为 DataFrame 索引(同 get_market_data 的日期索引惯例),
        # reset_index 后若列名漂移, 把首列恢复为 'Date'。
        to_dict = getattr(out, "to_dict", None)
        reset_index = getattr(out, "reset_index", None)
        if callable(to_dict) and callable(reset_index):
            try:
                records = reset_index().to_dict("records")
                if isinstance(records, list) and records:
                    if "Date" not in records[0]:
                        first_key = next(iter(records[0]))
                        if first_key not in ("Type", "Bonus", "AllotPrice", "ShareBonus", "Allotment"):
                            for row in records:
                                row["Date"] = row.pop(first_key)
                    return records
            except Exception:  # noqa: BLE001 — 转换失败按结构异常处理
                pass
        raise TdxQuantError(f"get_divid_factors({code}) 返回异常结构: {type(out)!r}")
