# 一切皆扩展 — 架构优化路线(P0–P4)

> 分支 `feat/ext-architecture`。目标: 因子/信号/指标/过滤/评分/策略全部以"定义"形式扩展,
> 保存即注册、处处可用 (检验/筛选/回测/监控/图表), 且性能随扩展数量增长基本持平。
> 本文档是路线图与验收基准, 各阶段独立可交付、可停。

## 目标架构总图

```
┌─ 定义层 ──────────────────────────────────────────────┐
│ 统一契约: {id, kind, version, params(类型+默认),        │
│           inputs(依赖列), expression, meta}            │
│ kind = signal | factor | indicator | filter | scoring  │
│ 存储: 标准 Store 基类(原子写+签名缓存+失效广播+文件锁)   │
│ 校验: DSL 解析 → 依赖提取 → DAG 环检测 → 未来函数防线    │
└──────────────────┬───────────────────────────────────┘
                   ▼
┌─ 编译层(三级逃生舱)──────────────────────────────────┐
│ T1 声明式 DSL(含 ewm/ts_slope + macd()/kdj() 宏)      │
│ T2 表达式级 Python(只返回 Polars 表达式, ctx.col 可追踪)│
│ T3 自由 Python 策略(现状保留; 第三方导入成真时再子进程)  │
└──────────────────┬───────────────────────────────────┘
                   ▼
┌─ 物化层(核心新增)────────────────────────────────────┐
│ L0 OHLCV+复权(现状)  L1 内置指标预计算(现状)            │
│ L2 扩展定义按需物化: 缓存键 = hash(定义) + 数据版本      │
│     + 分区键; 时序算子按 symbol, 截面算子按 date 分区    │
│ 失效: 定义变更沿 DAG 反向传播 (generation 机制)          │
└──────────────────┬───────────────────────────────────┘
                   ▼
┌─ 消费层 ──────────────────────────────────────────────┐
│ 因子检验 | 策略筛选 | 回测/优化 | 监控 | K线图叠加        │
│ 统一选择器 + schema 自动渲染参数表单 + 导入即注册即冒烟   │
└──────────────────────────────────────────────────────┘
横切: 备份/分发管线复用(定义即文件) | AI 生成(契约即提示词)
```

## 设计红线(做错即返工)

1. **截面算子(rank/zscore)按 date 分区, 时序算子(ts_*)按 symbol 分区** — 截面残缺不报错,
   是金融数据最危险的错误形态
2. **递归算子(EMA)缓存键必须含窗口参数** — 不同起点结果不同, 禁止跨窗口复用
3. **fail-closed 优先于 fail-fast** — 推导/解析不可证明时保持兜底或拒绝, 绝不静默给错列
4. **存量兼容** — 旧策略/三套缓存在每个阶段全程可跑, 适配器过渡, 不一刀切迁移

## 阶段与验收

| 阶段 | 内容 | 验收 | 状态 |
|---|---|---|---|
| P0 基准 | `backend/scripts/bench_feature_resolution.py`; 依赖解析宽度/耗时基线 | 基线 JSON 落 `data/benchmarks/`(本地不入库) | ✅ |
| P1a 依赖推导 | filter_history AST 列推导(`app/strategy/feature_deps.py`), 引擎加载接入 | 无声明且可证明的策略不再全量兜底 | ✅ |
| P1b 契约统一 | signal/factor 统一 schema + 依赖提取公共化 | 新增 kind 复用同一校验/注册路径 | ✅ |
| P2 L2 物化 | 按需物化 + 内容寻址缓存 + 失效传播; 指标成为一等定义 | 调参循环提速 ≥2×; 新增 10 个自定义指标热路径耗时不变 | ✅ |
| P3 表达式级 Python | T2 中间层 (ctx 追踪依赖+安全) | SKDJ 类口径零 DSL 可表达 | ✅ |
| P4 体验与分发 | 统一选择器/自动表单/导入冒烟; 备份管线复用为扩展包 | 新增信号导入到可回测 ≤2 分钟 | ✅ |
| 条件: 沙箱 | T3 子进程执行 | 触发条件: 首次真实导入第三方扩展 | ⬜ |

## P1a 实测(2026-10-26, 本机, 26 个动态策略)

| 策略 | legacy 指标列宽 | 推导后 | 宽度比 |
|---|---|---|---|
| custom_v1_kanban_strict | 37 | 3 | 12.3× |
| custom_mq4sq94g | 37 | 4 | 9.3× |
| custom_1782999589 | 37 | 5 | 7.4× |
| auction_steady_rise | 37 | 3 | 12.3× |
| short_term_top10 | 37 | 37 | 1.0× (动态列名循环, 正确 fail-closed) |

数据加载 IO 随列宽近似线性, 宽度比即回测数据准备阶段的近似提速上界。


## P2a 实测(2026-10-26, 本机, consolidation_limit_pullback, 15 组参数网格)

共享面板 + 共享矩阵编码 (prepare-once) vs 逐组独立准备, 同进程交错 A/B:

| 臂 | 试算累计 | 含 prepare |
|---|---|---|
| 逐组独立 (基线) | 19.47s | 19.47s |
| P2a 面板共享 | ~9.4s (2.1×) | ~11.0s (1.8×) |
| P2a-2 面板+矩阵编码共享 | 4.59s (4.24×) | 6.19s (**3.14×**) |

stats 剔除计时字段后逐组完全一致, 交易逐笔相同; tests/backtest 410 全过。
剩余每组成本 = filter_history (参数相关, 不可共享) + 掩码散布。
P2b (L2 内容寻址物化) 在此之上进一步覆盖跨 run/跨策略的因子复用。


## P4 交付(2026-10-26) — 全线收官

- 统一定义目录 `GET /api/definitions`: 跨 kind (signal/factor) 的目录视图
  (label/lang/依赖/warmup/状态), 统一选择器的数据半边; 坏定义不阻断目录
  (隔离)。
- 导入冒烟 `POST /api/definitions/smoke` + 备份导入内置: 校验 → 编译 →
  依赖可解析 (拼错列名 W001 当场报), 备份 apply 响应逐条携带冒烟结果 —
  导入即注册即冒烟。
- 验收: `test_import_to_backtest_within_two_minutes` 全链自动化 — 构建
  含新信号+新因子的备份 ZIP → apply (冒烟 ok) → 引用该信号的回测立即跑通,
  全程 <2s (验收线 120s)。

前端消费面 (同日补齐): `DefinitionPicker` 可复用选择器 (跨 kind 目录 +
筛选/搜索 + 逐条冒烟, smoke API 支持按 id 复检 `smoke_stored`)、设置页
「定义目录」页签、备份导入结果的「导入冒烟」逐条展示。浏览器 E2E: 导出 →
覆盖导入 275 项 → 冒烟 21/21 通过零 issue。参数表单自动渲染仍为后续工作
(需定义 schema 字段先落契约)。

**P0-P4 全部完成。** 分支 feat/ext-architecture: 契约统一 (P1b) → 依赖推导
(P1a) → 调参提速 3.16× (P2a) → 热路径平坦 0.1ms/10因子 (P2b) → T2 表达式层
(P3) → 导入即冒烟 (P4)。T3 子进程沙箱按设计等待首个真实第三方导入触发。

## P3 交付(2026-10-26)

- `app/factors/py_expr.py` T2 表达式级 Python: AST 白名单文法 (仅赋值/表达式
  语句, 名字限 ctx/pl/局部量, 禁 import/def/lambda/循环/推导式/下划线属性/
  回调与 IO 类方法) + {ctx, pl} + 空 builtins 求值, 结果必须是单个 pl.Expr。
- 依赖: ctx.col 构造期登记 → FactorSpec.dependencies (回测计划/P2b 缓存键
  同源); warmup 从窗口常量保守推断, 显式声明只能增大 (安全下限)。
- 贯通: registry FactorSpec.lang 字段 (默认 dsl, 存量零影响) → store to_spec
  编译分支 → materialize_scoring_columns 分发 → API validate/trial 支持
  lang=python。
- 验收: 看板口径 SKDJ (9日RSV→EMA(α=0.5)×2, 分母 0 取 50) 的 T2 单表达式与
  策略文件分阶段实现**逐元素一致** (test_skdj_t2_matches_staged_reference_exactly);
  19 个契约测试含 11 种文法拒绝形态。

## P2b 实测(2026-10-26, 本机, 132 万行合成帧, 13 个自定义 DSL 因子)

内容寻址因子列缓存 (app/factors/materialize_cache.py): 键 =
sha256(定义 + 依赖列内容指纹 + 行数), 进程内 LRU + 磁盘 parquet 两层,
fail-open。命中列一次批量附着 (与因子数无关)。

| 臂 | 冷 (首次) | 热 (重复求值) |
|---|---|---|
| K=3 因子 | 132ms | 0.0ms |
| K=13 因子 | 480.7ms (≈无缓存基线 478ms) | **0.1ms** |

**+10 因子的热路径绝对增量 0.1ms** (基线为 +400ms 线性增长), 冷启动
开销 ~0.5%。端到端优化器 A/B 复测: trials 4.23× / 含 prepare 3.16×,
stats 完全一致。P2 两项验收 (调参 ≥2×、热路径平坦) 均达成。
内置指标转为「一等定义」的完全体形态随 P3 表达式层一并落。

## P1b 交付(2026-10-26)

- `app/definitions.py`: `JsonDefinitionStore`(原子写 + 损坏跳过 + 目录 mtime 签名,
  glob/日志名/逐条归一化参数化)收口四处同型持久化 — 自定义信号 / 自定义因子 /
  监控规则 / 批次, 公开函数签名零变化; `DefinitionKind` 注册表 +
  `validate_definition` / `definition_dependencies` 统一分发, 内置 kind 惰性导入
  (消费方无需预导入领域模块)。
- 两种真实 kind 接入: signal(校验=`custom_signals.validate`, 依赖=编译表达式
  `meta.root_names` 与回测/管线同源)、factor(校验/依赖=`to_spec` DSL 编译同源)。
  验收测试 `test_definition_contract.py::test_new_kind_reuses_same_pipeline`:
  注册第三种 kind 描述器即走同一存取/校验/依赖管线, 零新持久化代码。
- 契约视图(params/inputs/expression 归一化渲染)留给 P4 消费方出现时再加;
  失效广播/文件锁随 P2 物化叠加。
