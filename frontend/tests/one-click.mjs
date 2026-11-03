import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import { automationActive, canStartOneClick, oneClickExport, oneClickKey, readWorkflowMode, saveWorkflowMode } from '../src/oneClick.ts'
import { createTaskPollGate } from '../src/taskPollGate.ts'

const imported = { id: 'synthetic-task', status: 'imported', plan: null, approval: null, assets: [], jobs: [], exports: [], document: { source_hash: 'synthetic-document' } }
const polls = createTaskPollGate()
const beforeCancel = polls.capture()
polls.begin()
const duringCancel = polls.capture()
assert.equal(polls.accepts(beforeCancel), false)
assert.equal(polls.accepts(duringCancel), false)
polls.end()
assert.equal(polls.accepts(beforeCancel), false, 'A late pre-cancel GET cannot restore a running automation after cancel')
assert.equal(polls.accepts(duringCancel), false, 'GETs taken during a write are also invalid after its confirmed response')
assert.equal(polls.accepts(polls.capture()), true, 'New reads resume after cancellation')
polls.begin(); polls.begin(); polls.end()
assert.equal(polls.accepts(polls.capture()), false, 'Overlapping writes cannot re-enable polling early')
polls.end(); assert.equal(polls.accepts(polls.capture()), true)
assert.equal(canStartOneClick(imported), true)
assert.equal(canStartOneClick({ ...imported, plan: { version: 1, slots: [] } }), false, 'Even a zero-image manual plan must be preserved')
for (const patch of [{ status: 'failed' }, { assets: [{}] }, { exports: [{}] }, { jobs: [{}] }, { approval: {} }, { automation: {} }]) assert.equal(canStartOneClick({ ...imported, ...patch }), false)
assert.equal(oneClickKey(imported), oneClickKey(structuredClone(imported)), 'Refreshing or retrying the same import uses the same operation key')
for (const status of ['running', 'waiting']) assert.equal(automationActive({ ...imported, automation: { status } }), true)
for (const status of ['completed', 'cancelled', 'paused', 'failed', 'unknown']) assert.equal(automationActive({ ...imported, automation: { status } }), false)
assert.equal(readWorkflowMode(), 'manual', 'SSR has a safe manual preference')
assert.doesNotThrow(() => saveWorkflowMode('one-click'), 'Unavailable browser storage does not prevent operation')
const exported = { ...imported, status: 'exported', plan: { version: 1, slots: [] }, exports: [{ url: '/api/exports/synthetic-task/final.docx', format: 'docx', plan_version: 1, approval_id: null, document_hash: 'synthetic-document' }], automation: { id: 'synthetic-run', status: 'completed', stage: 'completed', message: '制作完成', total_images: 0, completed_images: 0, max_images: 5, format: 'docx', export_url: '/api/exports/synthetic-task/final.docx' } }
assert.equal(oneClickExport(exported), exported.exports[0])
for (const patch of [{ status: 'review' }, { approval: { id: 'changed' } }, { plan: { version: 2, slots: [] } }, { document: { source_hash: 'changed' } }, { automation: { ...exported.automation, status: 'failed' } }, { exports: [...exported.exports, { url: '/newer-manual.docx' }] }]) assert.equal(oneClickExport({ ...exported, ...patch }), undefined, 'Old automatic exports cannot represent a revised task')

const previousFetch = globalThis.fetch
let calls = 0
globalThis.fetch = async () => { calls++; throw new Error('Rendering must not run the pipeline') }
const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' })
try {
  const { default: Panel, WorkflowModeSwitch } = await server.ssrLoadModule('/src/OneClickPanel.tsx')
  const props = { format: 'docx', onFormat() {}, providerLabel: '合成图片服务', textModel: 'local-text', ready: true, readiness: '模型尚未就绪', busy: false, onStart() {}, onManual() {}, children: null }
  const render = task => renderToStaticMarkup(createElement(Panel, { ...props, task }))
  const fresh = render(imported)
  for (const required of ['点击即授权', '最多5张', '每张仅尝试1次', '自动采用有效原图', '费用', '失败或结果未知', '一键生成图文文档']) assert.ok(fresh.includes(required), required)
  assert.doesNotMatch(fresh, /href=.*final\.docx/)
  const disabled = renderToStaticMarkup(createElement(Panel, { ...props, task: imported, ready: false }))
  assert.match(disabled, /disabled=""/)
  assert.match(disabled, /模型尚未就绪/)
  const ongoing = render({ ...imported, automation: { ...exported.automation, status: 'waiting', stage: 'generation', total_images: 2, completed_images: 1, message: '等待真实图片' } })
  assert.match(ongoing, /1 \/ 2/)
  assert.match(ongoing, /停止自动流程，改为人工设置/)
  assert.doesNotMatch(ongoing, /图文文档已就绪|one-click-download/)
  const done = render(exported)
  assert.match(done, /图文文档已就绪/)
  assert.match(done, /无需新增插图/)
  assert.match(done, /href="\/api\/exports\/synthetic-task\/final.docx"/)
  const stale = render({ ...exported, status: 'review' })
  assert.match(stale, /当前文档已有变化/)
  assert.doesNotMatch(stale, /one-click-download/)
  const failure = render({ ...imported, automation: { ...exported.automation, status: 'failed', message: '已停止，不自动重试' } })
  assert.match(failure, /需要处理/)
  assert.doesNotMatch(failure, /one-click-download/)
  const safe = render({ ...imported, automation: { ...exported.automation, status: 'paused', message: '<script>bad</script>' } })
  assert.match(safe, /&lt;script&gt;/)
  for (const mode of ['manual', 'one-click']) {
    const switcher = renderToStaticMarkup(createElement(WorkflowModeSwitch, { mode, disabled: false, onChange() {} }))
    assert.equal((switcher.match(/aria-pressed="true"/g) || []).length, 1)
    assert.match(switcher, /一键生成/); assert.match(switcher, /人工设置/)
  }
  assert.equal(calls, 0)
  console.log('PASS: one-click explicit scope, stable key, manual history preservation, progress/failed/cancelled gates, current-export identity, escaped UI and zero render requests')
} finally { globalThis.fetch = previousFetch; await server.close() }
