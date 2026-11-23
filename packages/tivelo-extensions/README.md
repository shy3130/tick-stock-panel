# Tivelo Extensions

本地扩展安装器供 Tivelo Data、Tivelo Backtest 和 TSP 共用。安装器校验发行签名、平台、能力和有效版本，在独立进程自检后切换版本。每个任务可按制品摘要固定已获准使用的版本。发行公钥由部署者通过 `TIVELO_RELEASE_TRUST_FILE` 配置。

## 许可

**Tivelo 自有实现未经权利人单独书面授权不得商用。** 自有实现采用 [PolyForm Noncommercial License 1.0.0](LICENSE)，按许可条款允许的用途可免费使用。

此前已按 MIT 许可授出的材料保留原有权利和版权声明，见 [MIT 许可](LICENSE-MIT)和[许可说明](NOTICE)。

```sh
tivelo-extensions --state ./installation inspect --archive vendor.zip
tivelo-extensions --state ./installation stage --archive vendor.zip --digest DIGEST --revision REVISION
tivelo-extensions --state ./installation switch activate --digest DIGEST --generation 0
tivelo-extensions --state ./installation list
tivelo-extensions --state ./installation describe-source --provider SOURCE_ID
```

使用 `list` 返回的活动摘要和代际可执行 `switch rollback` 或 `switch disable`。商业包使用相同安装身份和凭证管理命令。免费与商业数据源的运行配置见 Tivelo Data 的签名数据源说明。

Linux 桌面宿主可安装 `tivelo-extensions[linux-desktop]`，通过当前登录会话的 Secret Service 保管专业设备密钥。TSP 的 Linux 桌面构建含此依赖。使用商业扩展时需要可用的 D-Bus 用户会话和已解锁的系统密钥集合。免费回测和签名社区数据源无需设备身份。独立服务端继续使用自己的受限安装目录。

排查时先区分签名信任、版本兼容、是否已启用以及本机授权是否有效。独立进程测试可使用临时安装目录，工作台操作见[引擎扩展和更新](../../docs/engine-platform/extensions-and-releases.md)。
