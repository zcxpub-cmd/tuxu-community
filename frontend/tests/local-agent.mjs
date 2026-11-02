import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import { resolveGenerationKey } from '../src/generationKey.ts'
import { activeJobStates, activeTaskStates, targetReady } from '../src/imageProviders.ts'
import { taskStage } from '../src/workflow.ts'

const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
const previousFetch = globalThis.fetch
let requests = 0
globalThis.fetch = async () => { requests++; throw new Error('Rendering must not submit requests') }
try {
  const { default: LocalAgentSettings } = await server.ssrLoadModule('/src/LocalAgentSettings.tsx')
  const status = { available: true, enabled: false, mode: 'runner', label: '本机智能体', message: '已连接固定本机生图程序。' }
  const render = (value, locked = false) => renderToStaticMarkup(createElement(LocalAgentSettings, { status: value, locked, onRefresh: async () => {} }))
  assert.match(render(undefined), /未连接/)
  assert.match(render(undefined), /type="checkbox" disabled=""/)
  assert.match(render({ ...status, available: false }), /type="checkbox" disabled=""/)
  assert.match(render(status), /已连接 · 未启用/)
  assert.doesNotMatch(render(status), /type="checkbox" disabled=/, 'A configured agent can be enabled')
  assert.match(render({ ...status, enabled: true }), /已启用/)
  assert.match(render({ ...status, enabled: true }), /type="checkbox" checked=""/)
  assert.match(render(status, true), /type="checkbox" disabled=""/, 'An active task prevents changing the generation connection')
  assert.doesNotMatch(render({ ...status, available: false, enabled: true }), /type="checkbox" disabled=/, 'A stale enabled connection can still be disabled')
  assert.match(render(status), /文章分析及提示词由本地模型完成/)
  assert.match(render(status), /图像实际算力由智能体提供方决定/)
  assert.match(render(status), /不代表离线免费/)
  assert.equal((render(status).match(/<input/g) || []).length, 1, 'The UI exposes only an enable switch, not arbitrary command/path/key fields')
  assert.equal(requests, 0, 'Showing or rendering connection status cannot start generation')
} finally { globalThis.fetch = previousFetch; await server.close() }

const pending = { id: 'job', kind: 'generation', status: 'awaiting_agent', approval_id: 'approved', plan_version: 1, idempotency_key: 'durable-key', submissions: [{ state: 'submitted' }] }
for (const status of ['awaiting_agent', 'failed', 'cancelled']) {
  assert.equal(resolveGenerationKey([{ ...pending, status }], 1, 'approved', undefined, () => { throw new Error('Must not allocate another image request') }), 'durable-key')
}
assert.ok(activeTaskStates.includes('awaiting_agent') && activeJobStates.includes('awaiting_agent'))
assert.equal(taskStage({ status: 'awaiting_agent', jobs: [pending], exports: [{ filename: 'old.docx' }] }), 4, 'A restarted waiting task returns to image status rather than a stale export')
assert.equal(targetReady('codex', true, { profiles: [], built_in: { ready: false } }), false, 'Local image readiness cannot mask an unavailable agent')
const component = readFileSync(new URL('../src/LocalAgentSettings.tsx', import.meta.url), 'utf8')
assert.match(component, /request<LocalAgentStatus>\('\/local-agent', 'POST', \{ enabled \}\)/)
assert.doesNotMatch(component, /err\.message|error\.message|localStorage|sessionStorage/)
console.log('PASS: unavailable/disabled/enabled/locked agent UI; one enable switch, zero render requests; explicit local prompt and compute scope; waiting/cancel/unknown durable-key recovery')
