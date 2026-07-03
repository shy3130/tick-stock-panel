# 财务数据缺口与公开补源记录

最后更新：2026-07-03

## 当前结论

`fquant_local` 当前财务主源是 fstore PostgreSQL 的 `financial_report_*` 表，不来自 TDX 磁盘。A 股三大报表和年度指标已有可用覆盖，但并不完整；`quick`（业绩快报）和 `forecast`（业绩预告）的缺口要拆成两类：

- provider 侧已经支持：`FQuantProvider.get_financial()` 已把 `quick` / `forecast` 映射到 fstore 的 `financial_report_quick` / `financial_report_forecast`。
- 同步侧此前漏列：`financial_sync.FINANCIAL_TABLES` 和 `_PROVIDER_TABLE_MAP` 只覆盖 4 张本地 parquet 表，导致 `quick` / `forecast` 不会被同步到 `data/financials/*`。

当前本地 `data/financials/*/part.parquet` 覆盖情况：

| 表 | 当前行数 | 覆盖标的数 | 备注 |
| --- | ---: | ---: | --- |
| `metrics` | 17,558 | 5,489 | 由 fstore `financial_report_annual` 映射，适合作核心指标缓存 |
| `income` | 22,017 | 5,193 | 利润表可用，但非全市场覆盖 |
| `balance_sheet` | 11,727 | 5,177 | 资产负债表可用，但非全市场覆盖 |
| `cash_flow` | 16,820 | 5,177 | 现金流量表可用，但非全市场覆盖 |
| `quick` | 同步侧曾漏列 | fstore 直查 902 | fstore `financial_report_quick` 直查 993 行；`600519` 为 0 行，不代表无数据源 |
| `forecast` | 已接公开补源 | fstore 直查 0 | fstore `financial_report_forecast` 当前 0 行；同步侧在 fstore 为空时回退东财 `RPT_PUBLIC_OP_NEWPREDICT` |

## 缺口

| 缺口 | 影响 | 当前判断 |
| --- | --- | --- |
| 部分 A 股无三大报表缓存 | 财务页和财务 AI 分析对缺失标的返回空 | 需要补源或扩大 fstore 回填 |
| `quick` 业绩快报本地缓存缺 | 不能展示快报口径 EPS、ROE、营收、净利等 | 先接入现有 fstore 表同步；覆盖不足再补公开源 |
| `forecast` 业绩预告源表空 | 不能展示预告类型、预告净利区间、变动原因 | 已接东财 datacenter-web fallback |
| 港股财务缺 | 港股个股分析缺基本面/财务上下文 | 需要 HKEXnews 或商业/聚合源，成本高 |
| 美股财务缺 | 美股扩展不能做基本面分析 | SEC EDGAR 可作为官方主源 |

## 公开数据来源

| 来源 | 可补数据 | 账号/限制 | 适用性 |
| --- | --- | --- | --- |
| 上交所 / 深交所 / 北交所 XBRL 与公告 | 三大报表、定期报告、部分指标 | 公开披露；批量抓取需处理反爬、格式差异和频率 | A 股最可靠真源 |
| 巨潮资讯 CNINFO | A 股公告、定期报告、业绩预告/快报公告 | 网页公开；接口化和批量服务可能有授权限制 | A 股公告统一入口 |
| 东方财富 datacenter | 财务指标、预告、快报 | 非正式接口，字段和限流可能变化 | `datacenter-web.eastmoney.com/api/data/v1/get` 可复用现有 `eastmoney_client`，不新增重依赖 |
| 东方财富 F10 / NewFinanceAnalysis | A/HK/US 三大报表和指标 | 非正式接口，字段和限流可能变化 | 端点、host 和参数格式不同，需要独立适配器；延后 |
| 新浪 / 同花顺公开页 | 财务指标、三大报表、预告、快报 | 非正式接口，字段和限流可能变化 | 备选补源，不宜作唯一真源 |
| SEC EDGAR | 美股 10-K / 10-Q XBRL | 公开；需合规 User-Agent 和频率控制 | 美股首选 |
| HKEXnews 披露易 | 港股年报、中报、公告 | 公开；结构化弱，多为 PDF/HTML | 港股可做，解析成本高 |

参考入口：

- 上交所 XBRL：https://www.sse.com.cn/services/information/xbrl/ssexbrl/
- 上证所信息网络公告服务：https://www.sseinfo.com/services/other/announcement/
- 巨潮资讯：https://www.cninfo.com.cn/
- 巨潮数据服务：https://webapi.cninfo.com.cn/
- SEC EDGAR：https://www.sec.gov/edgar
- HKEXnews：https://www.hkexnews.hk/

## 本地 `../lib` 数据源核对

`../lib` 下有 `adata` 和 `akshare` 两个公开数据源库，可作为接口字典参考；不建议直接引入为运行时依赖。

| 库 | 可补内容 | 结论 |
| --- | --- | --- |
| `adata` | A 股核心财务指标 | 只实现 `stock.finance.get_core_index()`，底层走东方财富 `RPT_F10_FINANCE_MAINFINADATA`；README 明确“三大报表详细数据，暂时不提供”。可参考字段映射，不适合作主补源。 |
| `akshare` | A 股快报/预告/业绩报表、三大报表，港股/美股三大报表和指标 | 覆盖面最全，但本质是东财/CNINFO/Sina/THS 封装。短期只复用 datacenter-web 类 `reportName` 和字段映射；三大报表/F10 类端点需独立适配器，延后。不引入 AKShare 依赖。 |

已确认可复用的 AKShare 东财接口：

| 目标 | AKShare 函数 | 东财 `reportName` | 实测 |
| --- | --- | --- | --- |
| A 股业绩快报 | `stock_yjkb_em()` | `RPT_FCI_PERFORMANCEE` | `2026-03-31` 返回 22 行 |
| A 股业绩报表 | `stock_yjbb_em()` | `RPT_LICO_FN_CPD` | `2026-03-31` 返回 5,878 行 |
| A 股业绩预告 | `stock_yjyg_em()` | `RPT_PUBLIC_OP_NEWPREDICT` | `2026-06-30` 返回 199 行；`2025-12-31` 返回 7,800 行 |
| A 股三大报表 | `stock_three_report_em.py` | `NewFinanceAnalysis/*AjaxNew` | 单票逐表接口，可作 fstore 缺失兜底，批量成本高 |
| 港股财务 | `stock_financial_hk_report_em()` / `stock_financial_hk_analysis_indicator_em()` | `RPT_HKF10_FN_*` | 可补港股三大报表和主要指标；字段是标准项目码长表，需要单独映射币种/准则 |
| 美股财务 | `stock_financial_us_report_em()` / `stock_financial_us_analysis_indicator_em()` | `RPT_USF10_FN_*` | 可补美股三大报表和主要指标；长期仍建议以 SEC EDGAR 为主源 |

实现注意：

- 当前 `eastmoney_client` allowlist 只含 `datacenter-web.eastmoney.com`、`reportapi.eastmoney.com`、`search-api-web.eastmoney.com`、`searchapi.eastmoney.com`。
- `forecast` / `quick` / 业绩报表的短期 datacenter-web 路径不需要改 allowlist。
- `akshare` 三大报表/F10/港美股接口使用 `datacenter.eastmoney.com`、`emweb.securities.eastmoney.com`、`emweb.eastmoney.com`；采用这些接口前要显式扩 allowlist，并做独立适配器。
- 代码格式没有统一 helper：A 股核心指标用 `600519.SH`，部分 datacenter 报表用裸 `600519` 或 `SECUCODE`，港股用 `00700.HK`，美股用 `TSLA.O`，港股 `REPORT_DATE in (...)` 必须使用单引号。
- 优先接 `datacenter-web.eastmoney.com/api/data/v1/get` 可覆盖 `forecast` 的主要短期缺口，改动最小。

稳定性实测（2026-07-03，`trust_env=False`，每项连续 3 次）：

| 接口 | 结果 | 说明 |
| --- | --- | --- |
| `RPT_F10_FINANCE_MAINFINADATA` | 3/3 成功 | `600519.SH` 年报核心指标每次返回 5 行样本；`SH600519` 返回 0，必须用东财 `code.market` 格式 |
| `RPT_FCI_PERFORMANCEE` | 3/3 成功 | `2026-03-31` 每次返回 5 行样本，`count=22` |
| `RPT_LICO_FN_CPD` | 3/3 成功 | `2026-03-31` 每次返回 5 行样本，`count=5878` |
| `RPT_PUBLIC_OP_NEWPREDICT` | 3/3 成功 | `2026-06-30` 每次返回 5 行样本，`count=199` |
| `RPT_HKF10_FN_MAININDICATOR` | 3/3 成功 | `00700.HK` 每次返回 3 行样本，`count=25` |
| `RPT_USF10_FN_GMAININDICATOR` | 3/3 成功 | `TSLA.O` 每次返回 3 行样本，`count=20` |
| A 股三大报表 `NewFinanceAnalysis/*AjaxNew` | 2/2 成功 | `600519.SH` 资产负债表/利润表/现金流量表均可取，最近 3 个报告期各返回 3 行样本 |
| 港股三大报表 `RPT_HKF10_FN_*` | 3/3 成功 | `00700.HK` 最近 3 个年报：资产负债表 165 行、利润表 81 行、现金流量表 154 行；`REPORT_DATE in (...)` 必须使用单引号 |
| 美股三大报表 `RPT_USF10_FN_*` | 2/2 成功 | `TSLA.O` 最近 3 个 FY：资产负债表 94 行、利润表 89 行、现金流量表 93 行 |

运行时依赖实测：

- backend `uv` 环境不能直接 import `adata` / `akshare`：缺 `requests`。
- 系统 Python 有 `requests/pandas`，但 `adata` / `akshare` 顶层导入仍缺 `py_mini_racer`。
- 因此落地时不引入这两个库，直接复用现有 `eastmoney_client` 调底层 HTTP 源。

AKShare HTTP 服务：

- AKShare 官方文档推荐用 AKTools 暴露 HTTP API：`pip install aktools` 后运行 `python -m aktools`。
- 当前本机未安装 `aktools`。
- 该方式技术上可行，但会引入 AKTools + AKShare + FastAPI/Uvicorn/Typer 及其依赖；本项目只需要少量东财接口，优先直连 `eastmoney_client` 更小、更可控。

## 推荐落地顺序

1. **短期同步修正：复用 fstore**
   - 目标：把 `quick` / `forecast` 纳入 `financial_sync` 同步表和 API 入口。
   - 理由：provider 已有 fstore 映射，`quick` 表实测有数据；这是低成本确定性修复。
   - 状态：同步表枚举、provider 表映射和 `/api/financials/status`/`sync` 入口已补齐，待提交。
   - 风险：`quick` 覆盖有限；`forecast` fstore 源表当前为空，依赖公开 fallback。

2. **短期补源：复用现有 Eastmoney datacenter-web 客户端**
   - 目标：补 `forecast`。
   - 理由：项目已有 `eastmoney_client` 的域名 allowlist、翻页和节流能力，且 `datacenter-web.eastmoney.com` 已在 allowlist；不需要新增依赖或扩域名。
   - 状态：`sync_forecast` 在 fstore 为空时回退 `RPT_PUBLIC_OP_NEWPREDICT`，落 `data/financials/forecast/`，待提交。
   - 风险：非官方接口会变；需要缓存和失败降级。

3. **后续独立适配：三大报表 / F10 / 港美股**
   - 目标：A/HK/US 三大报表和主要指标的公开兜底。
   - 理由：接口已实测可取，但 host、端点、参数格式和字段形态都不同，不能简单复用 `eastmoney_client.get_datacenter_paged()`。
   - 风险：需要扩 allowlist、长表转宽表、币种/准则映射、批量节流和字段对拍。

4. **中期稳固：官方公告 / XBRL**
   - 目标：三大报表、年度/季度指标的官方口径回填。
   - 理由：可靠，适合长期替代 fstore 缺口。
   - 风险：解析成本高，公告/XBRL 字段口径需要对齐。

5. **港股另立：HKEXnews**
   - 目标：港股财务基础数据。
   - 理由：当前港股行情已可用，但财务为实测缺口。
   - 风险：PDF/HTML 解析成本高，币种和披露准则不同。

6. **美股另立：SEC EDGAR**
   - 目标：美股财务主源。
   - 理由：官方公开 XBRL。
   - 风险：符号体系、会计口径、时区和数据管道都需单独设计。

## 最小实现建议

现有 `financial_sync` 主链路已把 `quick` / `forecast` 加入 `FINANCIAL_TABLES` 和 `_PROVIDER_TABLE_MAP`，本地 parquet 可以复用已有 fstore provider 源（待提交）。

- `quick`：先从 fstore `financial_report_quick` 同步；覆盖不足再用 Eastmoney datacenter 补缺。
- `forecast`：fstore `financial_report_forecast` 当前为空，已用 Eastmoney datacenter-web `RPT_PUBLIC_OP_NEWPREDICT` fallback 回填。
- `metrics`：优先继续从 fstore `annual` 补；缺失标的再走公开源。

三大报表暂不从公开网页重抓，除非确认 fstore 覆盖无法继续补齐。
