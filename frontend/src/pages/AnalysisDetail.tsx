import { useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { ExtDimensionAnalysis } from '@/components/ExtDimensionAnalysis'
import { ExtUrlEmbed } from '@/components/ExtUrlEmbed'

export function AnalysisDetail() {
  const { menuId } = useParams()
  // url 嵌入型提前分发; 数据型模板回落 ExtDimensionAnalysis (其内部自行按 menuId 取菜单)
  const { data } = useQuery({ queryKey: QK.analysisMenus, queryFn: api.analysisMenus })
  const menu = data?.items.find(m => m.id === menuId)
  if (menu?.template === 'url') {
    return <ExtUrlEmbed label={menu.label} url={menu.url ?? ''} />
  }
  return <ExtDimensionAnalysis menuId={menuId} />
}
