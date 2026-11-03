import type { Asset } from './types'
import { reviewCategoryLabels, reviewStatusLabels, reviewSuggestions, reviewVerdictLabels, visibleReviewChecks } from './builtinAgent'
import Icon from './Icons'

export default function VisualReviewPanel({ asset, model, canReview, onReview }: {
  asset: Asset; model: string; canReview: boolean; onReview: () => void
}) {
  const review = asset.visual_review
  const status = review && Object.hasOwn(reviewStatusLabels, review.status) ? review.status : undefined
  const checks = visibleReviewChecks(asset)
  const suggestions = [...new Set(checks.filter(check => check.verdict !== 'match').map(check => reviewSuggestions[check.category]))]
  return <section className={`visual-review-panel ${status || 'not-checked'}`} aria-label="本地图片检查">
    <div className="visual-review-heading"><strong><Icon name="shield" size={15} />本地图片检查</strong><span>{status ? reviewStatusLabels[status] : review ? '检查结果未确认' : '尚未检查'}</span></div>
    <p className="visual-review-model">由本地 {review?.model || model || '看图模型'} 检查原图</p>
    {!!checks.length && <dl className="visual-review-checks">{checks.map((check, index) => <div key={`${check.category}-${index}`}><dt>{reviewCategoryLabels[check.category]}</dt><dd className={check.verdict}>{reviewVerdictLabels[check.verdict]}</dd></div>)}</dl>}
    {!!suggestions.length && <ul className="visual-review-suggestions">{suggestions.map(suggestion => <li key={suggestion}>{suggestion}</li>)}</ul>}
    {status === 'passed' && <p className="visual-review-note">本地模型未发现明显差异，仍需人工核对内容与事实。</p>}
    {review && (!status || ['needs_attention', 'unavailable', 'interrupted'].includes(status)) && <p className="visual-review-note">请人工检查这张图，按需调整画面后重新授权生成。结果未确认时不会自动再次检查或重画。</p>}
    {status && ['queued', 'running'].includes(status) && <p className="visual-review-note" role="status" aria-live="polite">任务已保存，检查完成后显示结论；刷新页面不会重复调用。</p>}
    {!review && <><p className="visual-review-note">点击即授权将这张原图和简短画面要求交给本地模型检查一次；不会调用生图服务。</p><button type="button" className="text-button" onClick={onReview} disabled={!canReview}>检查这张图（本地）<Icon name="arrow" size={14} /></button></>}
  </section>
}
