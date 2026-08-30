# 数据源插件开发指南

数据源插件是可选的行情数据来源(stock-sdk、akshare 等),作为独立模块放在
`backend/app/plugins/` 下。用户**手动安装依赖**后才可用(开发模式);不安装完全不影响主功能。

> ⚠️ **Docker 默认不打包 stock-sdk**(合规考虑:它抓取第三方财经网站接口,存在版权与反爬风险)。如需在 Docker 中启用,构建时传 `--build-arg INCLUDE_STOCKSDK=1`,使用风险自负。下方"手动安装依赖"适用于开发模式及自定义 Docker 构建。

## 快速上手

一个插件 = 一个目录 + 一个 `plugin.yaml` 清单:

```
backend/app/plugins/<your_plugin>/
├── plugin.yaml          # 清单(必需)
├── provider.py          # Provider 实现(必需)
├── ...                  # 桥接/依赖文件(按需)
```

### plugin.yaml 字段

```yaml
name: my_source                          # 唯一标识, 只允许 [a-z0-9_], 也是 provider name
display_name: "我的数据源"                 # 设置页显示名
runtime: python                          # 运行时类型: node | python | none
entry: app.plugins.my_source.provider:MyProvider   # provider 类的导入路径
check: app.plugins.my_source.bridge:availability   # 可用性检测函数(可选)
datasets: [daily, adj_factor, minute, realtime]     # 支持的数据集
api_key_env: MY_SOURCE_API_KEY           # (可选)声明后设置页提供 Key 输入框
hidden: false                            # (可选)true = 已加载但对设置页隐藏,不注册不展示
description: "数据源描述"
install_hint: "pip install xxx"          # 未装依赖时显示的安装提示
```

#### api_key_env(界面配置 API Key)

声明 `api_key_env` 的插件可以在设置页的数据源卡片中直接填写 Key, 对齐
TickFlow 的「先探后存」语义:

1. entry 模块需提供模块级 `probe_api_key(key) -> (ok, reason)`,
   后端用候选 Key 实探一次, **无效不落盘**
2. 有效则写入 `data/user_data/secrets.json` 的 `{name}_api_key` 字段
   (0600 权限, 优先级高于 `.env` / 环境变量)
3. 保存后自动重载数据源注册表, 插件即刻变为可切换
4. 插件取 Key 用 `secrets_store.get_env_backed_secret("{name}_api_key", api_key_env)`,
   保证 secrets.json 与 .env 两条配置路径一致

### runtime 字段说明

| runtime | 含义 | 典型场景 |
|---|---|---|
| `python` | 纯 Python 依赖, `pip install` | akshare、tushare |
| `node` | 需要 Node.js 运行时, `npm install` | stock-sdk(Docker 默认不打包,见 [deployment.md](./deployment.md)) |

> stock-sdk 在 Docker 中默认不打包(合规考虑);如需启用,构建时传 `--build-arg INCLUDE_STOCKSDK=1`,开发模式下需手动 `npm install`。
| `none` | 无额外依赖 | 纯 HTTP API 源 |

`runtime` 字段当前仅用于 UI 展示, 实际依赖检测由 `check` 函数负责。

### check 函数

插件自己负责检测依赖是否已安装。后端启动时会调用此函数:

```python
# app/plugins/my_source/bridge.py
def availability() -> tuple[bool, str]:
    """返回 (是否可用, 原因)。不抛异常。"""
    try:
        import akshare  # noqa: F401
        return True, "ok"
    except ImportError:
        return False, "未安装 akshare, 运行: pip install akshare"
```

- **可用** → 插件注册进路由表, 设置页可切换
- **不可用** → 设置页显示插件卡片但灰显, 展示 `install_hint`

## Provider 接口契约

Provider 是一个普通 Python 类(无需继承基类), 实现以下方法签名。方法签名对齐
`GenericHTTPProvider`, 这样 services 层(kline_sync / quote_service 等)的路由逻辑
零改动即可路由到插件。

```python
class MyProvider:
    name = "my_source"
    builtin = True  # 标记为内置(不可被用户编辑/删除)

    def __init__(self):
        self.config = MyConfig()  # 需有 .datasets 属性(dict, key 是数据集名)

    def close(self) -> None:
        """清理资源(load_all 重建注册表时会调)。"""

    def get_daily(self, symbols, start_time, end_time, asset_type="stock", on_chunk_done=None) -> pl.DataFrame:
        """日K: 返回 schema [symbol, date, open, high, low, close, volume, amount]"""

    def get_adj_factors(self, symbols, start_time, end_time, asset_type="stock", on_chunk_done=None) -> pl.DataFrame:
        """除权因子: 返回 schema [symbol, trade_date, ex_factor]"""

    def get_minute(self, symbols, start_time, end_time, asset_type="stock", on_chunk_done=None, freq="1m") -> pl.DataFrame:
        """分钟K: 返回 schema [symbol, datetime, open, high, low, close, volume, amount]"""

    def get_realtime(self) -> list[dict]:
        """全市场实时快照: 返回 list[dict], 每行含 symbol/last_price/prev_close/open/high/low/volume"""

    def get_instruments(self, asset_type="stock") -> list[dict]:
        """标的维表(可选): 返回 tickflow Instrument 形状的行, 供 instrument_sync 复用 flatten"""

    def get_auction_series(self, symbols, trade_date) -> list:
        """开盘竞价过程点(可选)。无此能力不要声明 auction_capabilities 含 series, 不要伪造点。"""

    def get_auction_finals(self, symbols, trade_date) -> list:
        """09:25 正式撮合(可选)。单位见 app.auction.contracts: 价格元、量手、涨跌幅小数制。"""

    auction_capabilities = ("series", "finals")  # 过程/撮合可只声明其中一个; 未声明 auction 的源会被跳过

```

### config.datasets 的作用

`provider_has_dataset(name, dataset)` 通过 `dataset in provider.config.datasets` 判断。
这是 services 层路由的关键: 用户在设置页选了插件, 但某数据集未声明时, 该数据集
自动回退 TickFlow。

```python
class MyConfig:
    datasets = {"daily": ..., "realtime": ...}  # key 是数据集名, value 任意
```

## 现有插件参考

- **`backend/app/plugins/fuyao/`** — 同花顺官方 REST 数据源(runtime: none, 纯 HTTP 零依赖)
  - 当前提供 `realtime`(A 股全市场快照, 分页拉取); Key 在设置页卡片直接配置(先探后存), 或 `.env` 配 `FUYAO_API_KEY`
  - `client.py` — httpx 客户端(X-api-key 认证 + 统一信封解包 + 分页)
  - `provider.py` — Provider 实现(字段映射、百分数→小数制单位转换、软失败、Key 探测)
  - 单位口径注意: 扶摇 `price_change_ratio_pct` 为百分数数值(1.74 = +1.74%),
    内部 `change_pct` 契约为小数制, provider 内显式 / 100(见 CONTRIBUTING §3.1)
- **`backend/app/plugins/stocksdk/`** — Node 型插件, 通过 subprocess 桥接调用 stock-sdk
  - `bridge.py` — Python↔Node 桥接 + availability 检测
  - `bridge.mjs` — Node 端(并发池、重试、SDK 解析)
  - `provider.py` — Provider 实现(归一化、分批、错误降级)

- **`backend/app/plugins/tushare/`** — Python 型插件, Tushare Pro SDK
  - 当前只声明 `auction`(stk_auction / stk_auction_o 日级正式撮合); 无 09:15–09:25 过程序列, `get_auction_series` 返回空列表
  - Token 在设置页卡片先探后存(`tushare_api_key`), 或 `.env` 配 `TUSHARE_TOKEN`; 设置页一键安装依赖靠 `requirements.txt`
  - 单位: `vol` 股→手, `amount` 元透传, `stk_auction.price` 只进 vwap, `stk_auction_o.close` 才是 open_price; `open_change_pct` 小数制
  - 未声明 daily/minute/realtime → 自动回退 TickFlow

- **`backend/app/plugins/eltdx/`** — Python 型插件, 通达信 7709 协议(eltdx SDK, Rust Runtime 驱动进程内 TCP 长连接池)
  - `bridge.py` — TdxClient 单例生命周期管理 + 数据获取薄封装, 统一转 list[dict] 供 provider 消费
  - `provider.py` — Provider 实现(代码转换、单位换算、逐日 qfq 因子→事件式 ex_factor 换算、区间裁剪、错误降级)
  - 增量拉取: 传 `since` 时用 `bars.get` 按最新在前分页、覆盖到起点即提前终止, 避免全量分页(精确裁剪由 provider 完成)
  - 批量拉取: 日K/除权因子/分钟线按单标的并发 8 路(对齐 TdxClient 默认 8 连接池), 单个标的失败聚合为一条 WARNING, 不拖垮整批
  - asset_type 分流: 代码前缀白名单区分 stock/etf(指数协议不支持, 直接拒绝); 全市场同步时按资产类型路由
  - ⚠️ 已知边界: 股票日K/分钟/实时走自定义 provider 路由, 但 **ETF 日K批量同步** `index_sync.sync_and_persist_etf_daily` 目前仍直连 TickFlow, 未走 `provider.get_daily(asset_type="etf")`; 故 eltdx 的 ETF 日K分支仅在分钟 intraday 补拉等显式调用路径生效
  - 与 stock-sdk 的 subprocess 桥接不同: 整个后端进程复用同一个 TdxClient, `close()` 统一释放; 探活 `availability()` 用独立临时 client, 不关闭共享单例
  - 合规提示: eltdx 为 Research-Only License(仅限个人学习/协议研究, 禁止商业/生产使用), Docker 默认不打包

## 路由机制(无需关心, 仅参考)

后端启动时, `loader.py` 的 `_load_builtin_plugins()` 扫描 `plugins/` 目录:
1. 读每个子目录的 `plugin.yaml`
2. 调 `check` 函数检测可用性
3. 可用 → 动态 import `entry` 指向的 Provider 类 → 注册进 `_PROVIDERS`
4. 不可用 → 记录状态, 设置页显示但不可切换

注册后, 插件和用户 YAML 自定义源走**完全相同的路由路径**(services 层的
`provider_has_dataset` / `get_provider` 调用), 无需额外集成代码。
