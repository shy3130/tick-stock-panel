# 可验证备份与隔离恢复

## 自动任务

任务计划程序中的 `TickFlow Verified Backup`：

- 周一至周五 19:00；
- 周日 03:00；
- `MultipleInstances=IgnoreNew`，最长运行 2 小时；
- 电脑错过执行时间后尽快补跑，并允许唤醒；
- 运行身份是当前 Windows 用户，不保存额外密码。

手动重新注册：

```powershell
& 'D:\A股-v2\scripts\register-tickflow-backup.ps1'
```

## 一致性和隐私

备份程序先取得 `D:\A股-v2-backups\.backup-run.lock`，记录容器原状态。如果生产
容器正在运行，则先停止容器，再复制 bind mount 数据；无论成功或失败都会尝试恢复
原运行状态并等待 health 为 `healthy`。原本停止的容器不会被擅自启动。

以下内容永不进入新快照：`auth.json*`、`secrets.json*`、`.env*`、私钥/证书、
凭据特征文件名、带非空密码/token/API key 赋值的小型文本配置、`*.lock`、`*.tmp`、
`*.partial`。项目根 `.env` 也不在备份范围。

每个成功快照以 `tickflow-*.complete` 命名，并包含：

- `manifest.sha256`：逐文件路径、大小、SHA-256；
- `metadata.json`：来源、排除清单、容器状态与配置哈希；
- `COMPLETE.json`：manifest 与 metadata 的哈希和统计。

`.staging`、缺少完成标记或校验失败的目录绝不作为可恢复备份，也不参与自动删除。
保留策略为 14 个日快照、8 个周快照、12 个“每月首个成功周日”快照。只有新快照
完成并通过校验后才轮换旧的 verified 快照；永久 `pre-docker-*` 回滚点不参与轮换。

## 验证和恢复演练

校验快照：

```powershell
& 'D:\A股-v2\backend\.venv\Scripts\python.exe' `
  'D:\A股-v2\backend\scripts\tickflow_backup.py' verify `
  'D:\A股-v2-backups\tickflow-YYYYMMDD-HHMMSS-xxxxxxxx.complete'
```

隔离恢复并在另一个端口启动临时实例：

```powershell
& 'D:\A股-v2\scripts\verify-tickflow-restore.ps1' `
  -Snapshot 'D:\A股-v2-backups\tickflow-YYYYMMDD-HHMMSS-xxxxxxxx.complete'
```

脚本先逐文件验 hash，再复制到 `D:\A股-v2-restore-test`，用临时容器和
`127.0.0.1:3028` 验证 `/health`。成功后删除临时容器和隔离目录；失败时保留隔离
目录供诊断。它拒绝覆盖 `D:\A股-v2\data`。生产恢复仍需单独、明确授权。

`pre-docker-20260812-103349` 是旧式备份，包含认证/密钥文件且没有 manifest，只能
留在本机作为历史回滚点，不得上传或外发。
