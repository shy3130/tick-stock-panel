# 统一导入与外部后端扩展包

入口是「设置 → 导入与备份」（`/settings?tab=import`）。策略、因子、信号页的小盒子，以及数据源的「安装插件」都会进入同一页面。旧配置备份和页面插件链接继续重定向到这里。

## 导入与管理

选择或拖入文件后，先自动识别、预览，再确认写入：

- 策略 `.py`、信号/因子 `.json` 和资源 ZIP 可混合多选；复用原有依赖闭包、冲突比较、批次回滚和缓存刷新。
- `tick-config-backup` 配置备份按类别恢复。每次新预览都默认不选择密钥和访问凭证；需要迁移 API Key 时明确勾选。
- 根目录有 `plugin.yaml` 的数据源包继续使用 [Provider 安装契约](external-data-source-packages.md)。安装后在「已安装扩展」配置 Key、验证并选择数据能力；不会自动切换行情来源。
- `.js/.mjs` 页面产物、单个 `.tsx` 或本项目已有源码页面继续使用 [Page Pack SDK](page-sdk.md)。确认信任和同名替换，通过浏览器加载校验后刷新生效。源码编译仍受现有部署环境限制。
- 根目录有 `extension.yaml` 的 ZIP 使用下述后端扩展契约，可同时包含配套前端页面。安装仅暂存，重启主程序后生效。

「创建配置备份」和「批量导出资源」保留原有范围，不包含后台扩展的安装文件。行情数据、K 线和回测缓存不包含在配置备份中。外部数据源与后端扩展请单独保留原 ZIP。

## 后端 / 前后端完整包

ZIP 根目录直接放清单，不额外套目录。每包一个后端模块，可选一个自包含页面 ES 模块：

```text
research.zip
├── extension.yaml
├── backend/
│   ├── __init__.py
│   └── extension.py
└── frontend/
    └── extension.mjs
```

```yaml
format: tick-extension-package
format_version: 1
id: example.research
display_name: 研究观察工作台
version: 1.0.0
extension_api_version: 1
author: 作者声明
description: 自定义研究服务与配套页面
backend: backend.extension
frontend: frontend/extension.mjs  # 纯后端扩展省略此项
requires_app: '>=0.3.25'
requires_python: '>=3.11'
platforms: [any]
requires: []  # 仅检查已有依赖，不运行 pip/npm
```

`id` 为小写字母开头的字母、数字、下划线，可用点或连字符分段，最长 80 字符；`official.*`、`core.*` 保留。版本如 `1.0.0`；相同版本不同内容拒绝覆盖。更新必须提高版本，降级使用回退操作。

后端模块沿用 `app.extensions` 的真实契约：

```python
from fastapi import APIRouter
from app.extensions import BACKEND_EXTENSION_API_VERSION

EXTENSION_ID = "example.research"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

def setup(registrar):
    router = APIRouter(prefix="/api/extensions/example.research")

    @router.get("/status")
    def status():
        return {"ready": True}

    registrar.include_router(router)

def startup(context):
    # 可选；核心仓库准备完成后运行。
    # context 提供 api_version、data_dir、只读 repository 协议。
    pass
```

`setup` / `startup` 是同步函数。可注册 HTTP 路由和 `NotificationFormatter`，不支持替换核心引擎。外置接口必须位于 `/api/extensions/<包id>/`，不能覆盖核心或其他扩展，不接受 WebSocket 路由。初始化使用 `startup(context)`，不要依赖路由器自身的 lifespan。

配套页面使用 Page Pack SDK v1，`default.id` 与包 `id` 相同。用户确认信任后，浏览器先加载候选页面检查导出契约，成功才提交安装；预览阶段不执行 JS 或 Python。页面路由最终由宿主注册表检查，启动时发生冲突会显示在「已安装扩展」，需修正包后重新安装。

## 生效与失败隔离

安装内容位于 `data/extensions/<id>/versions/<version>/`，状态原子写入 `state.json`。限 20 MiB 压缩包、80 MiB 解压总量、1000 条目；拒绝越界路径、重复/大小写冲突、符号链接、原生库、wheel 和安装脚本。不写入程序源码目录。

启动时验证文件摘要、兼容性、ID、接口命名空间和注册冲突，再运行启动钩子。成功后才发布路由、通知格式化器和配套页面。新后台失败时保留原版本，新页面不会先切换；单个扩展错误不阻断主程序或其他扩展。

启停、回退、更新和卸载在下一次主程序启动时应用；浏览器刷新不能替代后台重启。停用中回退不会自动启用。旧页面可能保留在已打开标签中，后台重启完成后需刷新浏览器同步。

失败隔离覆盖已提交的路由、策略注册与安装状态；可信代码自行产生的文件、网络等副作用无法自动撤销。扩展与主程序同权限，不是沙箱。管理使用面板登录会话，不向开放 API Token 授权；无密码时仅接受本机同源管理。

首版面向单进程部署；多 worker 应全部停止后统一维护和重启，不提供跨 worker 在线切换。升级后仍需满足包的兼容范围。

## API

| 请求 | 行为 |
| --- | --- |
| `POST /api/import-center/inspect` | 多文件 `files`，只读识别并委托既有预检 |
| `GET /api/extension-packages` | 安装、运行、待生效版本与错误 |
| `POST /api/extension-packages/install` | `file/digest/revision/trusted=true`，重新校验后暂存 |
| `POST /api/extension-packages/frontend` | 信任后读取候选页面供浏览器校验，不发布 |
| `POST /api/extension-packages/{id}/manage` | `enable/disable/rollback/cancel-update/remove` |
| `POST /api/import-center/page` | 页面上传/编译，增加摘要、信任和同名替换确认 |
| `POST /api/import-center/project` | 信任后编译现有项目页面，保留同名确认 |

配套页面复用 `/api/ext-pages` 清单与分发，标记 `managed_by`，只在对应后台版本就绪时提供。启停和卸载通过整包管理，不能拆开修改。
