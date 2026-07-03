# C12：Symbol Search 增强实现计划

> **面向 AI 代理的工作者：** 本地 instruments 是事实源，Eastmoney suggest 只做补全，不覆盖本地。

**目标：** 改善搜索框体验：代码/名称/拼音模糊搜索优先查本地 instruments；不足时用 Eastmoney suggest 补全，再归一成 panel symbol。

**现状证据：**
- provider/repository 已有 stock/index/etf/hk instruments。
- ext_presets/C3 已引入 `eastmoney_client.py` host allowlist、`trust_env=False` 和节流；C12 需确认 suggest host 已在 `_ALLOWED_HOSTS`。
- 外部 screener 与本地策略数据口径不一致，本计划不迁移。

**范围：** 搜索增强 API；不改策略选股。

## 文件

| 文件 | 动作 |
|---|---|
| `backend/app/services/symbol_search.py` | 创建 |
| `backend/app/services/eastmoney_client.py` | 复用/扩展 suggest helper |
| `backend/app/api/kline.py` 或 `backend/app/api/search.py` | 接入 endpoint |
| `backend/tests/services/test_symbol_search.py` | 创建 |
| `backend/tests/api/test_symbol_search.py` | 创建 |

## 任务 1：本地搜索

- [x] `search_symbols(repo, query, limit)` 本地优先；内部 `_search_local()` 合并本地 instruments。
- [x] 数据来源：
  - stock instruments
  - index instruments
  - etf instruments
  - hk instruments（若 repository 后续提供，可按同一模式追加；当前 B3 只落 HK 日 K，不提供 HK instruments universe）
- [x] 匹配字段：`symbol/code/name/name_pinyin/name_initials`。
- [x] 返回：

```json
{"symbol":"600519.SH","code":"600519","name":"贵州茅台","asset_type":"stock","source":"local","matched_by":"code"}
```

- [x] 排序：精确 code > symbol 前缀 > code 前缀 > code/symbol 包含 > name 包含 > 全拼前缀 > 全拼包含 > 首字母前缀。
- [x] 拼音列：`instrument_sync` 写 `name_pinyin/name_initials`；旧 parquet 缺列时 `symbol_search` 兼容计算。
- [x] 名称归一：转拼音前做 NFKC + 去空白，覆盖 `万 科Ａ -> wanke/wk`。

## 任务 2：失败测试

- [x] code 精确搜索排第一。
- [x] name 子串可命中。
- [x] limit 生效（API Query 上限 50，service 内部 clamp）。
- [x] Eastmoney suggest 只在本地结果不足时调用。
- [x] suggest 返回未知市场时不参与后续数据查询。
- [x] 全拼搜索：`guizhoumaotai -> 贵州茅台`。
- [x] 首字母搜索：`gzmt -> 贵州茅台`。
- [x] 全角/空格名称归一：`万 科Ａ -> wanke/wk`。

## 任务 3：Eastmoney suggest fallback

- [x] helper：`suggest_symbols(query, limit=10)`。
- [x] URL host 必须在 eastmoney allowlist；`searchapi.eastmoney.com` 已由 `eastmoney_client` 校验。
- [x] `trust_env=False`。
- [x] 返回项归一：
  - A 股 6 位：按代码规则 `.SH/.SZ/.BJ`
  - 港股 5 位：`.HK`
  - 无法归一：保留 `source=eastmoney_suggest`，但 `asset_type="unknown"`。
- [x] 本地已有 symbol 时去重，保留 local 版本。

## 任务 4：API

- [x] 复用现有 `GET /api/kline/instruments/search?q=茅台&limit=20`。
- [x] 响应只追加 `asset_type/source/matched_by` 字段，保持前端兼容。
- [x] `limit` 最大 50。
- [x] query 为空返回空列表。

## 任务 5：前端接入

- [x] 现有搜索框无需大改；`instrumentSearch` 类型扩展后，Watchlist/Backtest/RuleEditor/Financial search 下拉显示 asset_type/source/matched_by。
- [x] 选择行为不变；未知 source 只作为补全展示，不新增绕过后端数据校验的直连路径。

## 验证

```bash
cd backend
uv run --extra dev pytest tests/services/test_symbol_search.py tests/api/test_symbol_search.py -q
```

## 非目标

- 不迁移外部条件选股 API。
- 不用 suggest 结果覆盖本地 instruments。
- 不新增复杂前端筛选器。
- 不把搜索 fallback 当行情数据源。
