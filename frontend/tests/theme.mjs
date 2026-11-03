import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { createElement, Fragment } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import postcss from 'postcss'
import { createThemeStore, isThemePreference, THEME_STORAGE_KEY, THEME_MEDIA_QUERY } from '../src/theme.ts'

// Every store receives an isolated window. No real document or storage is read.
function fixture({ stored = null, dark = false, denied = false, writeDenied = false, legacyMedia = false, noMedia = false } = {}) {
  const mediaListeners = new Set(), storageListeners = new Set(), reads = [], writes = []
  const storage = {
    getItem(key) { reads.push(key); assert.equal(key, THEME_STORAGE_KEY); return stored },
    setItem(key, value) { assert.equal(key, THEME_STORAGE_KEY); if (writeDenied) throw new Error('Read-only storage'); writes.push([key, value]); stored = value },
  }
  const media = { get matches() { return dark } }
  if (legacyMedia) {
    media.addListener = listener => mediaListeners.add(listener)
    media.removeListener = listener => mediaListeners.delete(listener)
  } else {
    media.addEventListener = (type, listener) => { assert.equal(type, 'change'); mediaListeners.add(listener) }
    media.removeEventListener = (type, listener) => { assert.equal(type, 'change'); mediaListeners.delete(listener) }
  }
  const target = {
    get localStorage() { if (denied) throw new Error('Storage is disabled'); return storage },
    document: { documentElement: { dataset: {} } },
    matchMedia(query) { assert.equal(query, THEME_MEDIA_QUERY); if (noMedia) throw new Error('No media API'); return media },
    addEventListener(type, listener) { assert.equal(type, 'storage'); storageListeners.add(listener) },
    removeEventListener(type, listener) { assert.equal(type, 'storage'); storageListeners.delete(listener) },
  }
  return {
    target, store: createThemeStore(target), reads, writes, mediaListeners, storageListeners,
    system(value) { dark = value; [...mediaListeners].forEach(listener => listener({ matches: value })) },
    external(value, key = THEME_STORAGE_KEY, area = storage) { stored = value; [...storageListeners].forEach(listener => listener({ key, newValue: value, storageArea: area })) },
  }
}

const system = fixture()
assert.deepEqual(system.store.getSnapshot(), { preference: 'system', resolvedTheme: 'light' })
assert.equal(system.store.getSnapshot(), system.store.getSnapshot(), 'Snapshot identity remains stable between changes')
let notifications = 0
const stopSystem = system.store.subscribe(() => notifications++)
assert.equal(system.target.document.documentElement.dataset.workspaceTheme, 'light')
assert.equal(system.mediaListeners.size, 1)
assert.equal(system.storageListeners.size, 1)
system.system(true)
assert.deepEqual(system.store.getSnapshot(), { preference: 'system', resolvedTheme: 'dark' })
system.store.setPreference('light')
const manualSnapshot = system.store.getSnapshot(), beforeSystemChange = notifications
system.system(false); system.system(true)
assert.equal(system.store.getSnapshot(), manualSnapshot, 'System changes cannot override an explicit choice')
assert.equal(notifications, beforeSystemChange)
system.store.setPreference('system')
assert.equal(system.store.getSnapshot().resolvedTheme, 'dark')
system.system(false)
assert.equal(system.store.getSnapshot().resolvedTheme, 'light')
assert.deepEqual(system.writes.map(([, value]) => value), ['light', 'system'])
stopSystem()
assert.equal(system.mediaListeners.size, 0)
assert.equal(system.storageListeners.size, 0)
const detachedSnapshot = system.store.getSnapshot()
system.system(true)
assert.equal(system.store.getSnapshot(), detachedSnapshot, 'Unmounted stores have no active media callback')

for (const preference of ['light', 'dark', 'system']) {
  const saved = fixture({ stored: preference, dark: true })
  assert.equal(saved.store.getSnapshot().preference, preference)
  assert.equal(saved.store.getSnapshot().resolvedTheme, preference === 'light' ? 'light' : 'dark')
}
for (const invalid of ['', 'LIGHT', 'blue', '{}', 'null', '<script>']) {
  assert.equal(isThemePreference(invalid), false)
  const corrupt = fixture({ stored: invalid, dark: true })
  assert.equal(corrupt.store.getSnapshot().preference, 'system')
  assert.equal(corrupt.store.getSnapshot().resolvedTheme, 'dark')
  corrupt.store.setPreference(invalid)
  assert.equal(corrupt.writes.length, 0, 'Only the three allowed enum values can be persisted')
}
for (const restricted of [{ denied: true }, { writeDenied: true }]) {
  const blocked = fixture(restricted)
  const stop = blocked.store.subscribe(() => {})
  blocked.store.setPreference('dark')
  assert.equal(blocked.store.getSnapshot().resolvedTheme, 'dark', 'Storage denial keeps the current session usable')
  stop()
  const stopAgain = blocked.store.subscribe(() => {})
  assert.equal(blocked.store.getSnapshot().preference, 'dark', 'Failed persistence does not discard the in-memory choice on remount')
  stopAgain()
}
const unavailable = fixture({ noMedia: true })
const stopUnavailable = unavailable.store.subscribe(() => {})
assert.equal(unavailable.store.getSnapshot().resolvedTheme, 'light', 'Missing media API has a deterministic readable fallback')
unavailable.store.setPreference('dark')
assert.equal(unavailable.store.getSnapshot().resolvedTheme, 'dark')
stopUnavailable()

const shared = fixture({ legacyMedia: true })
const stopFirst = shared.store.subscribe(() => {}), stopSecond = shared.store.subscribe(() => {})
assert.equal(shared.mediaListeners.size, 1, 'Multiple controls share a single media subscription')
assert.equal(shared.storageListeners.size, 1)
shared.external('dark')
assert.deepEqual(shared.store.getSnapshot(), { preference: 'dark', resolvedTheme: 'dark' })
shared.external('light', 'unrelated-key')
assert.equal(shared.store.getSnapshot().preference, 'dark', 'Unrelated browser data does not affect appearance')
shared.external('light', THEME_STORAGE_KEY, {})
assert.equal(shared.store.getSnapshot().preference, 'dark', 'A sessionStorage event cannot replace the localStorage preference')
shared.external('invalid-value')
assert.equal(shared.store.getSnapshot().preference, 'system')
shared.system(true)
shared.external(null, null)
assert.deepEqual(shared.store.getSnapshot(), { preference: 'system', resolvedTheme: 'dark' }, 'Cleared storage restores live system following')
stopFirst()
assert.equal(shared.mediaListeners.size, 1)
stopSecond()
assert.equal(shared.mediaListeners.size, 0, 'Last subscriber cleans up legacy media callbacks too')
assert.equal(shared.storageListeners.size, 0)

const css = readFileSync(new URL('../src/theme.css', import.meta.url), 'utf8')
const sheet = postcss.parse(css)
const selectors = []
sheet.walkRules(rule => selectors.push(rule.selector))
for (const selector of selectors) assert.match(selector, /\.cinematic-studio|\.mobile-history-panel|\.navigation-confirm-panel|\.navigation-confirm-overlay|\.mobile-history-overlay/, `Theme rule must stay inside workspace/dialog scopes: ${selector}`)
assert.doesNotMatch(css.replace(/\/\*[\s\S]*?\*\//g, ''), /\.cinematic-entry|\.entry-|(?:^|[},])\s*(?:body|html|#root)\s*\{/m, 'No home image, title or global page palette override')
for (const selector of ['.object-section', '.style-recommendation', '.scope-note', '.asset-method', '.director-canvas.mood-low', '.director-canvas.mood-high', '.navigation-confirm-actions', '.mobile-history-panel', '[aria-invalid="true"]', ':disabled', '::placeholder', ':focus-visible']) assert.ok(css.includes(selector), `Cover ${selector}`)
sheet.walkRules(rule => {
  if (rule.selector.includes('.scene-object')) rule.walkDecls(decl => assert.ok(['color', 'background', 'border-color', 'box-shadow'].includes(decl.prop), 'Theme must not move or resize scene objects'))
})
const palette = new Map()
sheet.walkRules(rule => {
  if (rule.selector === ':root[data-workspace-theme="light"] :is(.cinematic-studio, .mobile-history-panel, .navigation-confirm-panel)') rule.walkDecls(decl => palette.set(decl.prop, decl.value))
})
function luminance(hex) {
  const rgb = hex.slice(1).match(/../g).map(value => parseInt(value, 16) / 255).map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4)
  return .2126 * rgb[0] + .7152 * rgb[1] + .0722 * rgb[2]
}
function contrast(a, b) { const values = [luminance(a), luminance(b)].sort((x, y) => y - x); return (values[0] + .05) / (values[1] + .05) }
for (const text of ['--cinema-text', '--cinema-secondary', '--cinema-muted', '--cinema-amber', '--cinema-danger', '--cinema-good']) {
  for (const surface of ['--cinema-bg', '--cinema-side', '--cinema-surface', '--cinema-raised', '--cinema-field']) assert.ok(contrast(palette.get(text), palette.get(surface)) >= 4.5, `${text} on ${surface} meets 4.5:1 text contrast`)
}
assert.ok(contrast(palette.get('--cinema-control'), palette.get('--cinema-field')) >= 3, 'Native field borders meet 3:1 contrast')
assert.ok(contrast('#687175', palette.get('--cinema-field')) >= 4.5, 'Form placeholders remain readable')

let requests = 0
const previousFetch = globalThis.fetch
globalThis.fetch = async () => { requests++; throw new Error('Theme rendering must not submit API/model requests') }
let server
try {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), server: { middlewareMode: true, hmr: false }, appType: 'custom' })
  const { default: ThemeSwitcher } = await server.ssrLoadModule('/src/ThemeSwitcher.tsx')
  const props = { preference: 'system', resolvedTheme: 'dark', setPreference() { throw new Error('Rendering must not mutate preferences') } }
  const html = renderToStaticMarkup(createElement(Fragment, null, createElement(ThemeSwitcher, props), createElement(ThemeSwitcher, props)))
  const radioTags = html.match(/<input\b[^>]*>/g)
  assert.equal(radioTags.length, 6)
  assert.ok(radioTags.every(tag => tag.includes('type="radio"')))
  assert.equal(radioTags.filter(tag => tag.includes('checked=""')).length, 2)
  const names = [...html.matchAll(/name="([^"]+)"/g)].map(match => match[1])
  assert.equal(new Set(names.slice(0, 3)).size, 1)
  assert.equal(new Set(names.slice(3)).size, 1)
  assert.notEqual(names[0], names[3], 'Toolbar and settings controls must not join the same browser radio group')
  assert.match(html, /<legend>工作台外观<\/legend>/)
  for (const label of ['日间', '夜间', '跟随系统']) assert.ok(html.includes(label))
  assert.match(renderToStaticMarkup(createElement(ThemeSwitcher, { ...props, disabled: true })), /<fieldset[^>]*disabled=""/)
  assert.equal(requests, 0)
} finally {
  globalThis.fetch = previousFetch
  await server?.close()
}
console.log('PASS: isolated enum persistence/fallback; live system changes; cross-tab sync; listener cleanup; native independent radio groups; scoped light surfaces/contrast; zero API/model requests')
