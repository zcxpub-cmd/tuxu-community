import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { chooseSceneForVisit, getInitialScene, pickVisitScene, scenes } from '../src/sceneLibrary.ts'

const legacyIds = ['blackhole', 'fold', 'stellar', 'city', 'field', 'mountain', 'ocean', 'dna']
const newIds = ['street', 'village', 'forest', 'lake', 'desert', 'rice', 'milkyway', 'cells', 'molecules', 'garden']
const ids = [...legacyIds, ...newIds]
const sceneCount = scenes.length
assert.deepEqual(scenes.map(scene => scene.id), ids)
assert.equal(new Set(scenes.map(scene => scene.url)).size, sceneCount)
assert.ok(scenes.every(scene => scene.url === `/visuals/${scene.id}.webp` && (scene.id === 'dna' ? scene.name === 'DNA' : /[\u4e00-\u9fff]/u.test(scene.name))))
assert.ok(scenes.every(scene => scene.videoUrl === `/visuals/${scene.id}.mp4`))
assert.deepEqual(scenes.filter(scene => scene.fadeLoop).map(scene => scene.id), newIds)
assert.ok(Object.isFrozen(scenes) && scenes.every(Object.isFrozen), 'The shared catalog cannot be mutated by consumers')

function memoryStorage(initial = null) {
  let value = initial
  const writes = []
  return {
    writes,
    getItem: () => value,
    setItem(key, next) { writes.push({ key, value: next }); value = next },
    get value() { return value },
  }
}

// Different random streams must retain both deck coverage and boundary exclusion.
let seed = 47
const seeded = () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 2 ** 32 }
const randoms = [() => 0, () => 0.99999999, () => 0.42, seeded]
const firstOrders = new Set()
for (const random of randoms) {
  const storage = memoryStorage()
  const visits = Array.from({ length: sceneCount * 30 }, () => chooseSceneForVisit(storage, random).id)
  for (let start = 0; start < visits.length; start += sceneCount) {
    assert.equal(new Set(visits.slice(start, start + sceneCount)).size, sceneCount, 'Each complete deck includes every scene once')
  }
  for (let index = 1; index < visits.length; index++) assert.notEqual(visits[index], visits[index - 1], 'Adjacent visits cannot repeat, including deck boundaries')
  firstOrders.add(visits.slice(0, sceneCount).join(','))
  for (const write of storage.writes) {
    const saved = JSON.parse(write.value)
    assert.equal(write.key, 'tuxu.ui.scene-visits.v1')
    assert.deepEqual(Object.keys(saved).sort(), ['last', 'remaining', 'v'])
    assert.equal(saved.v, 2)
    assert.ok(ids.includes(saved.last) && saved.remaining.every(id => ids.includes(id)), 'Storage contains UI scene IDs only')
    assert.equal(new Set(saved.remaining).size, saved.remaining.length)
    assert.ok(!saved.remaining.includes(saved.last))
  }
}
assert.ok(firstOrders.size > 1, 'Randomness changes the visit order rather than a fixed rotation')

// A fresh module/document can continue a persisted partial deck.
const persisted = memoryStorage()
const firstPart = Array.from({ length: 3 }, () => chooseSceneForVisit(persisted, () => 0).id)
const reopened = memoryStorage(persisted.value)
const nextPart = Array.from({ length: sceneCount - firstPart.length }, () => chooseSceneForVisit(reopened, () => 0.8).id)
assert.equal(new Set([...firstPart, ...nextPart]).size, sceneCount)
assert.notEqual(firstPart.at(-1), nextPart[0])

// Upgrading a partially used eight-scene deck reshuffles all scenes without repeating the last one.
const legacyStorage = memoryStorage(JSON.stringify({ v: 1, last: 'blackhole', remaining: ['city', 'dna'] }))
const upgradedVisits = Array.from({ length: sceneCount }, () => chooseSceneForVisit(legacyStorage, () => 0.99999).id)
assert.notEqual(upgradedVisits[0], 'blackhole')
assert.deepEqual(upgradedVisits.slice().sort(), ids.slice().sort())

// Corrupt or obsolete local state must fail safely, never leak arbitrary IDs.
const badStates = [
  'not JSON', 'null', '[]', '123', 'x'.repeat(2049),
  JSON.stringify({ v: 2, last: 'missing', remaining: ['unknown'] }),
  JSON.stringify({ v: 1, last: 'blackhole', remaining: ['city'] }),
  JSON.stringify({ v: 2, last: 'blackhole', remaining: ['city', 'city'] }),
  JSON.stringify({ v: 2, last: 'blackhole', remaining: ['blackhole'] }),
  JSON.stringify({ v: 2, last: 'blackhole', remaining: ['unknown'] }),
  JSON.stringify({ v: 2, last: 'blackhole', remaining: [false] }),
  JSON.stringify({ v: 2, last: 'blackhole', remaining: ids }),
  JSON.stringify({ v: 2, last: 'blackhole', remaining: null }),
]
for (const raw of badStates) {
  const storage = memoryStorage(raw)
  const scene = chooseSceneForVisit(storage, () => 0.99999)
  assert.ok(ids.includes(scene.id))
  if (raw.includes('"last":"blackhole"')) assert.notEqual(scene.id, 'blackhole', 'A recoverable last ID survives damaged-deck recovery')
  const clean = JSON.parse(storage.value)
  assert.equal(clean.v, 2)
  assert.equal(new Set(clean.remaining).size, sceneCount - 1)
}
for (const random of [() => -10, () => 1, () => Infinity, () => NaN, () => { throw Error('rng unavailable') }]) {
  const storage = memoryStorage()
  const visits = Array.from({ length: sceneCount * 2 }, () => chooseSceneForVisit(storage, random).id)
  assert.equal(new Set(visits.slice(0, sceneCount)).size, sceneCount)
  assert.equal(new Set(visits.slice(sceneCount)).size, sceneCount)
  assert.notEqual(visits[sceneCount - 1], visits[sceneCount])
}
assert.ok(ids.includes(chooseSceneForVisit().id))
assert.ok(ids.includes(chooseSceneForVisit({ getItem() { throw Error('read denied') }, setItem() { throw Error('write denied') } }).id))

const oldWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
const originalRandom = Math.random
const setWindow = browser => Object.defineProperty(globalThis, 'window', { configurable: true, writable: true, value: browser })
try {
  delete globalThis.window
  let draws = 0
  Math.random = () => { draws++; return 0 }
  for (let i = 0; i < 4; i++) {
    assert.equal(getInitialScene().id, 'blackhole')
    assert.equal(pickVisitScene().id, 'blackhole')
  }
  assert.equal(draws, 0, 'SSR fallback neither consumes randomness nor caches a browser initial scene')

  const browserStorage = memoryStorage()
  setWindow({ document: {}, localStorage: browserStorage })
  const initial = getInitialScene()
  const initialDraws = draws
  assert.equal(browserStorage.writes.length, 1)
  assert.equal(getInitialScene(), initial, 'StrictMode duplicate initializer returns the same scene object')
  assert.equal(getInitialScene(), initial)
  assert.equal(draws, initialDraws)
  assert.equal(browserStorage.writes.length, 1, 'Repeated initialization cannot consume a second card')
  const next = pickVisitScene()
  assert.notEqual(next.id, initial.id)
  assert.equal(browserStorage.writes.length, 2, 'Only an explicit new visit consumes the next card')
  assert.equal(getInitialScene(), initial, 'The document initial choice remains stable after a later visit')
  assert.equal(browserStorage.writes.length, 2)

  // New document means a new initial draw, continuing the persisted deck.
  setWindow({ document: {}, localStorage: browserStorage })
  const reopenedInitial = getInitialScene()
  assert.notEqual(reopenedInitial.id, next.id)
  assert.equal(browserStorage.writes.length, 3)

  // Blocked storage and quota failure still retain a no-repeat in-memory deck.
  for (const localStorage of [
    { getItem() { throw Error('blocked') }, setItem() { throw Error('blocked') } },
    { getItem() { return null }, setItem() { throw Error('quota') } },
    { getItem() { return JSON.stringify({ v: 2, last: 'blackhole', remaining: [] }) }, setItem() { throw Error('quota with stale state') } },
  ]) {
    setWindow({ document: {}, localStorage })
    const visits = [getInitialScene().id, ...Array.from({ length: sceneCount * 3 - 1 }, () => pickVisitScene().id)]
    for (let start = 0; start < visits.length; start += sceneCount) assert.equal(new Set(visits.slice(start, start + sceneCount)).size, sceneCount)
    for (let index = 1; index < visits.length; index++) assert.notEqual(visits[index], visits[index - 1])
  }
  const blockedGetter = { document: {} }
  Object.defineProperty(blockedGetter, 'localStorage', { get() { throw Error('SecurityError') } })
  setWindow(blockedGetter)
  assert.notEqual(getInitialScene().id, pickVisitScene().id)

  // Test the real Vite SSR transform without opening an HTTP server or browser.
  delete globalThis.window
  const { createServer } = await import('vite')
  const server = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)), configFile: false, appType: 'custom',
    server: { middlewareMode: true, hmr: false, watch: null },
    optimizeDeps: { noDiscovery: true, include: [] },
  })
  try {
    assert.equal(server.httpServer, null)
    const ssr = await server.ssrLoadModule('/src/sceneLibrary.ts')
    const beforeSSR = draws
    assert.equal(ssr.getInitialScene().id, 'blackhole')
    assert.equal(ssr.pickVisitScene().id, 'blackhole')
    assert.equal(draws, beforeSSR)
    const storage = memoryStorage()
    setWindow({ document: {}, localStorage: storage })
    assert.equal(ssr.getInitialScene(), ssr.getInitialScene())
    assert.equal(storage.writes.length, 1, 'Vite-transformed StrictMode initializers consume only one card')
  } finally {
    await server.close()
  }
} finally {
  Math.random = originalRandom
  if (oldWindow) Object.defineProperty(globalThis, 'window', oldWindow)
  else delete globalThis.window
}

console.log(`PASS: ${sceneCount}-scene random decks including DNA, boundary exclusion, persisted recovery, corrupt/blocked storage, SSR, StrictMode document cache, explicit re-entry and Vite SSR transform; no timers or UI automation`)
