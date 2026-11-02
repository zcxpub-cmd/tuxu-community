import assert from 'node:assert/strict'
import test from 'node:test'
import { completedStages, taskStage } from '../src/workflow.ts'

const oldExport = { filename: 'synthetic-old-v1.docx', format: 'docx' }
const plan = { version: 2, slots: [{ id: 'slot-current', paragraph_id: 'p0' }] }
const approval = { id: 'approval-current' }
const base = { status: 'imported', plan: null, approval: null, assets: [], selections: {}, jobs: [], exports: [] }
const historical = { ...base, exports: [oldExport] }

test('old export history never promotes an imported task to completed delivery', () => {
  assert.equal(taskStage(historical), 0)
  assert.equal(taskStage({ ...historical, status: undefined }), 0, 'A legacy record without status cannot prove a current export')
})

test('a revised plan with old exports resumes at its current saved plan', () => {
  assert.equal(taskStage({ ...historical, status: 'plan_ready', plan }), 1)
  assert.equal(taskStage({ ...historical, status: 'plan_ready', plan, assets: [{ stale: true }] }), 1)
})

test('current approval remains at confirmation/generation despite old exports', () => {
  assert.equal(taskStage({ ...historical, status: 'approved', plan, approval }), 3)
})

test('current candidate and selected asset resume at review, not a historical export', () => {
  const asset = { id: 'asset-current', slot_id: 'slot-current', plan_version: 2, stale: false }
  const task = { ...historical, status: 'review', plan, approval, assets: [asset] }
  assert.equal(taskStage(task), 4)
  assert.equal(taskStage({ ...task, selections: { 'slot-current': { asset_id: asset.id, use_processed: false } } }), 4)
})

test('stale selected images and old exports cannot imply current review or delivery', () => {
  assert.equal(taskStage({ ...historical, assets: [{ id: 'old-asset', stale: true }], selections: { old: { asset_id: 'old-asset', use_processed: false } } }), 0)
})

test('an explicit current exported state resumes at delivery', () => {
  assert.equal(taskStage({ ...historical, status: 'exported', plan, approval, assets: [{ stale: false }] }), 5)
})

test('a saved zero plan is editable until actually exported, then resumes at delivery', () => {
  const zero = { ...historical, status: 'plan_ready', plan: { version: 2, slots: [] }, assets: [{ stale: true }] }
  assert.equal(taskStage(zero), 1)
  assert.equal(taskStage({ ...zero, status: 'exported' }), 5, 'Zero-image exports require no approval or selected image')
})

test('analysis in progress takes precedence over old approval, assets and exports', () => {
  const analyzing = { ...historical, status: 'analyzing', plan, approval, assets: [{ stale: false }] }
  assert.equal(taskStage(analyzing), 1)
  assert.equal(taskStage({ ...base, status: 'analyzing' }), 1)
})

test('generation in progress resumes at image review even before candidates exist', () => {
  assert.equal(taskStage({ ...historical, status: 'generating', plan, approval }), 4)
})

test('awaiting agent state or job preserves pending-image recovery', () => {
  assert.equal(taskStage({ ...historical, status: 'awaiting_agent', plan, approval }), 4)
  assert.equal(taskStage({ ...historical, status: 'failed', plan, jobs: [{ kind: 'generation', status: 'awaiting_agent' }] }), 4)
  assert.equal(taskStage({ ...historical, status: 'exported', plan, jobs: [{ kind: 'generation', status: 'awaiting_agent' }] }), 4)
})

test('active jobs determine the pending stage rather than completed export history', () => {
  for (const status of ['queued', 'running']) {
    assert.equal(taskStage({ ...historical, status: 'plan_ready', plan, approval, jobs: [{ kind: 'analysis', status }] }), 1)
    assert.equal(taskStage({ ...historical, status: 'approved', plan, approval, jobs: [{ kind: 'generation', status }] }), 4)
  }
  assert.equal(taskStage({ ...historical, status: 'exported', plan, jobs: [{ kind: 'analysis', status: 'running' }] }), 1, 'A recorded active operation must not be hidden by stale completion state')
  assert.equal(taskStage({ ...historical, jobs: [{ kind: 'analysis', status: 'running' }, { kind: 'generation', status: 'queued' }] }), 4, 'The latest active operation determines recovery')
  assert.equal(taskStage({ ...historical, jobs: [{ kind: 'generation', status: 'completed' }, { kind: 'analysis', status: 'failed' }] }), 0, 'Terminal jobs alone do not imply current progress')
})

test('failed/cancelled tasks resume from their usable current artifacts', () => {
  for (const status of ['failed', 'cancelled']) {
    assert.equal(taskStage({ ...historical, status }), 0)
    assert.equal(taskStage({ ...historical, status, plan }), 1)
    assert.equal(taskStage({ ...historical, status, plan, approval }), 3)
    assert.equal(taskStage({ ...historical, status, plan, approval, assets: [{ stale: false }] }), 4)
  }
})

test('recovery is read-only and leaves old task/export records untouched', () => {
  const task = { ...historical, status: 'approved', plan, approval, jobs: [{ kind: 'analysis', status: 'completed' }, { kind: 'generation', status: 'queued' }] }
  const before = JSON.stringify(task)
  assert.equal(taskStage(task), 4)
  assert.equal(JSON.stringify(task), before)
  assert.equal(task.exports[0], oldExport)
})

const completionOptions = { unsaved: false, scopeChanged: false, selectedCount: 0 }
const complete = (task, options = {}) => completedStages(task, { ...completionOptions, ...options })
const twoSlotPlan = { ...plan, slots: [...plan.slots, { id: 'slot-second', paragraph_id: 'p1' }] }
const returnedAssets = twoSlotPlan.slots.map((slot, index) => ({ id: `candidate-${index}`, slot_id: slot.id, stale: false }))
const returnedTask = { ...historical, status: 'review', plan: twoSlotPlan, approval, assets: returnedAssets }

test('completion starts with actual article/plan records, not the viewed step', () => {
  assert.deepEqual(complete(null), [false, false, false, false, false, false])
  assert.deepEqual(complete(base), [true, false, false, false, false, false])
  assert.deepEqual(complete({ ...historical, status: 'plan_ready', plan }), [true, true, false, false, false, false])
})

test('approval completes director confirmation but cannot prove generation', () => {
  assert.deepEqual(complete({ ...historical, status: 'approved', plan, approval }), [true, true, true, false, false, false])
  assert.equal(complete({ ...returnedTask, approval: null })[3], false)
})

test('queued work without returned images cannot complete generation', () => {
  for (const status of ['queued', 'running']) {
    const task = { ...historical, status: 'approved', plan, approval, jobs: [{ kind: 'generation', status }] }
    assert.deepEqual(complete(task), [true, true, true, false, false, false])
  }
})

test('partial, stale and duplicate slot candidates cannot stand in for all current slots', () => {
  assert.equal(complete({ ...returnedTask, assets: returnedAssets.slice(0, 1) })[3], false)
  assert.equal(complete({ ...returnedTask, assets: [returnedAssets[0], { ...returnedAssets[1], stale: true }] })[3], false)
  assert.equal(complete({ ...returnedTask, assets: [returnedAssets[0], { ...returnedAssets[0], id: 'another-first-slot' }] })[3], false)
  assert.equal(complete({ ...returnedTask, assets: [returnedAssets[0], { slot_id: 'old-slot', stale: false }] })[3], false)
  assert.equal(complete({ ...returnedTask, assets: [returnedAssets[0], { slot_id: 'slot-second' }] })[3], false, 'An absent stale flag is not explicit evidence of a current asset')
})

test('all distinct current slots with real non-stale assets complete generation', () => {
  assert.deepEqual(complete(returnedTask), [true, true, true, true, false, false])
  const jobs = [{ kind: 'generation', status: 'completed', submissions: [{ state: 'saved' }, { state: 'saved' }] }]
  assert.equal(complete({ ...returnedTask, jobs })[3], true)
  assert.deepEqual(complete(returnedTask, { selectedCount: 1 }), [true, true, true, true, true, false])
})

test('active and unknown task states prevent a generation-complete check even with assets', () => {
  for (const status of ['analyzing', 'generating', 'awaiting_agent', 'queued', 'running', 'submitting', 'submitted', 'unknown', 'new-unrecognized-state']) {
    assert.equal(complete({ ...returnedTask, status })[3], false, status)
  }
})

test('active, unknown and unrecognized job statuses cannot complete generation', () => {
  for (const status of ['queued', 'running', 'awaiting_agent', 'submitting', 'submitted', 'unknown', 'new-unrecognized-state']) {
    assert.equal(complete({ ...returnedTask, jobs: [{ kind: 'generation', status }] })[3], false, status)
  }
  assert.equal(complete({ ...returnedTask, jobs: [{ kind: 'analysis', status: 'running' }] })[3], false)
})

test('unresolved submission fields block completion despite terminal-looking job statuses', () => {
  for (const status of ['completed', 'failed', 'cancelled']) {
    for (const state of ['submitting', 'submitted', 'awaiting_agent', 'unknown', 'new-unrecognized-state']) {
      const jobs = [{ kind: 'generation', status, submissions: [{ state: 'saved' }, { state }] }]
      assert.equal(complete({ ...returnedTask, jobs })[3], false, `${status}/${state}`)
    }
  }
  const resolved = [{ kind: 'generation', status: 'failed', submissions: [{ state: 'saved' }, { state: 'failed' }] }]
  assert.equal(complete({ ...returnedTask, jobs: resolved })[3], true, 'Confirmed failed attempts do not erase real returned assets for every slot')
})

test('zero-image plans mark director/generation/selection unnecessary rather than complete', () => {
  const zero = { ...returnedTask, plan: { version: 2, slots: [] }, status: 'plan_ready' }
  assert.deepEqual(complete(zero, { selectedCount: 2 }), [true, true, false, false, false, false])
  assert.deepEqual(complete({ ...zero, status: 'exported' }), [true, true, false, false, false, true])
})

test('unsaved work invalidates every saved completion after the article', () => {
  assert.deepEqual(complete({ ...returnedTask, status: 'exported' }, { unsaved: true, selectedCount: 2 }), [true, false, false, false, false, false])
})

test('scope changes invalidate generation, selection and export but preserve saved direction', () => {
  assert.deepEqual(complete({ ...returnedTask, status: 'exported' }, { scopeChanged: true, selectedCount: 2 }), [true, true, true, false, false, false])
})

test('current exported status is required for completion; old exports never count', () => {
  assert.equal(complete(returnedTask)[5], false)
  assert.equal(complete({ ...returnedTask, status: 'failed' })[5], false)
  assert.equal(complete({ ...returnedTask, status: 'exported', exports: [] })[5], true, 'Completion comes from current status, not historical file count')
})

test('completion calculation preserves assets, slots, jobs and exports', () => {
  const task = { ...returnedTask, jobs: [{ kind: 'generation', status: 'completed', submissions: [{ state: 'saved' }] }] }
  const before = JSON.stringify(task)
  complete(task, { selectedCount: 1 })
  assert.equal(JSON.stringify(task), before)
})
