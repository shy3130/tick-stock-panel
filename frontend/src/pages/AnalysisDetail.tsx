import { useParams } from 'react-router-dom'
import { ExtDimensionAnalysis } from '@/components/ExtDimensionAnalysis'

export function AnalysisDetail() {
  const { menuId } = useParams()
  return <ExtDimensionAnalysis menuId={menuId} />
}
