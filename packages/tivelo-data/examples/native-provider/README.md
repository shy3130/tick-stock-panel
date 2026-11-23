# 独立数据源发行与升级示例

在 Tivelo Data 源码包根目录执行。示例提供一个可编译的数据源、两份签名 ZIP、一次性演示公钥和采集请求。需要已安装的 `tivelo-data[extensions]`、uv 和适用于当前 Python 的本机 C 编译器。构建过程隔离安装 Cython 和 setuptools。示例输出目录必须是新目录。

```sh
python examples/native-provider/build.py --output ./demo-release
export TIVELO_RELEASE_TRUST_FILE="$PWD/demo-release/trust.json"
tivelo-extensions --state ./demo-state inspect --archive ./demo-release/example-1.0.0.zip
```

将预检结果中的 `digest` 和 `revision` 代入下列命令。

```sh
tivelo-extensions --state ./demo-state stage --archive ./demo-release/example-1.0.0.zip --digest DIGEST --revision REVISION
tivelo-extensions --state ./demo-state switch activate --digest DIGEST --generation 0
tivelo-data --root ./demo-data plan-provider --extension-state ./demo-state --capture ./demo-release/capture.json > first-plan.json
```

对 2.0.0 重复预检与暂存，启用时传入新版摘要和当前代际 1，随后分别执行两次采集。

```sh
tivelo-data --root ./demo-data import-provider --extension-state ./demo-state --capture ./demo-release/capture.json --locked-plan first-plan.json
tivelo-data --root ./demo-data import-provider --extension-state ./demo-state --capture ./demo-release/capture.json
```

两次调用分别返回快照 ID。两个交易日原始收盘价均为 10 元。新版增加一条后续修订到每个交易日，2025-01-06 08:00 UTC 可见。查询日设为 1 月 4 日时两个快照都是 10 元，设为 1 月 7 日时原快照仍是 10 元，新快照为 12 元。用 `tivelo-extensions --state ./demo-state list` 查看切换代际和可回退版本。TSP 的研究任务可把相同的 `capture.json` 对象放入 `nodes[].capture.datasets`，由工作进程生成可导出的快照。

用于签名的演示私钥只在构建进程内生成并使用。生产部署由发行者管理长期密钥，部署者配置已审核的发行公钥和允许的能力。[数据与凭据契约](../../README.md#导入签名数据源包) · [扩展签名和启用](../../../../docs/engine-platform/extensions-and-releases.md)

交给 AI 验证时，记录每代包的摘要和启用后的版本。用同一批合成样本检查采集、时点查询与回退。用于 TSP 工作台 v1 Provider 的 ZIP 参见[双端示例](../../../../examples/dual-host-data-source/README.md)。
