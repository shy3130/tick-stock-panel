/**
 * 品牌配置 — 集中管理产品名称、宣传语与品牌色。
 *
 * 目的:此前 "Sycee" 作为字符串字面量分散硬编码在 Landing/Auth/Onboarding/
 * Sidebar/router 等约 10 处,后续若改名/调品牌色需要逐处修改。收敛到这里后
 * 只改一处。
 *
 * 例外(有意不引用此文件):
 *   - index.html 的 <title> 和内联防闪烁脚本 —— 纯静态 HTML,构建前执行,
 *     无法 import TS 模块,只能手动保持同步。
 *   - Logo.tsx 的 aria-label —— 与图形定义强耦合,保留原地硬编码更直观。
 *   - lib/theme.ts / lib/sidebarState.ts / lib/storage.ts 里的 localStorage
 *     key 前缀(tf-)—— 是历史技术前缀,不是品牌展示文案,改名会导致老用户
 *     本地设置丢失,不在本次品牌清理范围内,故不收敛到这里。
 */

export const BRAND_NAME = 'Sycee'

/** 品牌强调色(logo 发光/描边等,不影响功能语义色如 accent/bull/bear) */
export const BRAND_COLOR = '#8B5CF6'

/** Auth/Onboarding/router loading 态等处使用的简短标语 */
export const BRAND_TAGLINE = '服务器部署 A 股量化工作台'
