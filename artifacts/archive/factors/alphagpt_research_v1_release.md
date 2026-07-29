# AlphaGPT Research v1.0.0

发布状态：**完整研究基线**。这不代表生产 alpha 已验证。

## 冻结结论

- 研究闭环完整：是
- 可用于生产交易：否
- 可进入 PPO：否
- 包含前端：否
- P11-D validation Spearman：
  `+0.618`
- P11-D 前瞻 reranker / random：
  `-0.649` /
  `-1.853`
- P11-E validation Spearman：
  `+0.072`

完整的公式生成、训练折评估、候选池、失败审计、rollout、CPU-only 模型和锁定
gate 已经落地。P11-D 前瞻绝对奖励与 P11-E 泛化 gate 均失败，所以本版本不会把
reward model 接入搜索，也不会加入 PPO。

## 统一命令

```powershell
Set-Location backend
# verify or rebuild the release manifest
.\.venv\Scripts\python.exe -m research.alphagpt.run_release_v1
.\.venv\Scripts\python.exe -m research.alphagpt.run_release_v1 --verify-only
.\.venv\Scripts\python.exe -m pytest
```

## 发布产物

| 文件 | 角色 | SHA-256 |
|---|---|---|
| `alphagpt_v1.json` | P10 search loop, lineage, reward audit and frozen HOLDOUT report | `d2ef69fd73c8c86231915c678ad08811b501d8cb4cf865a086798a4bba30cfd7` |
| `alphagpt_rollouts_v1.jsonl` | P11-A original offline token transitions | `7ee2446887136e882fec155cf6c178e24b9735bc5dbe0c9a50cfeb4b7877cb63` |
| `alphagpt_rollouts_v1_manifest.json` | P11-A dataset provenance and split audit | `8d36a2e71e0be721c348f4408c3b9297bb3a37408ef57b2d4d5f9f3253104199` |
| `alphagpt_rollouts_multiseed_v1.jsonl` | P11-C expanded token transitions | `b3082875ce8072a03ade076bcc4f584e6cd607c595341eed0f1191171023b9a6` |
| `alphagpt_rollouts_multiseed_v1_manifest.json` | P11-C expanded dataset provenance and training rewards | `ca0729515249ad603b85364aefee7ee54390f3b7f4419eb9307bb41a618e2ed3` |
| `alphagpt_bc_v1.npz` | P11-B deterministic NumPy behavior-clone checkpoint | `4377ee930c12d0dd2289212e4e992eead65ca2e177238d2f14b500b2dbe93a39` |
| `alphagpt_bc_v1.json` | P11-B training, generation and budget comparison report | `33b3c95b96fddd070b3c3fa61c108abdceb89b10847cd8ae22777f9e184f885a` |
| `alphagpt_bc_stability_v1.json` | P11-C multi-seed behavior stability gate | `b0f1adf73a82ed0f71f840fcb1efe8e5ab9d0d76254b340bc1187b2464f6acf5` |
| `alphagpt_reward_conditioned_stability_v1.json` | P11-C2 reward-weighted and elite behavior-clone gate | `833384cd008d83d7d4fdd12eb27a0c042218dc80bbb0ea5fee6bfdd7875d89fc` |
| `alphagpt_reward_model_v1.npz` | P11-D formula ridge checkpoint | `3a0ee0a6a7c510b942d3ac338cb1b8482b81844ce802997696cb845c313fadce` |
| `alphagpt_reward_model_v1.json` | P11-D locked formula-model validation gate | `7cc14d0176dc34b8811d85eb515a8b352b5de9c6708cc1a79d81b1d6295fb8c9` |
| `alphagpt_reward_reranker_v1.json` | P11-D prospective equal-budget reranker report | `293377965e5c2f9a1ba6d6f51bc542cb3aaba0898074ec8b6ccbd5511b866dda` |
| `alphagpt_reward_labels_v2.json` | P11-E seed-split random-formula labels | `1bb6a51d1026985c088c0e39866280f00c814fe852291e7959bf444c0aa01786` |
| `alphagpt_rank_model_v2.npz` | P11-E pairwise/listwise rank checkpoint | `e15c3548864f6e9c077b0d7f9624ac598fd1e9e54500b553c729fcb028ed0d8b` |
| `alphagpt_rank_model_v2.json` | P11-E locked seed-level validation gate | `845a21f58642d599fc987e8fab50928aaa03f4b79d9f399a93bea6bdf7e4c9bf` |

## 后续优化

后续如继续，另开 P11-F：使用固定训练区间 calibration sketch 提取低成本
execution-aware 特征，并使用全新 validation seed。不要继续扫描 P11-D/P11-E
已经消费过的 validation。
