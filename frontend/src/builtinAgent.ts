import type { Asset, BuiltinAgentCatalog, BuiltinAgentDraft, Task, VisualReviewCategory, VisualReviewStatus } from './types'

export const reviewStatusLabels: Record<VisualReviewStatus, string> = {
  queued: '等待本地检查', running: '正在查看图片', passed: '检查通过',
  needs_attention: '需要人工检查', unavailable: '检查暂不可用', interrupted: '检查结果未确认',
}
export const reviewCategoryLabels: Record<VisualReviewCategory, string> = {
  subject: '主体', count: '数量', placement: '摆放', style: '画风', text: '图片文字',
}
export const reviewVerdictLabels = { match: '符合', mismatch: '有差异', uncertain: '待核对' } as const
export const reviewSuggestions: Record<VisualReviewCategory, string> = {
  subject: '请核对主体是否完整、准确，必要时调整画面描述。',
  count: '请核对物体数量，必要时明确数量后重新授权生成。',
  placement: '请核对物体位置，可返回导演台调整摆放。',
  style: '请核对画风与明暗，可返回导演台调整。',
  text: '请人工核对图片内文字，避免错字或伪字。',
}
export function visualReviewActive(task: Task | null): boolean {
  return !!task?.assets?.some(asset => ['queued', 'running'].includes(asset.visual_review?.status || ''))
}
export function canReviewAsset(task: Task | null, asset: Asset, locked: boolean): boolean {
  return !!task && !!task.plan && !!task.approval && !!(task.approval.id || task.approval.approval_id) &&
    task.status !== 'cancelled' && !locked && !asset.stale && !asset.visual_review &&
    task.assets.some(item => item.id === asset.id && item.stale === false && !item.visual_review) &&
    !task.jobs?.some(job => ['queued', 'running', 'awaiting_agent'].includes(job.status)) &&
    !['running', 'waiting'].includes(task.automation?.status || '') && !visualReviewActive(task)
}
export function builtinModels(catalog?: BuiltinAgentCatalog, models: string[] = []): string[] {
  return [...new Set([catalog?.model, ...models].filter((name): name is string => typeof name === 'string' && name.length > 0))]
}
export function builtinDraftChanged(catalog: BuiltinAgentCatalog, draft: BuiltinAgentDraft): boolean {
  return draft.model !== catalog.model || draft.visual_review_enabled !== catalog.visual_review_enabled
}
/** Public model text is never shown as a verdict or instruction. */
export function visibleReviewChecks(asset: Asset) {
  return (asset.visual_review?.checks || []).filter(check =>
    Object.hasOwn(reviewCategoryLabels, check.category) && Object.hasOwn(reviewVerdictLabels, check.verdict),
  )
}
