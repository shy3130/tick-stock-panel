/**
 * AI 对话助手前端扩展 — 完全解耦模块。
 *
 * 只注册一个 layout.navigation.extra 插槽作为常驻挂载点(菜单里不渲染
 * 入口): 可拖动悬浮球 + 左上角 AI 配置徽标旁入口 + 右缘滑入的非模态
 * 抽屉(见 AssistantLauncher)。删除本目录即整体卸载, 不修改任何核心
 * 文件; 复用的核心能力均为只读 import(MarkdownRenderer、cn、设计令牌)。
 * 自包含 HTTP 客户端见 client.ts 的取舍说明。
 */
import type { FrontendExtension } from '@/extensions/types'
import { AssistantLauncher } from './AssistantLauncher'
import { AssistantHeaderEntry } from './ui/AssistantHeaderEntry'
import { FabVisibilitySetting } from './ui/FabVisibilitySetting'

const extension: FrontendExtension = {
  id: 'assistant.chat',
  apiVersion: 1,
  slots: [
    {
      name: 'layout.navigation.extra',
      id: 'assistant-entry',
      order: 10,
      component: AssistantLauncher,
    },
    {
      name: 'layout.header.quick',
      id: 'assistant-header-entry',
      order: 10,
      component: AssistantHeaderEntry,
    },
    {
      name: 'settings.system.section',
      id: 'assistant-fab-visibility',
      order: 10,
      component: FabVisibilitySetting,
    },
  ],
}

export default extension
