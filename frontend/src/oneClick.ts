import type { Task } from './types'

export type WorkflowMode = 'manual' | 'one-click'
export const workflowModeKey = 'tuxu.workflow-mode'
export function readWorkflowMode(): WorkflowMode {
  try { return typeof window !== 'undefined' && window.localStorage.getItem(workflowModeKey) === 'one-click' ? 'one-click' : 'manual' } catch { return 'manual' }
}
export function saveWorkflowMode(mode: WorkflowMode) {
  try { window.localStorage.setItem(workflowModeKey, mode) } catch { /* A preference never blocks creation. */ }
}
export function automationActive(task: Task | null): boolean {
  return !!task?.automation && ['running', 'waiting'].includes(task.automation.status)
}
export function canStartOneClick(task: Task | null): boolean {
  return !!task && task.status === 'imported' && !task.plan && !task.approval && !task.automation &&
    !task.assets.length && !task.exports.length && !task.jobs.length
}
export function oneClickKey(task: Task): string { return `one-click:${task.id}` }

/** A historical automatic export must never become the current delivery. */
export function oneClickExport(task: Task | null) {
  if (!task || task.status !== 'exported' || !task.plan || task.automation?.status !== 'completed') return undefined
  const entry = task.exports.find(item => item.url === task.automation?.export_url)
  if (!entry || entry !== task.exports.at(-1) || entry.plan_version !== task.plan.version || entry.document_hash !== task.document.source_hash) return undefined
  const approvalId = task.approval?.id || task.approval?.approval_id || null
  if (entry.approval_id !== approvalId) return undefined
  return entry
}
