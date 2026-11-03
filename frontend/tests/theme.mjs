import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { createElement, Fragment } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import postcss from 'postcss'
import { createThemeStore, isThemePreference, localClockTheme, nextThemeBoundaryDelay, THEME_STORAGE_KEY } from '../src/theme.ts'

const localAt = (hour, minute = 0, second = 0, millis = 0, day = 3) => new Date(2026, 10, day, hour, minute, second, millis)
// Isolated local dates and fake timers only; no browser or computer clock is edited.
function fixture({ stored = null, at = localAt(12), denied = false, writeDenied = false } = {}) {
  let wall = at.getTime(), elapsed = 0, nextTimerId = 0, mediaReads = 0
  const listeners = new Map(), documentListeners = new Map(), timers = new Map(), reads = [], writes = []
  const listen = (map, type, listener) => { if (!map.has(type)) map.set(type, new Set()); map.get(type).add(listener) }
  const unlisten = (map, type, listener) => map.get(type)?.delete(listener)
  const emit = (map, type, event = {}) => [...(map.get(type) || [])].forEach(listener => listener(event))
  const storage = {
    getItem(key) { reads.push(key); assert.equal(key, THEME_STORAGE_KEY); return stored },
    setItem(key, value) { assert.equal(key, THEME_STORAGE_KEY); if (writeDenied) throw new Error('Read-only storage'); writes.push([key, value]); stored = value },
  }
  const target = {
    get localStorage() { if (denied) throw new Error('Storage is disabled'); return storage },
    document: { documentElement: { dataset: {} }, addEventListener(type, listener) { listen(documentListeners, type, listener) }, removeEventListener(type, listener) { unlisten(documentListeners, type, listener) } },
    matchMedia() { mediaReads++; throw new Error('OS appearance must not determine the clock theme') },
    addEventListener(type, listener) { listen(listeners, type, listener) },
    removeEventListener(type, listener) { unlisten(listeners, type, listener) },
  }
  const clock = {
    now: () => new Date(wall),
    setTimeout(callback, delay) { const id = nextTimerId++; timers.set(id, { due: elapsed + delay, callback }); return id },
    clearTimeout(id) { timers.delete(id) },
  }
  return {
    target, store: createThemeStore(target, clock), reads, writes, listeners, documentListeners, timers,
    get mediaReads() { return mediaReads },
    jump(date) { wall = date.getTime() },
    advance(milliseconds) {
      const end = elapsed + milliseconds
      let callbacks = 0
      while (timers.size) {
        const [id, pending] = [...timers].sort((a, b) => a[1].due - b[1].due)[0]
        if (pending.due > end) break
        assert.ok(++callbacks < 10_000, 'Timer must not spin without advancing time')
        wall += pending.due - elapsed; elapsed = pending.due
        timers.delete(id); pending.callback()
      }
      wall += end - elapsed; elapsed = end
    },
    event(type) { emit(type === 'visibilitychange' ? documentListeners : listeners, type) },
    external(value, key = THEME_STORAGE_KEY, area = storage) { stored = value; emit(listeners, 'storage', { key, newValue: value, storageArea: area }) },
  }
}

for (const [date, expected] of [[localAt(0), 'dark'], [localAt(6, 59, 59, 999), 'dark'], [localAt(7), 'light'], [localAt(18, 59, 59, 999), 'light'], [localAt(19), 'dark'], [localAt(23, 59), 'dark']]) {
  assert.equal(localClockTheme(date), expected, '07:00 is inclusive; 19:00 is exclusive for the local daytime interval')
}
assert.equal(nextThemeBoundaryDelay(localAt(6, 59, 59, 999)), 1)
assert.equal(nextThemeBoundaryDelay(localAt(18, 59, 59, 999)), 1)
assert.equal(nextThemeBoundaryDelay(localAt(19)), localAt(7, 0, 0, 0, 4) - localAt(19), 'The night boundary is the following local calendar morning')

const system = fixture({ at: localAt(6, 59, 59, 999) })
assert.deepEqual(system.store.getSnapshot(), { preference: 'system', resolvedTheme: 'dark' })
assert.equal(system.store.getSnapshot(), system.store.getSnapshot(), 'Snapshot identity remains stable between changes')
assert.equal(system.store.getServerSnapshot(), system.store.getServerSnapshot(), 'SSR uses one stable server snapshot')
let notifications = 0
const stopSystem = system.store.subscribe(() => notifications++)
assert.equal(system.target.document.documentElement.dataset.workspaceTheme, 'dark')
assert.equal(system.timers.size, 1)
system.advance(1)
assert.deepEqual(system.store.getSnapshot(), { preference: 'system', resolvedTheme: 'light' }, 'The 07:00 boundary switches without refresh')
assert.equal(notifications, 1)
const daytimeSnapshot = system.store.getSnapshot()
system.advance(60_000)
assert.equal(system.store.getSnapshot(), daytimeSnapshot, 'Clock recalibration does not publish identical snapshots')
system.jump(localAt(18, 59, 59, 999)); system.event('focus'); system.advance(1)
assert.equal(system.store.getSnapshot().resolvedTheme, 'dark', 'The 19:00 boundary switches automatically')
system.store.setPreference('light')
const manualSnapshot = system.store.getSnapshot(), beforeClockChange = notifications
assert.equal(system.timers.size, 0, 'An explicit manual preference has no clock timer')
system.jump(localAt(2)); system.event('focus'); system.event('pageshow'); system.event('visibilitychange'); system.advance(36 * 60 * 60 * 1000)
assert.equal(system.store.getSnapshot(), manualSnapshot, 'Manual appearance wins over clock changes and resume events')
assert.equal(notifications, beforeClockChange)
system.jump(localAt(22)); system.store.setPreference('system')
assert.equal(system.store.getSnapshot().resolvedTheme, 'dark')
assert.equal(system.timers.size, 1)
assert.deepEqual(system.writes.map(([, value]) => value), ['light', 'system'])
assert.equal(system.mediaReads, 0, 'OS prefers-color-scheme is never queried')
stopSystem()
assert.equal(system.timers.size, 0)
for (const type of ['storage', 'focus', 'pageshow']) assert.equal(system.listeners.get(type).size, 0)
assert.equal(system.documentListeners.get('visibilitychange').size, 0)

for (const event of ['focus', 'pageshow', 'visibilitychange']) {
  const waking = fixture({ at: localAt(12) }); const stop = waking.store.subscribe(() => {})
  waking.jump(localAt(23)); waking.event(event)
  assert.equal(waking.store.getSnapshot().resolvedTheme, 'dark', `${event} recalibrates after sleep, backgrounding or manual clock edits`)
  assert.equal(waking.timers.size, 1, 'Resuming replaces rather than duplicates the timer')
  stop()
}
const edited = fixture(); const stopEdited = edited.store.subscribe(() => {})
edited.jump(localAt(23)); edited.advance(60_000)
assert.equal(edited.store.getSnapshot().resolvedTheme, 'dark', 'An active-page clock edit is detected within a minute even without a browser event')
stopEdited()

for (const preference of ['light', 'dark', 'system']) {
  const saved = fixture({ stored: preference, at: localAt(23) })
  assert.equal(saved.store.getSnapshot().preference, preference)
  assert.equal(saved.store.getSnapshot().resolvedTheme, preference === 'light' ? 'light' : 'dark')
}
for (const invalid of ['', 'LIGHT', 'blue', '{}', 'null', '<script>']) {
  assert.equal(isThemePreference(invalid), false)
  const corrupt = fixture({ stored: invalid, at: localAt(23) })
  assert.equal(corrupt.store.getSnapshot().preference, 'system')
  assert.equal(corrupt.store.getSnapshot().resolvedTheme, 'dark')
  corrupt.store.setPreference(invalid)
  assert.equal(corrupt.writes.length, 0, 'Only the three allowed enum values can be persisted')
}
for (const restricted of [{ denied: true }, { writeDenied: true }]) {
  const blocked = fixture(restricted); const stop = blocked.store.subscribe(() => {})
  blocked.store.setPreference('dark'); assert.equal(blocked.store.getSnapshot().resolvedTheme, 'dark')
  stop()
  const stopAgain = blocked.store.subscribe(() => {})
  assert.equal(blocked.store.getSnapshot().preference, 'dark', 'Failed persistence does not discard this session choice on remount')
  blocked.store.setPreference('system'); blocked.jump(localAt(23)); blocked.event('pageshow')
  assert.equal(blocked.store.getSnapshot().resolvedTheme, 'dark', 'Blocked storage does not disable clock-based following')
  stopAgain(); assert.equal(blocked.timers.size, 0)
}

const shared = fixture(), listener = () => {}
const stopFirst = shared.store.subscribe(listener), stopSecond = shared.store.subscribe(listener)
assert.equal(shared.timers.size, 1, 'Multiple controls share one timer even with a repeated callback')
for (const type of ['storage', 'focus', 'pageshow']) assert.equal(shared.listeners.get(type).size, 1)
shared.external('dark'); assert.deepEqual(shared.store.getSnapshot(), { preference: 'dark', resolvedTheme: 'dark' })
assert.equal(shared.timers.size, 0)
shared.external('light', 'unrelated-key'); assert.equal(shared.store.getSnapshot().preference, 'dark')
shared.external('light', THEME_STORAGE_KEY, {}); assert.equal(shared.store.getSnapshot().preference, 'dark')
shared.external('invalid-value'); assert.equal(shared.store.getSnapshot().preference, 'system')
shared.jump(localAt(23)); shared.external(null, null)
assert.deepEqual(shared.store.getSnapshot(), { preference: 'system', resolvedTheme: 'dark' }, 'Clearing storage restores local-clock following')
const oldTimer = [...shared.timers.values()][0].callback
stopFirst(); stopFirst(); assert.equal(shared.timers.size, 1, 'An idempotent unsubscribe cannot detach a second subscriber')
stopSecond(); assert.equal(shared.timers.size, 0)
for (const type of ['storage', 'focus', 'pageshow']) assert.equal(shared.listeners.get(type).size, 0)
const stopRemounted = shared.store.subscribe(() => {})
const remountedSnapshot = shared.store.getSnapshot()
oldTimer()
assert.equal(shared.store.getSnapshot(), remountedSnapshot)
assert.equal(shared.timers.size, 1, 'A stale callback from StrictMode cleanup cannot create another timer')
stopRemounted(); assert.equal(shared.timers.size, 0)
for (let cycle = 0; cycle < 3; cycle++) { const stop = shared.store.subscribe(() => {}); assert.equal(shared.timers.size, 1); stop(); assert.equal(shared.timers.size, 0) }
assert.equal(shared.documentListeners.get('visibilitychange').size, 0)

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
  assert.match(html, /按电脑本地时间：07:00–19:00 日间，其余时间夜间/)
  assert.match(renderToStaticMarkup(createElement(ThemeSwitcher, { ...props, disabled: true })), /<fieldset[^>]*disabled=""/)
  assert.equal(requests, 0)
} finally {
  globalThis.fetch = previousFetch
  await server?.close()
}
console.log('PASS: local 07:00/19:00 clock boundaries; manual priority; automatic/resume clock calibration; stable snapshots; cross-tab/denied storage; shared/StrictMode timer cleanup; scoped contrast/radios; zero API/model requests')
