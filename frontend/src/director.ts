import type { Director, DirectorObject, Plan, Slot } from './types'

export const styleOptions: { id: Director['style_id']; label: string; detail: string }[] = [
  { id: 'inherit', label: '沿用方案', detail: '跟随这篇文章的风格' },
  { id: 'editorial', label: '自然插画', detail: '柔和配色，表达清楚' },
  { id: 'photographic', label: '真实摄影', detail: '自然、细腻、有层次' },
  { id: 'watercolor', label: '水彩', detail: '柔和笔触与纸面质感' },
  { id: 'ink', label: '水墨', detail: '墨色、留白与笔意' },
  { id: 'flat', label: '简洁色块', detail: '干净轮廓，简明直观' },
  { id: 'cinematic', label: '电影氛围', detail: '有故事感的画面' },
  { id: 'three_d', label: '立体质感', detail: '表现物体的体积与材质' },
  { id: 'diagram', label: '清晰示意', detail: '讲清结构与关系' },
]
export const brightnessOptions = [{ id: 'low', label: '偏暗', detail: '柔和暗调，主体清楚' }, { id: 'balanced', label: '自然', detail: '明暗自然、层次平衡' }, { id: 'high', label: '明亮', detail: '通透、轻盈' }] as const
export const lightingOptions = [{ id: 'soft', label: '柔光' }, { id: 'hard', label: '硬光' }, { id: 'backlit', label: '逆光' }] as const
export const cameraOptions = [{ id: 'wide', label: '远景 · 看环境' }, { id: 'medium', label: '中景 · 看主体' }, { id: 'closeup', label: '近景 · 看细节' }, { id: 'topdown', label: '俯视 · 看布局' }, { id: 'isometric', label: '等距 · 看结构' }] as const
export const depthLabels: Record<DirectorObject['depth'], string> = { foreground: '前景', midground: '中景', background: '后景' }
export const depthOrder: Record<DirectorObject['depth'], number> = { foreground: 3, midground: 2, background: 1 }
export function defaultDirector(): Director { return { schema_version: 1, style_id: 'inherit', brightness: 'balanced', lighting: 'soft', camera: 'medium', notes: '', objects: [] } }
/** Reading an old slot must never mutate it or add a field to its saved hash. */
export function directorFor(slot: Slot): Director { return slot.director ? structuredClone(slot.director) : defaultDirector() }
/** Removing the final object also removes its optional regional background. */
export function updateDirector(config: Director, patch: Partial<Director>): Director {
  const next = { ...config, ...patch }
  if (patch.objects && patch.objects.length === 0) delete next.background
  return next
}
const finite = (value: number | undefined, fallback: number) => value !== undefined && Number.isFinite(value) ? value : fallback
const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value))
const round = (value: number) => Math.round(value * 10) / 10
/** User interaction constrains a rectangle; the server independently rejects invalid saved values. */
export function fitObject(object: DirectorObject, patch: Partial<DirectorObject>): DirectorObject {
  const width = round(clamp(finite(patch.width, object.width), 8, 100))
  const height = round(clamp(finite(patch.height, object.height), 8, 100))
  const x = round(clamp(finite(patch.x, object.x), 0, 100 - width))
  const y = round(clamp(finite(patch.y, object.y), 0, 100 - height))
  return { ...object, ...patch, x, y, width, height }
}
export function moveObject(object: DirectorObject, dx: number, dy: number): DirectorObject { return fitObject(object, { x: object.x + dx, y: object.y + dy }) }
export function applyVisualSettings(slots: Slot[], source: Director): Slot[] {
  return slots.map(slot => {
    const current = directorFor(slot)
    const director = { ...structuredClone(source), objects: current.objects }
    if (current.background === undefined) delete director.background
    else director.background = current.background
    return { ...slot, director }
  })
}
export function planHasRegions(plan: Plan | null): boolean { return !!plan?.slots.some(slot => (slot.director?.objects.length || 0) > 0) }
export function summarizeDirector(config: Director): string {
  return [styleOptions.find(row => row.id === config.style_id)?.label, brightnessOptions.find(row => row.id === config.brightness)?.label].join(' · ')
}
export function directorSummary(slot: Slot): string { return summarizeDirector(directorFor(slot)) }
export function objectPosition(object: DirectorObject): string {
  const horizontal = object.x + object.width / 2 < 100 / 3 ? 0 : object.x + object.width / 2 > 200 / 3 ? 2 : 1
  const vertical = object.y + object.height / 2 < 100 / 3 ? 0 : object.y + object.height / 2 > 200 / 3 ? 2 : 1
  return [['左上方', '上方居中', '右上方'], ['左侧', '居中', '右侧'], ['左下方', '下方居中', '右下方']][vertical][horizontal]
}
export function alignObject(object: DirectorObject, axis: 'horizontal' | 'vertical', position: 'start' | 'center' | 'end'): DirectorObject {
  const size = axis === 'horizontal' ? object.width : object.height
  const value = position === 'start' ? 4 : position === 'center' ? (100 - size) / 2 : 96 - size
  return fitObject(object, axis === 'horizontal' ? { x: value } : { y: value })
}
