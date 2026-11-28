# 自定义数据源 mock 联调示例

这个目录提供一个本地 mock HTTP 数据源,用于验证项目的自定义数据源接入链路。

## 运行 mock 服务

```bash
cd docs/examples/custom-data-source
python mock_server.py
```

服务默认监听:

```text
http://127.0.0.1:3021
```

端点:

| 端点 | 数据 |
| --- | --- |
| `/daily` | 日K |
| `/adj_factor` | 除权因子 |
| `/realtime` | 全市场实时快照 |

## 接入项目

运行 mock 后，另开终端从仓库根目录复制 YAML。下面假定后端使用默认 `data/`，也可复制到隔离环境的 `DATA_DIR/data_sources/`。

```bash
mkdir -p data/data_sources
cp docs/examples/custom-data-source/mock_source.yaml data/data_sources/mock_source.yaml
```

然后在项目里打开:

```text
设置 -> 数据源 -> 重新加载
```

选择 `mock_source` 后，用「试拉测试」分别核对 `daily`、`adj_factor` 和 `realtime`。只有实际切换路由后，模拟响应才会进入工作台同步。使用 Docker 时，YAML 中的主机名应指向容器能访问的 mock 服务。

完整说明见 [../../custom-data-source.md](../../custom-data-source.md)。
