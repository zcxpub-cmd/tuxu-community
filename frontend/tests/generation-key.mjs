import assert from 'node:assert/strict'
import { resolveGenerationKey } from '../src/generationKey.ts'

let created = 0
const freshKey = () => `fresh-${++created}`
const saved = { kind: 'generation', status: 'failed', approval_id: 'approval-1', plan_version: 2, idempotency_key: 'server-key', submissions: [] }
const resolve = (jobs, pendingKey) => resolveGenerationKey(jobs, 2, 'approval-1', pendingKey, freshKey)

for (const status of ['queued', 'running', 'awaiting_agent', 'completed']) {
  assert.equal(resolve([{ ...saved, status }]), 'server-key', 'Refresh recovers an existing active/completed job')
  assert.equal(resolve([{ ...saved, status }], 'different-memory-key'), 'server-key', 'Active/completed server jobs remain authoritative')
}
for (const status of ['failed', 'cancelled', 'completed']) {
  for (const state of ['submitting', 'submitted', 'awaiting_agent']) {
    const unknown = { ...saved, status, submissions: [{ state }] }
    assert.equal(resolve([unknown]), 'server-key', 'Unknown failure after refresh only recovers the old job')
    assert.equal(resolve([unknown], 'different-memory-key'), 'server-key', 'An unresolved intent overrides a new local key')
  }
}
const unresolved = { ...saved, idempotency_key: 'older-unresolved', submissions: [{ state: 'submitted' }] }
assert.equal(resolve([unresolved, { ...saved, idempotency_key: 'latest-terminal' }]), 'older-unresolved', 'Any unresolved job in the same approval blocks new keys')
assert.equal(created, 0, 'Recovery and repeating completed results never allocate a new key')

for (const status of ['failed', 'cancelled']) {
  for (const submissions of [[], [{ state: 'failed' }], [{ state: 'saved' }], [{ state: 'failed' }, { state: 'saved' }]]) {
    const terminal = { ...saved, status, submissions }
    const before = created
    const retry = resolve([terminal], 'server-key')
    assert.equal(retry, `fresh-${before + 1}`, 'A confirmed terminal failure can retry under the same approval')
    assert.equal(resolve([terminal], retry), retry, 'Unknown HTTP response or repeat click retains the new retry key')
    assert.equal(created, before + 1, 'Repeated clicks before server sync cannot allocate another key')
  }
  const before = created
  assert.equal(resolve([{ ...saved, status, submissions: [{ state: 'failed' }] }]), `fresh-${before + 1}`, 'Refreshing a confirmed terminal failure still permits a safe retry')
}
const beforeNetwork = created
assert.equal(resolve([], 'pending-network-key'), 'pending-network-key', 'A first request with unknown HTTP result retains its key')
assert.equal(created, beforeNetwork)
const retryKey = resolve([{ ...saved, submissions: [{ state: 'failed' }] }], 'server-key')
assert.equal(resolve([{ ...saved, submissions: [{ state: 'failed' }] }], retryKey), retryKey)
const serverRetry = { ...saved, idempotency_key: retryKey, status: 'queued' }
assert.equal(resolve([saved, serverRetry]), retryKey, 'After refresh the recorded retry key is recovered')
assert.equal(resolve([saved, { ...serverRetry, status: 'failed', submissions: [{ state: 'submitting' }] }]), retryKey, 'A failed retry with an unresolved submit remains recoverable after refresh')
const failedRetry = { ...serverRetry, status: 'failed', submissions: [{ state: 'failed' }] }
assert.notEqual(resolve([saved, failedRetry], retryKey), retryKey, 'After positively confirmed failure of the retry, a later attempt may use a fresh key')
const mixed = { ...saved, submissions: [{ state: 'failed' }, { state: 'submitted' }] }
assert.equal(resolve([mixed]), 'server-key', 'Partial confirmed failure cannot bypass another unresolved image')
assert.throws(() => resolve([{ ...saved, idempotency_key: undefined }], 'memory-key'), /缺少恢复标识/)
assert.equal(resolveGenerationKey([{ ...saved, approval_id: 'old-approval' }], 2, 'approval-1', 'new-approval-key', freshKey), 'new-approval-key')
assert.equal(resolveGenerationKey([{ ...saved, plan_version: 1 }], 2, 'approval-1', 'new-plan-key', freshKey), 'new-plan-key')
assert.equal(resolve([{ ...saved, status: 'awaiting_agent', submissions: [] }]), 'server-key', 'Waiting for an agent cannot allocate another request even if intent metadata is missing')
console.log('PASS: confirmed terminal retry, unknown submission recovery, unknown HTTP/repeated clicks, partial unresolved submissions, refresh, completed reuse and missing-key rejection')
