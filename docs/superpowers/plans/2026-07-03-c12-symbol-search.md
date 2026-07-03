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

- [ ] `search_local(repo, query, limit, asset_types=None)`
- [ ] 数据来源：
  - stock instruments
  - index instruments
  - etf instruments
  - hk instruments（若存在）
- [ ] 匹配字段：`symbol/code/name`。
- [ ] 返回：

```json
{"symbol":"600519.SH","code":"600519","name":"贵州茅台","asset_type":"stock","source":"local","matched_by":"code"}
```

- [ ] 排序：精确 code > symbol 前缀 > name 包含 > 其他。

## 任务 2：失败测试

- [ ] code 精确搜索排第一。
- [ ] name 子串可命中。
- [ ] limit 生效。
- [ ] Eastmoney suggest 只在本地结果不足时调用。
- [ ] suggest 返回未知市场时不参与后续数据查询。

## 任务 3：Eastmoney suggest fallback

- [ ] helper：`suggest_symbols(query, limit=10)`。
- [ ] URL host 必须在 eastmoney allowlist；若实测 suggest host 不在 `_ALLOWED_HOSTS`，先追加白名单和测试。
- [ ] `trust_env=False`。
- [ ] 返回项归一：
  - A 股 6 位：按代码规则 `.SH/.SZ/.BJ`
  - 港股 5 位：`.HK`
  - 无法归一：保留 `source=eastmoney_suggest`，但 `asset_type="unknown"`。
- [ ] 本地已有 symbol 时去重，保留 local 版本。

## 任务 4：API

- [ ] `GET /api/search/symbols?q=茅台&limit=20`
- [ ] 若复用现有 kline search endpoint，响应只追加字段，保持前端兼容。
- [ ] `limit` 最大 50。
- [ ] query 为空返回 400 或空列表，测试固定。

## 任务 5：前端接入

- [ ] 现有搜索框无需大改；只消费追加字段显示 asset_type/source。
- [ ] 未知 source 不允许直接进入 kline 详情，需用户确认或禁用。

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
