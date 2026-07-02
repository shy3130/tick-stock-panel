import { EmptyState } from 'tickflow-stock-panel-frontend'
import { Inbox, SearchX } from 'lucide-react'

export function Default() {
  return <EmptyState title="暂无数据" />
}

export function WithHint() {
  return (
    <EmptyState
      icon={Inbox}
      title="自选列表为空"
      hint="在个股页面点击「加自选」,即可在此处追踪持仓与关注的股票。"
    />
  )
}

export function SearchEmpty() {
  return (
    <EmptyState
      icon={SearchX}
      title="没有找到匹配的策略"
      hint="换个关键词试试,或清空筛选条件重新搜索。"
    />
  )
}
