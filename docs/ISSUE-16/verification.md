# Issue #16 验证记录

日期：2026-08-27
分支：`issue-16-macd-stages`

## 编译

命令：

```bash
cd backend
python3 -m py_compile app/services/macd_stages.py app/api/research.py
```

结果：通过，无输出。

说明：`uv run python ...` 首次尝试触发仓库既有 Hatch 构建配置错误（`Readme path must be within the project directory: ../README.md`），未修改该配置；改用直接 Python 完成同一编译检查。

## focused tests

命令：

```bash
cd backend
PYTHONPATH=. /Users/wf2311/Projects/wf2311/fm/tickflow-stock-panel/backend/.venv/bin/pytest tests/test_macd_stages.py -q
```

结果：`3 passed`。

覆盖：

- 端点 HTTP 200、schema、固定 10/20/7 参数和 unavailable 原因；
- 不返回 rows/series 等伪造阶段序列；
- 服务层确定性与固定参数。

## diff / 范围

命令：

```bash
git diff --check
git status --short
git diff --stat
git diff -- backend/app/api/research.py
```

结果：`git diff --check` 通过。改动仅包括：

- `backend/app/api/research.py`：新增研究路由及服务导入；
- `backend/app/services/macd_stages.py`：新增纯函数 fail-closed 契约；
- `backend/tests/test_macd_stages.py`：新增 3 个 focused tests；
- `docs/ISSUE-16/`：设计文档、README、本文档。

未修改 `data/`，未接入外部接口，未创建提交。

## 禁止项扫描

对本次新增内容扫描保留标识与操作性词汇，结果：零命中。

## 结论

当前端点明确返回 `status="unavailable"`；逐日状态机、OOS 和 PIT 读取能力仍标记缺失，不生成阶段数值。
