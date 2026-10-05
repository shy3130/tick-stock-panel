# TSP 品牌资产 · 唯一来源

图形：**方括号 `[ ]` + 中央 bullish K 线**，品牌紫渐变，全直角（无圆角/圆笔帽）。
设计提案与评审记录见 [`logo-proposals.html`](./logo-proposals.html)。

## SVG 源文件（矢量，优先使用）

| 文件 | 变体 | 场景 |
| --- | --- | --- |
| `logo.svg` | 渐变 · 浅底 `#A78BFA → #7C3AED` | 默认：README / 文档 / 浅色 UI |
| `logo-dark.svg` | 渐变 · 深底（亮紫）`#C4B5FD → #8B5CF6` | 终端 / 暗色 UI |
| `logo-white.svg` | 反白 · 纯白 `#FFFFFF` | 页头 / 启动页 / 品牌色背景 |
| `logo-badge.svg` | 徽章 · 方底白标 `#8B5CF6 → #6D28D9` | App 图标 / 头像（平台圆角由宿主裁切） |
| `logo-tile-light.svg` | 场景底卡 · 浅色圆角卡 `#F6F6F8` 底 | 文档配图 / 社交卡片（浅色） |
| `logo-tile-dark.svg` | 场景底卡 · 深色圆角卡 `#191C25` 底 + `#232838` 描边 | 文档配图 / 社交卡片（深色） |
| `logo-title.svg` / `logo-dark-title.svg` | 裁剪版 · viewBox 裁至墨迹底边 (`0 0 64 57.6`) | **README 标题行内嵌** — 墨迹贴基线, 与文字垂直居中 |

## PNG（全部透明底）

- `png/logo-{16,24,32,48,64,128,256,512}.png` — 浅底渐变
- `png/logo-dark-{16,24,32,48,64,128,256,512}.png` — 深底亮紫
- `png/logo-white-{16,24,32,48,64,128,256,512}.png` — 纯白
- `png/logo-badge-{32,48,64,128,256,512,1024}.png` — 徽章
- `png/logo-tile-{light,dark}-{512,128}.png` — 场景底卡（圆角 12.5%，logo 占比 62.5% 居中）
- `png/menu-icons-preview.png` — 侧栏菜单图标族对照表（19 个, 源码 `frontend/src/components/BrandIcons.tsx`）

## 其他

- `icon.ico` — Windows 应用图标（内嵌 16 / 24 / 32 / 48 / 64 / 128 / 256 七档；浅色圆角卡版，由 `png/logo-tile-light-512.png` 派生 —— 透明底渐变 logo 在桌面快捷方式上显得过空，圆角浅卡在深/浅壁纸均成立）
- `icon.icns` — macOS 应用图标（16–1024 八档，含 @2x；与 `icon.ico` 同源同版）

## 接入指引

- **favicon**：`frontend/public/favicon.svg`（几何与本套一致）
- **README / 文档**：引用 `logo.svg`；徽章位可用 `png/logo-128.png`
- **打包分发**：`icon.ico`（如 `packaging/`、PyInstaller、Docker 元数据）
- **GitHub 社交头像**：`png/logo-badge-1024.png`

## 使用规范

- 网格 64×64；括号线幅 5；影线 3.5 @55% 不透明；K 线实体 11×17；bullish 姿态（上影短/下影长）
- 徽章版：方底 60×60，标记白色、括号线幅 4、影线 3 @60%、实体 9×14
- 最小可用尺寸 **16px**；小于 32px 优先用 PNG 预缩版本而非浏览器缩放
- 保持全直角语言：不得添加圆角、描边、投影或渐变以外的色彩
- 周边留白：图形外保留 ≥ 1/8 图形宽度的净空
