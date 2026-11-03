import { useSyncExternalStore } from 'react'

export type ThemePreference = 'light' | 'dark' | 'system'
export type WorkspaceTheme = 'light' | 'dark'
export interface ThemeSnapshot { preference: ThemePreference; resolvedTheme: WorkspaceTheme }
export interface ThemeAppearance extends ThemeSnapshot { setPreference: (preference: ThemePreference) => void }

export const THEME_STORAGE_KEY = 'tuxu.workspace-theme'
export const THEME_MEDIA_QUERY = '(prefers-color-scheme: dark)'
const serverSnapshot: ThemeSnapshot = Object.freeze({ preference: 'system', resolvedTheme: 'light' })
type ThemeWindow = Pick<Window, 'localStorage' | 'matchMedia' | 'document' | 'addEventListener' | 'removeEventListener'>

export function isThemePreference(value: unknown): value is ThemePreference {
  return value === 'light' || value === 'dark' || value === 'system'
}

/** The optional window makes tests independent of browser storage and user data. */
export function createThemeStore(target?: ThemeWindow) {
  const readPreference = (fallback: ThemePreference = 'system'): ThemePreference => {
    try {
      const stored = target?.localStorage.getItem(THEME_STORAGE_KEY)
      return isThemePreference(stored) ? stored : 'system'
    } catch { return fallback }
  }
  const readMedia = (): MediaQueryList | null => {
    try { return target?.matchMedia(THEME_MEDIA_QUERY) ?? null } catch { return null }
  }
  let media = readMedia()
  const resolve = (preference: ThemePreference, dark: boolean): WorkspaceTheme => preference === 'system' ? dark ? 'dark' : 'light' : preference
  const initialPreference = readPreference()
  let snapshot: ThemeSnapshot = Object.freeze({ preference: initialPreference, resolvedTheme: resolve(initialPreference, media?.matches ?? false) })
  const subscribers = new Set<() => void>()
  let detach: (() => void) | undefined
  let unsavedPreference = false

  const publish = (preference: ThemePreference, dark = media?.matches ?? false) => {
    const resolvedTheme = resolve(preference, dark)
    // Only this data attribute is changed. Entry colors and business state are untouched.
    if (target && target.document.documentElement.dataset.workspaceTheme !== resolvedTheme) target.document.documentElement.dataset.workspaceTheme = resolvedTheme
    if (snapshot.preference === preference && snapshot.resolvedTheme === resolvedTheme) return
    snapshot = Object.freeze({ preference, resolvedTheme })
    subscribers.forEach(listener => listener())
  }
  const start = () => {
    media = readMedia()
    const onMedia = (event: MediaQueryListEvent) => {
      if (snapshot.preference === 'system') publish('system', event.matches)
    }
    const onStorage = (event: StorageEvent) => {
      if (event.key !== THEME_STORAGE_KEY && event.key !== null) return
      try { if (event.storageArea && event.storageArea !== target?.localStorage) return } catch { return }
      unsavedPreference = false
      publish(isThemePreference(event.newValue) ? event.newValue : 'system')
    }
    if (media?.addEventListener) media.addEventListener('change', onMedia)
    else media?.addListener(onMedia)
    target?.addEventListener('storage', onStorage)
    detach = () => {
      if (media?.removeEventListener) media.removeEventListener('change', onMedia)
      else media?.removeListener(onMedia)
      target?.removeEventListener('storage', onStorage)
    }
    publish(unsavedPreference ? snapshot.preference : readPreference(snapshot.preference))
  }

  return {
    getSnapshot: () => snapshot,
    getServerSnapshot: () => serverSnapshot,
    subscribe: (listener: () => void) => {
      subscribers.add(listener)
      if (subscribers.size === 1) start()
      return () => {
        subscribers.delete(listener)
        if (!subscribers.size) { detach?.(); detach = undefined }
      }
    },
    setPreference: (preference: ThemePreference) => {
      if (!isThemePreference(preference)) return
      publish(preference)
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
