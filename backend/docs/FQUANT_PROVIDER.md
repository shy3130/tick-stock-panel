# FQuantProvider

> FQuant 数据源 Provider — 打通日 K 线链路作为多数据源 PoC。

## 概述

`FQuantProvider` 是 tickflow-stock-panel 的第二个 `MarketDataProvider` 实现（第一个是 `TickFlowProvider`）。它将本地 [fquant](../fquant/) 服务（`localhost:8088`）的 HTTP API 适配为统一的 Provider 接口，验证 Provider 层的多数据源可插拔架构。

### 设计目标

| 目标 | 状态 |
|------|------|
| 实现 `MarketDataProvider` 接口 | ✅ |
| 打通日 K 线链路 (`get_daily`) | ✅ |
| 打通股票列表 (`get_instruments`) | ✅（搜索模式） |
| 符号格式归一（`600519.SH` ↔ fquant `asset_type+code`） | ✅ |
| 离线安全（fquant 不在线返回空 DF，不报错） | ✅ |
| 复权因子 / 分钟线 / 实时行情 | ⏳ PoC 暂未实现，返回空 DF |

## 架构

```
┌─────────────────────────────────────────────┐
│              service 层（未改动）              │
│   DailyKLineService / InstrumentService ...  │
│                  ↓ 调用                       │
│         registry.get_provider(name)          │
│              ↓ dispatch                       │
│    ┌──────────┬──────────────┐               │
│    │ tickflow │   fquant     │  ← 本次新增    │
│    └──────────┴──────────────┘               │
│                     ↓ HTTP                    │
│            localhost:8088 (fquant)           │
└─────────────────────────────────────────────┘
```

## fquant API 映射

### 符号格式

对外统一用带交易所后缀的符号格式，内部映射到 fquant 的 `asset_type` 数字编码：

| 对外符号 | code | 后缀 | fquant asset_type | fquant market |
|---------|------|------|-------------------|---------------|
| `600519.SH` | 600519 | SH | 1 (A股) | `a` |
| `000001.SZ` | 000001 | SZ | 1 (A股) | `a` |
| `00700.HK` | 00700 | HK | 3 (港股) | `hk` |

> **注意**: fquant asset_type 编码：1=A股, 3=港股, 10=指数, 20=ETF。指数和 ETF 暂未打通。

归一工具函数（`fquant_provider.py` 内导出）：

- `split_symbol("600519.SH")` → `("600519", "SH")`
- `symbol_to_market("600519.SH")` → `(1, "a")`
- `code_and_market_to_symbol("600519", 1)` → `"600519.SH"`

### 接口映射

| Provider 方法 | fquant API | 说明 |
|--------------|-----------|------|
| `get_daily` | `GET /api/stocks/{market}/{code}/kline?limit=N` | 日 K 线，不复权 |
| `get_instruments` | `GET /api/metadata/stocks?markets=...&q=...` | 股票搜索（需 `q` 关键词） |
| `get_adj_factors` | — | ⏳ fquant 暂未暴露，返回空 DF |
| `get_minute` | — | ⏳ 待接入 `/api/stocks/{market}/{code}/minute` |
| `get_realtime` | — | ⏳ 待接入 |

### fquant kline 响应格式

```json
[
  {
    "date": "2026-07-01",
    "open": 1180.1,
    "high": 1196.8,
    "low": 1166.33,
    "close": 1193.01,
    "volume": 42474,
    "amount": 5033840128,
    "turnover": 0.34,
    "main_net_inflow": 193626000
  }
]
```

经 `normalize_daily()` 归一后输出列：`symbol, date, open, high, low, close, volume, amount`。

## 配置

环境变量（均有默认值，零配置即可使用）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FQUANT_BASE_URL` | `http://localhost:8088` | fquant 服务地址 |
| `FQUANT_TIMEOUT` | `10` (秒) | HTTP 请求超时 |

## 使用方法

### 通过 registry 获取

```python
from app.data_providers.registry import get_provider

provider = get_provider("fquant")
df = provider.get_daily(["600519.SH"], None, None, "stock")
```

### 直接实例化

```python
from app.data_providers.fquant_provider import FQuantProvider

provider = FQuantProvider()
df = provider.get_daily(["600519.SH", "000001.SZ"], None, None, "stock")
```

## 测试

```bash
cd backend
uv run python scripts/test_fquant_provider.py
```

测试覆盖 8 项：
1. import 无错
2. registry 注册了 fquant
3. 实例化 & capabilities 声明正确
4. 符号归一工具函数
5. get_daily 日 K 线链路（在线返回数据 / 离线返回空 DF）
6. get_instruments
7. 未实现接口返回空 DF
8. 空 symbols 安全性

## 约束遵循

- ✅ **只改 Provider 层**：新增 `fquant_provider.py`、修改 `registry.py`、新增测试脚本
- ✅ **不动 service 层**：service 代码零改动
- ✅ **不动 base.py**：接口已满足，无需修改
- ✅ **不动前端**
- ✅ **不动 git**（未提交）

## 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `backend/app/data_providers/fquant_provider.py` | 新增 | FQuantProvider 实现 |
| `backend/app/data_providers/registry.py` | 修改 | 注册 fquant |
| `backend/scripts/test_fquant_provider.py` | 新增 | 冒烟测试脚本 |
| `backend/docs/FQUANT_PROVIDER.md` | 新增 | 本文档 |

## 后续扩展方向

1. **复权因子**: fquant 暴露 chuquan 接口后实现 `get_adj_factors`
2. **分钟线**: 接入 `GET /api/stocks/{market}/{code}/minute`
3. **指数/ETF**: 扩展 `_SUFFIX_MAP` 支持 `.INDEX` / `.ETF` 后缀
4. **批量拉取**: 当前逐符号请求，可考虑并发或 fquant 批量接口
