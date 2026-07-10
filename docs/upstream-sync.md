# Sycee 上游同步

Sycee 的 `main` 保留原项目 `shy3130/tickflow-stock-panel` 的 Git 提交血缘。上游新增功能时，应通过独立同步分支合并，验证后再用普通 merge commit 进入 `main`。

## 远端职责

| 远端 | 地址 | 用途 |
| --- | --- | --- |
| `origin` | `g35756666/tickflow-stock-panel` | Sycee fork，可推送 |
| `upstream` | `shy3130/tickflow-stock-panel` | 原作者仓库，只拉取 |
| `legacy` | 本机旧目录 | 历史参考，只读；目录切换完成后可移除该 remote |

`upstream` 的 push URL 应保持为 `no_push`，避免误推原作者仓库。

## 自动同步

`.github/workflows/upstream-sync.yml` 每 6 小时运行一次，也支持在 GitHub Actions 页面手动执行。

工作流按以下顺序运行：

1. 比较 `origin/main` 和 `shy3130/main`；上游提交已包含时直接结束。
2. 创建 `codex/sync-upstream-<上游 SHA>-onto-<main SHA>` 分支，让每个已验证的 base/upstream 组合都有唯一身份。
3. 在只有只读权限的 job 中准备合并树，运行后端完整测试，以及前端行为测试、lint 和生产构建。
4. 发布 job 重新生成相同的 Git tree，并核对 base、upstream、tree 三个 SHA。
5. 校验一致后才生成双亲 merge commit、推送同步分支并创建 PR。

工作流不会直接修改 `main`。合并冲突、测试失败或构建失败都会让运行失败，不会推送半成品分支。

测试 job 不持有仓库写凭据；持有写权限的发布 job 不安装依赖，也不执行任何上游项目代码。这一隔离避免尚未审阅的上游脚本取得 fork 写权限。第三方 Actions 均固定到完整 commit SHA。

首次启用时，仓库的 **Settings -> Actions -> General -> Workflow permissions** 必须允许 GitHub Actions 创建 Pull Request。工作流本身只申请 `contents: write` 和 `pull-requests: write`。

公开 fork 的定时工作流可能默认处于禁用状态，或在仓库长期无活动后被 GitHub 暂停。合并此工作流后应在 Actions 页面手动启用并运行一次；`workflow_dispatch` 始终作为手动检查入口。

如果匹配的同步 PR 被关闭，或曾用 squash/rebase 合并而没有保留上游祖先关系，工作流会失败并要求人工处理，不会静默跳过。确认分支仍有效时可重新打开原 PR；否则按下文创建新的人工同步分支。

同一时间只允许一个自动同步 PR 保持开放。如果 `main` 或上游在审阅期间继续变化，先处理现有同步 PR，再重新运行工作流；自动化不会创建多个互相竞争的同步 PR，也不会擅自关闭或覆盖已有分支。

## 审阅和合并 PR

先确认 PR 的 base 是 `main`，compare 是 `codex/sync-upstream-*`，再检查测试结果和功能差异。合并方式必须选择 **Create a merge commit**：

```bash
gh pr view <PR编号> --repo g35756666/tickflow-stock-panel
gh pr merge <PR编号> --repo g35756666/tickflow-stock-panel --merge
```

不要使用 squash 或 rebase。它们会丢失用于判断“哪些上游提交已经接入”的父提交关系，使后续同步产生重复改动。

## 冲突时的本地处理

自动工作流遇到冲突会停止。此时从最新的 Sycee `main` 创建人工同步分支：

```bash
git fetch origin
git fetch upstream
git switch main
git merge --ff-only origin/main
git switch -c codex/sync-upstream-YYYYMMDD
git merge --no-ff upstream/main
```

逐个解决冲突后，运行与自动工作流相同的验证：

```bash
bash tests/start-command-auto-open-test.sh

cd backend
uv run --frozen --extra dev pytest -q

cd ../frontend
pnpm install --frozen-lockfile
node --test src/lib/publicEntry.test.ts src/lib/navRegistry.test.ts
pnpm lint
pnpm build
```

验证完成后提交、推送并创建 PR。冲突解决应优先保留上游模块边界和公共数据接口，再把 Sycee 的品牌、导航、公开入口和产品交互接回这些接口，避免复制上游模块形成长期分叉。

## 数据安全

上游同步只处理 Git 跟踪的源码。`data/`、`.env`、虚拟环境和依赖缓存均被忽略，不应出现在同步提交或 PR 中。同步前后仍应保留独立的数据备份。
