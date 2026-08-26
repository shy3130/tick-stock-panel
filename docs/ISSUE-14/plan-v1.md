# 方案 v1

独立 `volume_divergence_convergence_breakout_v1` 研究契约。放量事件以 raw volume/amount 相对前 20 日历史分位定义；3–15 个市场日整理，箱体高低在突破前冻结；分别输出向上/向下突破。整理均量同时相对事件日与整理前 20 日均量；信号仅在收盘确认后生效。sealed generation、窗口完整性、raw 字段缺失即 unavailable/censored；重叠事件按 symbol/区间处理，OOS purge。现有 patterns 仅作参考，不直接复用其可能事后重画的箱体。只读输出证据/删失/覆盖，不把洗盘/出货当事实，不接 short_pool/Agent/交易。先基线、有限调参、OOS；无稳定增量 rejected。