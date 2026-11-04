import type { Approval, CloudCatalog, CloudQuality, ComfyStatus, ImageProvider, Task } from './types'

export function approvedProvider(approval?: Approval | null): ImageProvider {
  return approval?.image_target?.id || approval?.image_provider || 'local'
}

/** New-task recommendations never rewrite an unapproved historical draft. */
export function taskImageProvider(task: Pick<Task, 'id' | 'approval'>, choices: ReadonlyMap<string, ImageProvider>): ImageProvider {
  return task.approval ? approvedProvider(task.approval) : choices.get(task.id) || 'local'
}

export function targetModel(provider: ImageProvider, checkpoint: string, catalog?: CloudCatalog): string {
  return provider === 'local' ? checkpoint : provider === 'codex' ? 'codex-imagegen' : catalog?.profiles.find(profile => profile.id === provider)?.model || ''
}

export function targetReady(provider: ImageProvider, localReady: boolean, catalog?: CloudCatalog): boolean {
  return provider === 'local' ? localReady : provider === 'codex' ? !!catalog?.built_in.ready : !!catalog?.profiles.find(profile => profile.id === provider)?.ready
}

/** New approvals use masks; saved area approvals still use their original node set. */
export function regionControlFor(comfy?: ComfyStatus, savedApproval?: Approval | null) {
  return savedApproval && savedApproval.local_region_mode !== 'mask' ? comfy?.region_control : comfy?.mask_control
}

/** Freeze provider/model/configuration/quality together; old approvals remain local. */
export function imageScopeChanged(approval: Approval | null | undefined, provider: ImageProvider, checkpoint: string, quality: CloudQuality, attempts: number, catalog?: CloudCatalog): boolean {
  if (!approval) return false
  if (approvedProvider(approval) !== provider || approval.max_attempts !== undefined && approval.max_attempts !== attempts) return true
  if (provider === 'local') return !!approval.checkpoint && approval.checkpoint !== checkpoint
  const target = approval.image_target
  if (!target || target.model !== targetModel(provider, checkpoint, catalog)) return true
  // Native image tools choose their own quality. API controls cannot describe or
  // invalidate that scope; old v1 promises require a fresh explicit approval.
  if (provider === 'codex') return target.quality !== 'auto' || target.config_revision !== 'codex-bridge-v2'
  if (target.quality !== quality) return true
  const revision = catalog?.profiles.find(profile => profile.id === provider)?.revision
  return target.config_revision !== revision
}

export function approvalRequest(version: number, provider: ImageProvider, checkpoint: string, quality: CloudQuality, attempts: number) {
  return { version, image_provider: provider, ...(provider === 'local' ? { checkpoint } : provider === 'codex' ? {} : { cloud_quality: quality }), max_attempts: attempts, confirm: true }
}

export const activeJobStates = ['queued', 'running', 'awaiting_agent']
export const activeTaskStates = ['analyzing', 'generating', 'awaiting_agent']
