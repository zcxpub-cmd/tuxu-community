import { english } from './messages.en.ts'

export type Locale = 'zh' | 'en'
const preferenceKey = 'tuxu.ui.locale'
const listeners = new Set<() => void>()

function savedLocale(): Locale {
  try {
    if (typeof window !== 'undefined' && window.localStorage.getItem(preferenceKey) === 'en') return 'en'
  } catch { /* Unavailable storage should not prevent opening the workbench. */ }
  return 'zh'
}

let locale = savedLocale()
export const currentLocale = () => locale
export const subscribeLocale = (listener: () => void) => {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export function setLocale(next: Locale) {
  if (next === locale) return
  locale = next
  try { window.localStorage.setItem(preferenceKey, next) } catch { /* Session preference still works. */ }
  listeners.forEach(listener => listener())
}

export function t(source: string, ...values: (string | number)[]): string {
  const message = locale === 'en' && Object.hasOwn(english, source) ? english[source] : source
  return values.length ? message.replace(/\{(\d+)\}/g, (token, index) => String(values[Number(index)] ?? token)) : message
}

const systemPatterns = Object.keys(english).filter(key => /\{\d+\}/.test(key)).map(key => {
  const indexes: number[] = []
  const expression = key.split(/(\{\d+\})/g).map(part => {
    const placeholder = /^\{(\d+)\}$/.exec(part)
    if (placeholder) { indexes.push(Number(placeholder[1])); return '(.+?)' }
    return part.replace(/[.*+?^$()|[\]{}\\]/g, '\\$&')
  }).join('')
  return { key, indexes, pattern: new RegExp('^' + expression + '$', 's') }
})

/** Translate known application status messages without translating user content. */
export function systemText(source: string): string {
  if (locale !== 'en' || !/\p{Script=Han}/u.test(source)) return source
  if (Object.hasOwn(english, source)) return english[source]
  for (const { key, indexes, pattern } of systemPatterns) {
    const match = pattern.exec(source)
    if (!match) continue
    const args: string[] = []
    indexes.forEach((index, position) => { args[index] = t(match[position + 1]) })
    return t(key, ...args)
  }
  return source
}
