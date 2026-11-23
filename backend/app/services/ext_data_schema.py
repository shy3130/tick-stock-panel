"""扩展数据创建与拉取配置的共享校验契约。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class FieldDef(BaseModel):
    name: str
    dtype: str = "string"        # string | int | float | bool
    label: str = ""



class CreateExtReq(BaseModel):
    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_]+$")
    label: str = Field(..., min_length=1, max_length=64)
    mode: Literal["snapshot", "timeseries"]
    fields: list[FieldDef] = Field(..., min_length=1)
    description: str = ""
    symbol_map: dict = {}   # {"type": "mapped", "col": "..."} 或 {"type": "computed", "from": "code", "method": "append_exchange"}
    code_map: dict = {}     # {"type": "mapped", "col": "..."} 或 {"type": "computed", "from": "symbol", "method": "strip_exchange"}
    market_level: bool = False  # 市场级表: 行=全市场每日一条, 无 symbol 列 (如择时状态序列)



class PullAuthReq(BaseModel):
    """拉取接口鉴权方式 (与自定义行情源 AuthConfig 同口径)。

    Key 本体存 secrets_store (secrets.json), 不写入 config.json。
    """
    type: Literal["none", "bearer", "header", "query"] = "none"
    header: str = Field("Authorization", min_length=1, max_length=64)  # bearer/header 用
    param: str = Field("token", min_length=1, max_length=64)          # query 用



class PullConfigReq(BaseModel):
    """定时拉取配置请求。"""
    url: str = Field(..., min_length=1)
    method: str = "GET"
    headers: dict[str, str] | None = None
    body: str | None = None
    response_path: str = ""          # dot-path to rows array
    field_map: dict[str, str] | None = None  # external → internal field name
    schedule_minutes: int = Field(1440, ge=1)
    # 间隔秒数 (10~2592000): 配置后优先于 schedule_minutes 生效 (秒级拉取,
    # 如集合竞价 30s 快照); null = 沿用 schedule_minutes (存量配置与旧导出包)
    schedule_seconds: int | None = Field(None, ge=10, le=2_592_000)
    enabled: bool = False
    # 每日固定拉取时刻 "HH:MM" (北京时间): 配置后按墙钟每天该时刻拉一次,
    # 忽略 schedule_minutes; null = 间隔制 (清除固定时刻)
    schedule_time: str | None = Field(
        None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    # 固定时刻的重复周期: daily=每天 | weekly=每周 | monthly=每月 (仅 schedule_time 生效)
    schedule_period: str | None = Field(None, pattern=r"^(daily|weekly|monthly)$")
    # weekly: 1=周一..7=周日; monthly: 1..31 (短月无该日则该月跳过)
    schedule_day: int | None = Field(None, ge=1, le=31)
    # 仅交易日: 跳过周六日 (法定节假日无法离线判定, 仍会按期尝试, 拉取幂等)
    schedule_trading_only: bool = False
    time_window_start: str | None = None   # "HH:MM", None=不限
    time_window_end: str | None = None     # "HH:MM", None=不限
    # 接口按日查询的参数名 (如 "date"): 配置后支持历史回补, 且当日拉取也带日期参数
    date_param: str | None = Field(None, min_length=1, max_length=16, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    # 日期参数值的格式: iso/compact/ts_s/ts_ms (时间戳=该交易日北京时间 00:00:00), 缺省 iso
    date_format: str = "iso"
    # 日内序列表时间列名 (字段映射后的列名, 如 "ts"): 配置后同 symbol 允许多行,
    # 按 [symbol, time_field] 去重; 留空 = 每日快照表 (按 symbol 去重)
    time_field: str | None = Field(None, max_length=32)
    # 增量续传参数名 (如 "since"): 每次拉取自动带上本地目标日分区 time_field 的
    # 最大值, 源只返回其后数据 (盘中多盘表免全量重拉); 需先配置 time_field
    since_param: str | None = Field(None, min_length=1, max_length=16, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    # 序列粒度: auto=按数据形态自动判断 (时间列值含非零时刻→日内, 否则日级;
    # 未配 time_field 恒为日级) | intraday=日内多盘 | daily=日级别。
    # 决定 series 端点口径与列表迷你图形态
    granularity: Literal["auto", "intraday", "daily"] = "auto"
    # 鉴权方式; 请求中缺省 (None) = 保留现有配置, {"type":"none"} = 关闭鉴权
    auth: PullAuthReq | None = None
    # 单次拉取请求超时 (秒), 默认 30 与历史行为一致; 大响应接口可调高
    timeout_seconds: int = Field(30, ge=5, le=300)
    # ── 分页协议 (仅 GET; 页码经 query 参数传递) ──
    # 必须与本类其它字段一样显式声明: Pydantic 默认忽略未声明键, 前端保存时
    # 提交的 page_param/page_size_* 会被静默丢弃, 回读退化成单次请求。
    page_param: str | None = Field(None, max_length=64)
    page_size_param: str | None = Field(None, max_length=64)
    page_size: int = Field(0, ge=0)
    page_start: int = Field(1, ge=0)
    max_pages: int = Field(20, ge=1, le=200)
    # 两级拉取配方 (目录 → 逐条目明细); 缺省 (None) = 保留现有配置
    fanout: dict | None = None
