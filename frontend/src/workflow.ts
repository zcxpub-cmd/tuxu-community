import type { Task } from './types'

export const stages = ['文章与设置', '配图方案', '画面导演台', '授权与生成', '选图与处理', '插入与导出']

/** Resume the current operation/result; export history can belong to an old plan. */
export function taskStage(task: Task): number {
  if (task.status === 'awaiting_agent' || task.jobs?.some(job => job.status === 'awaiting_agent')) return 4
  if (task.status === 'analyzing') return 1
  if (task.status === 'generating') return 4
  const activeJob = [...(task.jobs || [])].reverse().find(job => job.status === 'queued' || job.status === 'running')
  if (activeJob) return activeJob.kind === 'analysis' ? 1 : 4
  if (task.status === 'exported') return 5
  if (task.assets?.some(asset => !asset.stale)) return 4
  if (task.approval) return 3
  return task.plan ? 1 : 0
}

type CompletionOptions = { unsaved: boolean; scopeChanged: boolean; selectedCount: number }
type StageCompletion = [boolean, boolean, boolean, boolean, boolean, boolean]

/** Completion is evidence of saved work, independently from the currently viewed step. */
export function completedStages(task: Task | null, { unsaved, scopeChanged, selectedCount }: CompletionOptions): StageCompletion {
  if (!task) return [false, false, false, false, false, false]
  const hasImages = !!task.plan?.slots.length
  const savedPlan = !!task.plan && !unsaved
  const directed = hasImages && savedPlan && !!task.approval
  // Job.status is an open string in the public contract. Unknown/new states must
  // not accidentally turn a pending operation into a successful generation.
  const settledTask = ['imported', 'plan_ready', 'approved', 'review', 'exported', 'failed', 'cancelled'].includes(task.status)
  const settledJobs = (task.jobs || []).every(job =>
    ['completed', 'failed', 'cancelled'].includes(job.status) &&
    (job.submissions || []).every(submission => ['saved', 'failed'].includes(submission.state)),
  )
  // Backend stale already validates the asset's plan/prompt/approval/document
  // relationship; a count alone cannot prove coverage of distinct current slots.
  const allSlotsReturned = !!task.plan?.slots.every(slot =>
    task.assets?.some(asset => asset.slot_id === slot.id && asset.stale === false),
  )
  return [
    true,
    savedPlan,
    directed,
    directed && !scopeChanged && allSlotsReturned && settledTask && settledJobs,
    hasImages && selectedCount > 0 && !unsaved && !scopeChanged,
    task.status === 'exported' && !unsaved && !scopeChanged,
  ]
}
