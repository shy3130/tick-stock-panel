# TSP 品牌资产

主标采用**双引精修版**。两笔共用水平紫色渐变，中央留白勾出一根 K 线。方形主标使用 64 × 64 的网格。[新版 Logo 预览与下载](./logo-proposals.html)

## 矢量源文件

| 文件 | 用途 |
| --- | --- |
| [logo.svg](./logo.svg) | 透明底主标，浅紫到深紫，适用于浅底 |
| [logo-dark.svg](./logo-dark.svg) | 透明底亮紫版，适用于深底 |
| [logo-white.svg](./logo-white.svg) | 透明底纯白版，适用于品牌色背景 |
| [logo-badge.svg](./logo-badge.svg) | 紫色方底白标，适用于正方形头像 |
| [logo-tile-light.svg](./logo-tile-light.svg) / [logo-tile-dark.svg](./logo-tile-dark.svg) | 浅色或深色圆角底卡 |
| [logo-title.svg](./logo-title.svg) / [logo-dark-title.svg](./logo-dark-title.svg) | 底部对齐墨迹的裁剪版，适合嵌入单行标题 |

## PNG 与桌面图标

- `png/logo-{16,24,32,48,64,128,256,512}.png` 是标准渐变版本。带 `-dark` 或 `-white` 的文件可用于深色或品牌色背景。
- `png/logo-badge-{32,48,64,128,256,512,1024}.png` 是头像徽章。
- `png/logo-tile-{light,dark}-{128,512}.png` 是圆角底卡。
- `icon.ico` 与 `icon.icns` 由浅色圆角底卡生成，分别用于 Windows 安装程序和 macOS 应用。打包配置直接引用本目录的图标。
- Windows 安装向导使用主标和 `png/installer-wizard.png`，欢迎页与完成页的图案由深底亮紫主标生成。
- macOS DMG 挂载后的卷图标与卷内应用都使用 `icon.icns`。
- [侧栏菜单图标族](./png/menu-icons-preview.png) 对应 `frontend/src/components/BrandIcons.tsx`。

## 项目接入

- 侧栏、登录、初始化和路由加载页使用共用的 `frontend/src/components/Logo.tsx`。界面图标继承所在页面的配色。
- 浏览器标签页使用 `frontend/public/favicon.svg`，按系统明暗模式选择渐变。
- README 首屏使用 `picture` 切换浅色 `logo.svg` 与深色 `logo-dark.svg`。社交头像可用 [1024 像素徽章](./png/logo-badge-1024.png)。
- 小图标使用已有的 16、24 或 32 像素版本。等比缩放，保留中间的 K 线留白。

更新品牌资源后，分别检查 README 浅深色、浏览器标签及桌面安装包。macOS 查看安装后应用与 Dock，Windows 查看安装向导及应用图标。
