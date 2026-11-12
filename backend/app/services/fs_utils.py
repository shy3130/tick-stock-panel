"""文件系统小工具 — 原子写等。

历史遗留: json_report_store / strategy_cache / kline_sync 等模块里各有一份内联的
同款原子写。新代码统一用本模块的 atomic_write_text, 一处实现一处维护。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只做类型标注; JSON 原子写的调用方 (preferences/secrets) 不必加载 polars
    import polars as pl


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """临时文件 + os.replace 原子替换, 避免读侧读到半截 JSON。

    `mode` 在替换*之前*打到临时文件上。凭证类文件如果先落盘再 chmod, 中间有一段
    以默认权限存在的窗口; 先改临时文件就没有这个窗口。Windows 上 chmod 只影响
    只读位, 失败不该让写入失败, 所以吞掉 OSError, 与原调用处的处理一致。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    # newline="" 关闭换行转换: 默认 newline=None 会在 Windows 上把 \n 写成 \r\n,
    # 若传入文本本身已含 \r\n (例如从 zip 取出的 Windows 原作者文件), 就会变成
    # \r\r\n —— 备份→还原一轮多两个空行, 再还原又翻倍。配置文件与策略源码应当
    # 按字节原样落盘, 行尾由内容自己决定, 不由运行平台决定。
    tmp.write_text(text, encoding="utf-8", newline="")
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:  # noqa: R002 -- chmod 失败不该让写入失败, 理由见上方 docstring
            pass
    os.replace(tmp, path)


def atomic_write_parquet(df: pl.DataFrame, path: Path) -> None:
    """parquet 版原子写: 先写 `<name>.tmp` 再替换, 与 repository / kline_sync 的
    `_atomic_write_parquet` 同语义。

    直接 `df.write_parquet(path)` 在进程被 kill (dev.sh 清端口用 kill -9)、断电或
    磁盘写满时会留下半截文件, 之后读侧 `read_parquet` / `scan_parquet` 整条报错。
    `.tmp` 后缀不匹配 `*.parquet` glob, 不会被视图误读。Windows 下目标正被并发读取时
    由 `replace_with_retry` 短退避穿过。
    """
    from app.tickflow.repository import replace_with_retry  # 惰性导入, 避免模块级环

    tmp = path.with_name(path.name + ".tmp")
    df.write_parquet(tmp)
    replace_with_retry(tmp, path)
