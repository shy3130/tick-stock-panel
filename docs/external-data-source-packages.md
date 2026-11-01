# 外部数据源插件安装

外部安装是数据源插件机制的新入口：开发者发布 ZIP，用户在「设置 → 导入与备份」安装（原数据源页「安装插件」会跳转到此），随后点击新卡片填写 API Key、验证并选择接管的数据能力。无需为每个源改前端或重新发布主程序。

## 使用流程

1. 打开「设置 → 导入与备份」，选择或拖入 ZIP；数据源页的小盒子入口仍可使用。
2. 查看名称、作者声明、版本、能力和兼容检查结果。预览不会执行包内 Python。
3. 确认信任来源后安装。Python 插件与后端同权限，可读取数据和凭据；这不是沙箱。
4. 安装完成后点击「查看已安装扩展」，在数据源详情中继续配置。需要 Key 时显示「待配置 API Key」，有效 Key 才保存到现有 `user_data/secrets.json`；运行时初始化失败恢复原凭据和实例。
5. 在详情区「选择启用能力」勾选需要接管的能力，确认当前源到新源的变化。仅修改勾选项；安装、配置 Key 都不会自动切换路由。
6. 插件提供 `test_dataset` 时可以逐项试拉；未提供时显示「未检测」。验证 Key 不代表全部接口都有权限，试拉可能消耗供应商额度。

主程序部署的后端收到包，而不是浏览器所在电脑。Docker 请持久挂载整个配置的数据目录。

## 更新、回退和卸载

- 使用同一入口上传更高版本。新版暂存，当前进程继续使用旧版；完整重启后生效，设置页「重新加载」不会应用待生效更新。
- 重启时新版初始化失败，保留原有效版本并显示错误。缺 Key 属于待配置状态，不视为代码初始化失败。
- 详情区支持取消待生效更新、停用/启用、回退上一版本；回退也在重启后生效。
- 停用或卸载前必须先切换引用该插件的所有能力，包括除权、五档和全量分钟。检查保存的路由配置，不依赖当前可用性。
- 卸载后当前实例停止接收新调用，正在执行的调用/流式迭代结束后关闭；文件在重启时删除。Key 保留，需清除时在卸载前使用原 Key 配置区的清除操作。
- 相同包重复安装不会重复注册。相同版本但不同内容拒绝覆盖，请提升版本。
- 外部包在 `data/plugins/<name>/versions/<version>/`，激活状态在 `state.json`。实际 `data` 根目录服从应用配置；升级主程序不会覆盖该目录，兼容性仍须重新检查。

管理接口复用面板登录会话，不向开放 API Token 授权。未设置密码时仅允许本机同源访问；局域网部署请先设置密码。多 worker 部署应在停止全部 worker 后统一维护/重启；首版不提供跨 worker 在线版本同步。

## 包结构与清单

ZIP 根目录必须直接包含 `plugin.yaml`，不要额外包一层目录。每包一个数据源，继续沿用现有 Provider 协议，不需要另加 `manifest.json`。

```text
my-source.zip
├── plugin.yaml
├── provider.py
├── client.py
└── README.md
```

```yaml
name: my_source                # 小写字母开头，字母/数字/下划线，最多 64 字符
version: 1.0.0
plugin_api_version: 1
display_name: 我的数据源
runtime: none                  # none/python；不自动安装依赖
entry: provider:Provider       # 相对于包的模块；不是 app.plugins.*
datasets: [realtime]
api_key_env: MY_SOURCE_API_KEY
requires_app: '>=0.3.24'
requires_python: '>=3.11'
platforms: [any]               # 或 sys.platform-machine，如 linux-x86_64
requires: []                   # 现有宿主发行包版本约束，例如 'httpx>=0.27'
```

`description`、`author`、`homepage`、`key_manage_url`、`billing`、`billing_note`、`usage_notes` 沿用插件元数据；链接须为 HTTPS。作者字段只是包内声明，不是签名认证。`check: provider:availability` 可选，返回 `(bool, reason)`；只做本地就绪检查，不进行慢网络操作。

外部插件独立命名空间加载，内部使用 `from .client import ...` 等相对导入。普通子包需要自己的 `__init__.py`。不要修改全局 `sys.path`，不要写入程序目录，不要依赖核心服务私有属性。构造函数应快速、无网络访问；取数在数据方法中执行。

`Provider.name`、`config.datasets` 与清单必须一致，并实现声明能力的方法及 `close()`。接口、单位、复权和分钟时区契约见 [插件开发指南](plugin-development.md)。已安装但缺凭据或运行失败的源保留标识并显示不可用，不因名字暂时消失而换源。

声明 `api_key_env` 的插件必须在 entry 模块实现 `probe_api_key(key) -> (bool, reason)`，使用候选 Key 试探而不是读取当前已保存的 Key。Provider 实例读取 `secrets_store.get_env_backed_secret('<name>_api_key', '<ENV>')`，不要按请求重新读取 Key，否则无法保证在途调用使用原凭据。异常和探测原因不得包含 Key、鉴权头或带凭据的 URL。

可选 `test_dataset(dataset, symbols)` 沿用现有接口，返回 `{provider, dataset, rows, columns, preview, error?}`。实现有界样本请求、超时、分页上限；不要因试拉触发全历史同步。

## 运行环境边界

首版支持源码包（Python、YAML、JSON、文本/Markdown、CSV），上限 20 MiB 压缩包、80 MiB 解压总量、1000 条目。拒绝路径穿越、符号链接、重复/大小写冲突条目、Windows 保留路径以及原生库、wheel、可执行文件。

可使用主程序已有且版本满足要求的依赖；纯 Python 辅助模块可随包发布并相对导入。不会自动运行 pip/npm 或修改宿主环境。额外 SDK 的原生库随包安装暂不开放；需要平台认证和独立运行支持后再扩展。已有主程序内置的 SDK 不受影响。

旧 `app/plugins/*/plugin.yaml` 仍按原入口加载，旧 HTTP YAML 配置和凭据保留。外部包不允许覆盖内置或 YAML 数据源名称。主程序升级若超出插件兼容范围，卡片显示不可用；请更新插件后再启用。

## 扩展边界与 API

- 数据源加载器负责外置目录发现、包入口解析、统一注册和受控实例释放；原有服务继续通过 `get_provider` / `provider_has_dataset` 取数。
- 安装管理通过 `app/custom/source_packages` 的后端扩展注册，不修改主应用路由入口。
- 统一页面复用 `src/custom/source-packages` 的管理与能力选择组件。数据源页通过两个正式插槽跳转到统一入口：`settings.data-sources.toolbar`（`refresh`、`selectSource`）和 `settings.data-sources.detail-actions`（`plugin`、`refresh`）。这些是本次新增的已实现契约。
- 数据源列表、Key、试拉、路由 API 和查询缓存复用原实现，不建立另一套数据服务。

新增管理端点：

| 请求 | 用途 |
| --- | --- |
| `POST /api/custom/source-packages/inspect` | multipart `file`，静态预检，返回清单、摘要、兼容问题 |
| `POST /api/custom/source-packages/install` | multipart `file` + 预览的 `digest`，再次校验后安装/暂存更新 |
| `POST /api/custom/source-packages/{name}/manage` | JSON `action`: `enable` / `disable` / `rollback` / `cancel-update` / `remove` |

包文件先完整写入临时目录，再发布版本目录及原子状态文件。管理操作用跨进程文件数据库锁串行化；上传摘要只用于绑定预览和内容，不证明作者身份。Python 异常按插件隔离，但进程内插件无法隔离恶意行为、无限循环或原生崩溃。

## 开发示例与验收

仓库提供 [HTTP 数据源模板](../examples/data-source-plugin/provider.py) 及配套清单、client。它使用不可访问的示例域名；先替换实际接口并实现标准化，不会伪装成已可用数据源。

在模板目录执行 `zip -r ../example-http.zip plugin.yaml provider.py client.py`，或选中这三个文件压缩，确保 ZIP 根目录直接是文件。开发测试应使用假 Client，不需要真实 Key。

新增测试覆盖安全预检、安装取数、Key 先探后存、定向刷新、在途调用和迭代器释放、重启升级/回退、失败保留旧版、名称冲突、卸载引用检查和管理鉴权。发布时还应对目标操作系统的桌面产物和 Docker 镜像做安装冒烟；源码测试不能替代这些验证。
