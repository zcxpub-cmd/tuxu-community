import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const previousFetch = globalThis.fetch
let requests = 0
globalThis.fetch = async () => { requests++; throw new Error('Rendering a size notice must not submit requests') }
const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' })
try {
  const { NativeImageSizeNotice, NativeImageConfirmation } = await server.ssrLoadModule('/src/App.tsx')
  const renderNotice = asset => renderToStaticMarkup(createElement(NativeImageSizeNotice, { notice: asset.native_size_notice }))
  const notice = '原生图片实际为1536×1024，与请求尺寸不同；完整图片已保留。'
  const current = { native_size_notice: notice, prompt: 'PRIVATE_RENDER_SENTINEL' }
  assert.equal(renderNotice(current), `<p class="scope-note">${notice}</p>`, 'Show the public native-size explanation using the existing readable note style')
  for (const asset of [{}, { native_size_notice: undefined }, { native_size_notice: null }, { native_size_notice: '' }, { native_size_notice: ' \n\t' }]) {
    assert.equal(renderNotice(asset), '', 'Old assets and absent or blank notices add no empty message')
  }
  const escaped = renderNotice({ native_size_notice: '<img src=x onerror=alert(1)>' })
  assert.match(escaped, /&lt;img/)
  assert.doesNotMatch(escaped, /<img/, 'Public notice text is escaped rather than injected as HTML')
  assert.doesNotMatch(renderNotice(current), /PRIVATE_RENDER_SENTINEL/, 'The notice never renders private generation fields')

  const renderConfirmation = provider => renderToStaticMarkup(createElement(NativeImageConfirmation, { provider }))
  const confirmation = renderConfirmation('codex')
  assert.match(confirmation, /与请求不同的像素尺寸/)
  assert.match(confirmation, /横向、纵向或方形方向/)
  assert.match(confirmation, /完整保留图片/)
  assert.match(confirmation, /选图时显示实际尺寸/)
  for (const provider of ['local', 'ark', 'openai']) assert.equal(renderConfirmation(provider), '', 'The native tool explanation applies only to the Codex provider')

  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
  const methodIndex = app.indexOf('className="asset-method"')
  const noticeIndex = app.indexOf('<NativeImageSizeNotice notice={asset.native_size_notice} />')
  const actionsIndex = app.indexOf('className="asset-actions"')
  assert.ok(methodIndex >= 0 && noticeIndex > methodIndex && actionsIndex > noticeIndex, 'The asset notice follows processing information and precedes actions; review guidance may appear between the notice and actions')
  assert.match(app, /<NativeImageConfirmation provider=\{provider\} \/>/, 'The confirmation area supplies the selected provider')
  assert.match(app, /原图 \{asset\.width\} × \{asset\.height\}/, 'Image selection continues to show actual asset dimensions')
  assert.equal(requests, 0, 'Neither explanation starts a request')
  console.log('PASS: native-size note text/escaping; absent/blank/legacy assets; Codex-only confirmation; actual dimensions and zero render requests')
} finally {
  globalThis.fetch = previousFetch
  await server.close()
}
