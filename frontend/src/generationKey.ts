interface GenerationRecord {
  kind: string
  status: string
  approval_id: string
  plan_version: number
  idempotency_key?: string
  submissions?: readonly { state: string }[]
}

/** Unknown submissions recover their original job; confirmed terminal failures may retry. */
export function resolveGenerationKey(
  jobs: readonly GenerationRecord[], planVersion: number, approvalId: string,
  pendingKey?: string, createKey: () => string = () => crypto.randomUUID(),
): string {
  const matching = [...jobs].reverse().filter(job => job.kind === 'generation' && job.approval_id === approvalId && job.plan_version === planVersion)
  // An older unresolved intent also prevents a new submission for this approval.
  const unresolvedJob = matching.find(job => job.status === 'awaiting_agent' || job.submissions?.some(submission => ['submitting', 'submitted', 'awaiting_agent'].includes(submission.state)))
  const savedJob = unresolvedJob || matching[0]
  if (savedJob) {
    if (!savedJob.idempotency_key) throw new Error('当前生成记录缺少恢复标识，已阻止重新提交。请刷新本地服务后恢复原任务。')
    if (!unresolvedJob && ['failed', 'cancelled'].includes(savedJob.status)) {
      // A new retry can have an unknown HTTP response while GET still shows the
      // old terminal job. Reuse that pending key until the server records it.
      if (pendingKey && pendingKey !== savedJob.idempotency_key) return pendingKey
      return createKey()
    }
    return savedJob.idempotency_key
  }
  return pendingKey || createKey()
}
