# Artifacts

研究产物与运行日志统一放在这里，避免仓库根目录被结果文件淹没。

- `current/`：当前权威交付物。固定为三项 regime 事实源和两项 MVP 回测交付物。
- `archive/factors/`：因子研究历史结果。
- `archive/regime/`：旧版 regime 研究结果。
- `archive/optimization/`：样本内优化历史结果。
- `archive/validation/`：区间验证与旧 walk-forward 结果。
- `archive/selection/`：可解释选股历史对照与逐股审计。
- `logs/`：本地运行日志；受 `.gitignore` 的 `*.log` 规则保护，不进入版本库。

脚本不得把新结果直接写到仓库根目录。更新当前结论时，应同步更新
`current/` 中对应领域的白名单产物、`HANDOFF.md` 和相关 skill。

AlphaGPT Research v1.0 属于研究归档，不改变 `current/` 的 regime/MVP 事实源。
其发布入口和人读说明分别为：

- `archive/factors/alphagpt_research_v1_manifest.json`
- `archive/factors/alphagpt_research_v1_release.md`
