import { useSyncExternalStore } from 'react'

export type ThemePreference = 'light' | 'dark' | 'system'
export type WorkspaceTheme = 'light' | 'dark'
export interface ThemeSnapshot { preference: ThemePreference; resolvedTheme: WorkspaceTheme }
export interface ThemeAppearance extends ThemeSnapshot { setPreference: (preference: ThemePreference) => void }
export interface ThemeClock {
  now: () => Date;
  setTimeout: (callback: () => void, delay: number) => unknown;
  clearTimeout: (timer: unknown) => void;
}

export const THEME_STORAGE_KEY = 'tuxu.workspace-theme'
export const THEME_DAY_START_HOUR = 7
export const THEME_NIGHT_START_HOUR = 19
const CLOCK_CHECK_INTERVAL = 60_000
const serverSnapshot: ThemeSnapshot = Object.freeze({ preference: 'system', resolvedTheme: 'light' })
type ThemeWindow = Pick<Window, 'localStorage' | 'document' | 'addEventListener' | 'removeEventListener'>
const localClock: ThemeClock = {
  now: () => new Date(),
  setTimeout: (callback, delay) => globalThis.setTimeout(callback, delay),
  clearTimeout: timer => globalThis.clearTimeout(timer as ReturnType<typeof globalThis.setTimeout>),
}

export function isThemePreference(value: unknown): value is ThemePreference {
  return value === 'light' || value === 'dark' || value === 'system'
}
export function localClockTheme(now: Date): WorkspaceTheme {
  return now.getHours() >= THEME_DAY_START_HOUR && now.getHours() < THEME_NIGHT_START_HOUR ? 'light' : 'dark'
}
/** Build the next boundary as a local date, including a changed day/time zone. */
export function nextThemeBoundaryDelay(now: Date): number {
  const next = new Date(now.getTime())
  if (now.getHours() < THEME_DAY_START_HOUR) next.setHours(THEME_DAY_START_HOUR, 0, 0, 0)
  else if (now.getHours() < THEME_NIGHT_START_HOUR) next.setHours(THEME_NIGHT_START_HOUR, 0, 0, 0)
  else { next.setDate(next.getDate() + 1); next.setHours(THEME_DAY_START_HOUR, 0, 0, 0) }
  return Math.max(1, next.getTime() - now.getTime())
}

/** Inject a window and clock for isolated tests; no user system time is changed. */
export function createThemeStore(target?: ThemeWindow, clock: ThemeClock = localClock) {
  const readPreference = (fallback: ThemePreference = 'system'): ThemePreference => {
    try {
      const stored = target?.localStorage.getItem(THEME_STORAGE_KEY)
      return isThemePreference(stored) ? stored : 'system'
    } catch { return fallback }
  }
  const resolve = (preference: ThemePreference): WorkspaceTheme => preference === 'system' ? localClockTheme(clock.now()) : preference
  const initialPreference = readPreference()
  let snapshot: ThemeSnapshot = Object.freeze({ preference: initialPreference, resolvedTheme: resolve(initialPreference) })
  const subscribers = new Map<() => void, number>()
  let subscriptionCount = 0
  let detach: (() => void) | undefined
  let listening = false
  let timer: unknown
  let timerEpoch = 0
  let unsavedPreference = false

  const publish = (preference: ThemePreference) => {
    const resolvedTheme = resolve(preference)
    // Only this attribute changes. Homepage colors and business state are untouched.
    if (target && target.document.documentElement.dataset.workspaceTheme !== resolvedTheme) target.document.documentElement.dataset.workspaceTheme = resolvedTheme
    if (snapshot.preference === preference && snapshot.resolvedTheme === resolvedTheme) return
    snapshot = Object.freeze({ preference, resolvedTheme })
    subscribers.forEach((_count, listener) => listener())
  }
  const clearTimer = () => {
    timerEpoch++
    if (timer !== undefined) clock.clearTimeout(timer)
    timer = undefined
  }
  const schedule = () => {
    clearTimer()
    if (!listening || !target || snapshot.preference !== 'system') return
    const expectedEpoch = timerEpoch
    // Exact local boundaries plus a bounded clock check catch active-page time edits.
    const delay = Math.min(CLOCK_CHECK_INTERVAL, nextThemeBoundaryDelay(clock.now()))
    timer = clock.setTimeout(() => {
      if (!listening || expectedEpoch !== timerEpoch) return
      timer = undefined
      publish(snapshot.preference)
      schedule()
    }, delay)
  }
  const recalibrate = () => {
    if (!listening) return
    publish(snapshot.preference)
    schedule()
  }
  const start = () => {
    listening = true
    const onStorage = (event: StorageEvent) => {
      if (event.key !== THEME_STORAGE_KEY && event.key !== null) return
      try { if (event.storageArea && event.storageArea !== target?.localStorage) return } catch { return }
      unsavedPreference = false
      publish(isThemePreference(event.newValue) ? event.newValue : 'system')
      schedule()
    }
    target?.addEventListener('storage', onStorage)
    target?.addEventListener('focus', recalibrate)
    target?.addEventListener('pageshow', recalibrate)
    target?.document.addEventListener('visibilitychange', recalibrate)
    detach = () => {
      listening = false
      clearTimer()
      target?.removeEventListener('storage', onStorage)
      target?.removeEventListener('focus', recalibrate)
      target?.removeEventListener('pageshow', recalibrate)
      target?.document.removeEventListener('visibilitychange', recalibrate)
    }
    publish(unsavedPreference ? snapshot.preference : readPreference(snapshot.preference))
    schedule()
  }

  return {
    getSnapshot: () => snapshot,
    getServerSnapshot: () => serverSnapshot,
    subscribe: (listener: () => void) => {
      subscribers.set(listener, (subscribers.get(listener) || 0) + 1)
      if (++subscriptionCount === 1) start()
      let subscribed = true
      return () => {
        if (!subscribed) return
        subscribed = false
        const count = subscribers.get(listener) || 0
        if (count <= 1) subscribers.delete(listener)
        else subscribers.set(listener, count - 1)
        if (--subscriptionCount === 0) { detach?.(); detach = undefined }
      }
    },
    setPreference: (preference: ThemePreference) => {
      if (!isThemePreference(preference)) return
      publish(preference)
      schedule()
      try { target?.localStorage.setItem(THEME_STORAGE_KEY, preference); unsavedPreference = false }
      catch { unsavedPreference = true /* Keep this session usable when storage is denied. */ }
    },
  }
}

const serverStore = createThemeStore()
let browserStore: ReturnType<typeof createThemeStore> | undefined
export function useTheme(): ThemeAppearance {
  const store = typeof window === 'undefined' ? serverStore : (browserStore ??= createThemeStore(window))
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getServerSnapshot)
  return { ...snapshot, setPreference: store.setPreference }
}
