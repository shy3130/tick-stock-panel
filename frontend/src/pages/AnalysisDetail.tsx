import { useParams } from 'react-router-dom'
import { ExtDimensionAnalysis } from '@/components/ExtDimensionAnalysis'

export function AnalysisDetail() {
  const { menuId } = useParams()
  return (
    <div className="workspace-page h-full min-h-0">
      <ExtDimensionAnalysis menuId={menuId} />
    </div>
  )
}
