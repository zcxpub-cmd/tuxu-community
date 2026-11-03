import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import { builtinDraftChanged, builtinModels, canReviewAsset, visualReviewActive, visibleReviewChecks } from '../src/builtinAgent.ts'
import { createCatalogGate } from '../src/catalogGate.ts'
import { taskStage, taskStatusPresentation } from '../src/workflow.ts'

const catalog = { runtime: 'tuxu-builtin', model: 'qwen2.5vl:7b', visual_review_enabled: false, revision: 'r1', roles: ['article_director', 'prompt_author', 'visual_reviewer'], limits: { max_images: 5, generation_attempts_per_image: 1, visual_reviews_per_image: 1, text_repairs_per_step: 1 } }
const asset = { id: 'image-one', slot_id: 'slot-one', stale: false }
const task = { id: 'synthetic', status: 'review', plan: { model: 'frozen-local-model' }, approval: { id: 'approved-one' }, assets: [asset], jobs: [] }

const halted = { ...task, status: 'cancelled', automation: { status: 'paused', stage: 'visual_review', message: '图片检查需要人工处理' } }
const haltedSnapshot = structuredClone(halted)
assert.deepEqual(taskStatusPresentation(halted), { label: '需人工处理', tone: 'orange' }, 'An uncertain image check pauses for review rather than claiming that the user cancelled')
assert.deepEqual(halted, haltedSnapshot, 'Status projection cannot rewrite the durable safety-halt state')
assert.deepEqual(taskStatusPresentation({ ...halted, automation: { ...halted.automation, status: 'cancelled' } }), { label: '已取消', tone: 'green' }, 'Explicit user cancellation remains cancelled')
assert.equal(taskStatusPresentation({ ...task, status: 'cancelled' }).label, '已取消', 'Historical cancelled tasks retain their label')
assert.equal(taskStatusPresentation({ ...halted, status: 'exported' }).label, '已导出', 'A later real manual export is not hidden by the old paused automation receipt')

for (const [status, label] of [['analyzing', '正在阅读'], ['plan_ready', '方案待确认'], ['approved', '已授权'], ['generating', '正在生成']]) {
  assert.equal(taskStatusPresentation({ ...halted, status }).label, label, 'Historical paused automation cannot replace the current manual workflow state')
}


assert.deepEqual(builtinModels(catalog, ['qwen2.5vl:7b', 'another-local', 'another-local', '']), ['qwen2.5vl:7b', 'another-local'])
assert.deepEqual(builtinModels(catalog), ['qwen2.5vl:7b'], 'Missing model inventory preserves the configured model')
assert.deepEqual(builtinModels(undefined), [])
assert.equal(builtinDraftChanged(catalog, { model: catalog.model, visual_review_enabled: false, expected_revision: 'r1' }), false)
assert.equal(builtinDraftChanged(catalog, { model: catalog.model, visual_review_enabled: true, expected_revision: 'r1' }), true)
assert.equal(builtinDraftChanged(catalog, { model: 'another-local', visual_review_enabled: false, expected_revision: 'r1' }), true)
assert.equal(canReviewAsset(task, asset, false), true)
assert.equal(canReviewAsset(task, asset, true), false)
for (const patch of [{ plan: null }, { approval: null }, { approval: {} }, { status: 'cancelled' }, { assets: [] }, { jobs: [{ status: 'queued' }] }, { jobs: [{ status: 'running' }] }, { jobs: [{ status: 'awaiting_agent' }] }, { automation: { status: 'running' } }, { automation: { status: 'waiting' } }]) assert.equal(canReviewAsset({ ...task, ...patch }, asset, false), false)
assert.equal(canReviewAsset(task, { ...asset, stale: true }, false), false)
for (const status of ['queued', 'running', 'passed', 'needs_attention', 'unavailable', 'interrupted']) {
  const current = { ...asset, visual_review: { status, checks: [], suggestions: [], model: 'frozen-local-model' } }
  const reviewedTask = { ...task, assets: [current] }
  assert.equal(canReviewAsset(reviewedTask, current, false), false, 'No automatic retry or second review for an existing receipt')
  assert.equal(canReviewAsset(reviewedTask, asset, false), false, 'An old unreviewed UI object cannot authorize the same current reviewed asset')
  assert.equal(visualReviewActive(reviewedTask), ['queued', 'running'].includes(status))
}
assert.equal(visualReviewActive(task), false)
for (const status of ['queued', 'running']) assert.equal(taskStage({ ...task, status: 'exported', assets: [{ ...asset, visual_review: { status } }] }), 4, 'An in-flight image review reopens its own stage, not historical export history')
const gate = createCatalogGate()
gate.publish(catalog)
const oldRead = gate.capture()
gate.publish(catalog) // save intent invalidates old reads
const changed = { ...catalog, revision: 'r2', visual_review_enabled: true }
gate.publish(changed)
assert.equal(gate.publish(catalog, oldRead), false, 'Late bootstrap cannot restore disabled image checks after a confirmed save')
assert.equal(gate.latest(), changed)
assert.deepEqual(visibleReviewChecks({ ...asset, visual_review: { checks: [{ category: 'raw_prompt', verdict: 'match' }, { category: 'subject', verdict: 'injected' }, { category: 'subject', verdict: 'mismatch' }] } }), [{ category: 'subject', verdict: 'mismatch' }])

const previousFetch = globalThis.fetch
let requests = 0
globalThis.fetch = async () => { requests++; throw new Error('SSR must not submit a model operation') }
const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' })
try {
  const { default: Settings } = await server.ssrLoadModule('/src/BuiltinAgentSettings.tsx')
  const { default: Review } = await server.ssrLoadModule('/src/VisualReviewPanel.tsx')
  const { default: OneClick } = await server.ssrLoadModule('/src/OneClickPanel.tsx')
  const renderSettings = value => renderToStaticMarkup(createElement(Settings, { catalog: value, models: ['qwen2.5vl:7b'], available: true, locked: false, onSave: async () => true }))
  const off = renderSettings(catalog)
  for (const text of ['图序内置视觉导演', '三个角色共用一个本地模型', '推荐开启', '只对新授权生效', '不会自动重画', 'role="switch"', '保存导演设置']) assert.ok(off.includes(text), text)
  assert.doesNotMatch(off, /checked=""/)
  const on = renderSettings(changed)
  assert.match(on, /checked=""/)
  assert.match(on, /图片检查已启用/)
  const locked = renderToStaticMarkup(createElement(Settings, { catalog, available: true, locked: true, onSave: async () => true }))
  assert.match(locked, /fieldset disabled=""/)
  const renderReview = value => renderToStaticMarkup(createElement(Review, { asset: value, model: 'frozen-local-model', canReview: true, onReview() {} }))
  const initial = renderReview(asset)
  assert.match(initial, /由本地 frozen-local-model 检查原图/)
  assert.match(initial, /授权.*原图.*本地模型检查一次/)
  assert.match(initial, /检查这张图（本地）/)
  const needs = renderReview({ ...asset, visual_review: { status: 'needs_attention', model: 'frozen-local-model', checks: [{ category: 'subject', verdict: 'mismatch' }, { category: 'count', verdict: 'uncertain' }], suggestions: ['FULL_PRIVATE_PROMPT_DO_NOT_RENDER'] } })
  for (const text of ['需要人工检查', '有差异', '待核对', '请核对主体', '请核对物体数量']) assert.ok(needs.includes(text), text)
  assert.doesNotMatch(needs, /FULL_PRIVATE_PROMPT|检查这张图（本地）/)
  for (const status of ['unavailable', 'interrupted']) {
    const uncertain = renderReview({ ...asset, visual_review: { status, model: 'frozen-local-model', checks: [], suggestions: [] } })
    assert.match(uncertain, /请人工检查/)
    assert.match(uncertain, /不会自动再次检查或重画/)
    assert.doesNotMatch(uncertain, /检查这张图（本地）|检查通过/)
  }
  const running = renderReview({ ...asset, visual_review: { status: 'running', model: 'frozen-local-model', checks: [], suggestions: [] } })
  assert.match(running, /正在查看图片/)
  assert.doesNotMatch(running, /检查通过|检查这张图（本地）/)
  const passed = renderReview({ ...asset, visual_review: { status: 'passed', model: 'frozen-local-model', checks: [{ category: 'subject', verdict: 'match' }], suggestions: [] } })
  assert.match(passed, /仍需人工核对内容与事实/)
  const escaped = renderReview({ ...asset, visual_review: { status: 'injected-status', model: '<script>model</script>', checks: [{ category: 'injected-category', verdict: 'mismatch' }], suggestions: ['<script>prompt</script>'] } })
  assert.match(escaped, /&lt;script&gt;model/)
  assert.doesNotMatch(escaped, /injected-category|injected-status|<script>|prompt/)
  const checking = renderToStaticMarkup(createElement(OneClick, { task: { ...task, exports: [], document: {}, automation: { status: 'waiting', stage: 'visual_review', message: '本地图片检查中', completed_images: 1, total_images: 1 } }, format: 'docx', onFormat() {}, providerLabel: '图片服务', textModel: 'frozen-local-model', ready: true, readiness: '', busy: false, onStart() {}, onManual() {}, children: null }))
  assert.match(checking, /检查画面/)
  assert.match(checking, /aria-current="step"/)
  assert.doesNotMatch(checking, /图文文档已就绪|one-click-download/)
  assert.equal(requests, 0)
  console.log('PASS: configuration revision gate, default-off disclosure, current-image/once-only review gates, pending polling state, fixed verdicts/suggestions, uncertain manual handoff and no SSR inference')
} finally { globalThis.fetch = previousFetch; await server.close() }
