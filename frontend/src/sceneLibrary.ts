/** UI-only visit backgrounds. No article data, provider state or model calls. */
export type Scene = Readonly<{ id: string; name: string; url: string; videoUrl?: string; fadeLoop?: boolean }>

export const scenes: readonly Scene[] = Object.freeze([
  { id: 'blackhole', name: '黑洞', url: '/visuals/blackhole.webp', videoUrl: '/visuals/blackhole.mp4' },
  { id: 'fold', name: '折光', url: '/visuals/fold.webp', videoUrl: '/visuals/fold.mp4' },
  { id: 'stellar', name: '星际', url: '/visuals/stellar.webp', videoUrl: '/visuals/stellar.mp4' },
  { id: 'city', name: '城市', url: '/visuals/city.webp', videoUrl: '/visuals/city.mp4' },
  { id: 'field', name: '田野', url: '/visuals/field.webp', videoUrl: '/visuals/field.mp4' },
  { id: 'mountain', name: '高山', url: '/visuals/mountain.webp', videoUrl: '/visuals/mountain.mp4' },
  { id: 'ocean', name: '大海', url: '/visuals/ocean.webp', videoUrl: '/visuals/ocean.mp4' },
  { id: 'dna', name: 'DNA', url: '/visuals/dna.webp', videoUrl: '/visuals/dna.mp4' },
  { id: 'street', name: '市井', url: '/visuals/street.webp', videoUrl: '/visuals/street.mp4', fadeLoop: true },
  { id: 'village', name: '村庄', url: '/visuals/village.webp', videoUrl: '/visuals/village.mp4', fadeLoop: true },
  { id: 'forest', name: '森林', url: '/visuals/forest.webp', videoUrl: '/visuals/forest.mp4', fadeLoop: true },
  { id: 'lake', name: '湖泊', url: '/visuals/lake.webp', videoUrl: '/visuals/lake.mp4', fadeLoop: true },
  { id: 'desert', name: '沙漠', url: '/visuals/desert.webp', videoUrl: '/visuals/desert.mp4', fadeLoop: true },
  { id: 'rice', name: '金秋稻田', url: '/visuals/rice.webp', videoUrl: '/visuals/rice.mp4', fadeLoop: true },
  { id: 'milkyway', name: '银河星野', url: '/visuals/milkyway.webp', videoUrl: '/visuals/milkyway.mp4', fadeLoop: true },
  { id: 'cells', name: '生物细胞', url: '/visuals/cells.webp', videoUrl: '/visuals/cells.mp4', fadeLoop: true },
  { id: 'molecules', name: '微观分子', url: '/visuals/molecules.webp', videoUrl: '/visuals/molecules.mp4', fadeLoop: true },
  { id: 'garden', name: '晨光花园', url: '/visuals/garden.webp', videoUrl: '/visuals/garden.mp4', fadeLoop: true },
].map(scene => Object.freeze(scene)))

type SceneStorage = Pick<Storage, 'getItem' | 'setItem'>
const deckVersion = 2
type VisitState = { v: typeof deckVersion; last: string | null; remaining: string[] }
const storageKey = 'tuxu.ui.scene-visits.v1'
const sceneById = new Map(scenes.map(scene => [scene.id, scene]))

function readVisitState(storage?: SceneStorage): VisitState {
  const empty: VisitState = { v: deckVersion, last: null, remaining: [] }
  try {
    const raw = storage?.getItem(storageKey)
    if (!raw || raw.length > 2048) return empty
    const state: unknown = JSON.parse(raw)
    if (!state || typeof state !== 'object' || Array.isArray(state)) return empty
    const saved = state as Record<string, unknown>
    const last = typeof saved.last === 'string' && sceneById.has(saved.last) ? saved.last : null
    // Upgrades and damaged decks start a new shuffle while retaining a valid last scene.
    const reset: VisitState = { v: deckVersion, last, remaining: [] }
    if (saved.v !== deckVersion || !last || !Array.isArray(saved.remaining) || saved.remaining.length >= scenes.length) return reset
    const remaining = saved.remaining
    if (remaining.some(id => typeof id !== 'string' || !sceneById.has(id) || id === last) || new Set(remaining).size !== remaining.length) return reset
    return { v: deckVersion, last, remaining: remaining as string[] }
  } catch {
    return empty
  }
}

function randomIndex(length: number, random: () => number): number {
  try {
    const value = random()
    if (typeof value !== 'number' || !Number.isFinite(value)) return 0
    return Math.max(0, Math.min(length - 1, Math.floor(value * length)))
  } catch {
    return 0
  }
}

function shuffledDeck(last: string | null, random: () => number): string[] {
  const deck = scenes.map(scene => scene.id)
  for (let index = deck.length - 1; index > 0; index--) {
    const next = randomIndex(index + 1, random)
    ;[deck[index], deck[next]] = [deck[next], deck[index]]
  }
  if (deck[0] === last) {
    const next = 1 + randomIndex(deck.length - 1, random)
    ;[deck[0], deck[next]] = [deck[next], deck[0]]
  }
  return deck
}

/** Deterministic with supplied storage/random; only the small scene deck is saved. */
export function chooseSceneForVisit(storage?: SceneStorage, random: () => number = Math.random): Scene {
  const state = readVisitState(storage)
  const deck = state.remaining.length ? state.remaining : shuffledDeck(state.last, random)
  const scene = sceneById.get(deck[0])!
  try {
    storage?.setItem(storageKey, JSON.stringify({ v: deckVersion, last: scene.id, remaining: deck.slice(1) }))
  } catch { /* Private mode/quota failure cannot block entry to the page. */ }
  return scene
}

type DocumentVisit = { initial?: Scene; storage: SceneStorage }
const documentVisits = new WeakMap<Document, DocumentVisit>()

function currentDocumentVisit(): DocumentVisit | null {
  if (typeof window === 'undefined') return null
  const browser = window
  let page: Document
  try { page = browser.document } catch { return null }
  if (!page || typeof page !== 'object') return null
  const existing = documentVisits.get(page)
  if (existing) return existing
  let memory: string | null = null
  let unavailable = false
  const context: DocumentVisit = {
    storage: {
      getItem(key) {
        if (!unavailable) {
          try { return browser.localStorage.getItem(key) ?? memory }
          catch { unavailable = true }
        }
        return memory
      },
      setItem(key, value) {
        memory = value
        if (!unavailable) {
          try { browser.localStorage.setItem(key, value) }
          catch { unavailable = true }
        }
      },
    },
  }
  documentVisits.set(page, context)
  return context
}

/** One initial draw per document, including React StrictMode double initialization. */
export function getInitialScene(): Scene {
  const visit = currentDocumentVisit()
  if (!visit) return scenes[0] // SSR must not initialize or consume a browser deck.
  if (!visit.initial) visit.initial = chooseSceneForVisit(visit.storage)
  return visit.initial
}

/** Call only on an explicit new home visit; no timer or manual selector exists. */
export function pickVisitScene(): Scene {
  const visit = currentDocumentVisit()
  return visit ? chooseSceneForVisit(visit.storage) : scenes[0]
}
