import type { Asset, Paragraph, Plan, Selection, Slot, Task } from './types'

export interface SelectedFigure {
  slot: Slot
  paragraph: Paragraph
  selection: Selection
  asset: Asset
  number: number
  caption: string
}

/** Mirrors backend/app/documents.py: _caption and _validated_selections. */
export function figureCaption(number: number, value: string): string {
  let text = (value || '配图示意').replace(/^\s*图\s*\d+\s*[：:.、-]?\s*/, '')
  if (!text.replace(/\s+/g, '').includes('AI生成示意图')) text += '（AI 生成示意图）'
  return `图 ${number} ${text}`
}

export function insertableParagraph(paragraph: Paragraph): boolean {
  return paragraph.kind === 'paragraph' && (paragraph.insertable ?? !!paragraph.text.trim())
}

export function selectedFigures(task: Pick<Task, 'document' | 'plan' | 'selections' | 'assets'>): SelectedFigure[] {
  const paragraphs = new Map(task.document.paragraphs.map(paragraph => [paragraph.id, paragraph]))
  const assets = new Map(task.assets.map(asset => [asset.id, asset]))
  const rows = (task.plan?.slots || []).flatMap((slot, slotOrder) => {
    const paragraph = paragraphs.get(slot.paragraph_id)
    const selection = task.selections[slot.id]
    const asset = selection && assets.get(selection.asset_id)
    if (!paragraph || !insertableParagraph(paragraph) || !selection || !asset || asset.stale || asset.slot_id !== slot.id || (selection.use_processed && !asset.processed_url)) return []
    return [{ slot, paragraph, selection, asset, slotOrder }]
  })
  rows.sort((a, b) => a.paragraph.source_index - b.paragraph.source_index || a.slotOrder - b.slotOrder)
  return rows.map((row, index) => ({ ...row, number: index + 1, caption: figureCaption(index + 1, row.slot.caption) }))
}

/** A saved empty plan can export the original without generation approval. */
export function canConfirmExport({ plan, selectedCount, approvalId, unsaved, scopeChanged, locked }: {
  plan: Plan | null
  selectedCount: number
  approvalId?: string
  unsaved: boolean
  scopeChanged: boolean
  locked: boolean
}): boolean {
  if (!plan || unsaved || locked) return false
  if (plan.slots.length === 0) return selectedCount === 0
  return selectedCount > 0 && !!approvalId && !scopeChanged
}
