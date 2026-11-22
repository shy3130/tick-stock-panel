# Python 数据源 ZIP 模板

本目录是一个只声明 `realtime` 能力的 Provider 模板。将 `client.py` 接到实际接口，并按 TSP 的字段、单位和时区契约转换响应。清单 `plugin.yaml` 的 `entry: provider:Provider` 用于外部 ZIP，Key 通过面板填写。

在本目录打包，压缩包根目录直接包含三个文件。

```bash
zip example-http.zip plugin.yaml provider.py client.py
```

1. 把 ZIP 导入「设置 → 导入与备份」，核对包版本和所声明能力。
2. 安装后配置并验证自己的 Key，试拉少量测试行情。
3. 核对返回的 `symbol`、价格、涨跌幅和成交字段，按需接管实时行情。后续版本更新清单版本号，再验证重启与回退。

[Provider 开发契约](../../docs/plugin-development.md) · [外部包安装与版本规则](../../docs/external-data-source-packages.md)
