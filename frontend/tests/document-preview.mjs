import assert from 'node:assert/strict'
import { canConfirmExport, figureCaption, selectedFigures } from '../src/documentPreview.ts'

const paragraphs = [
  { id: 'first', kind: 'paragraph', text: '第一段', source_index: 0 },
  { id: 'second', kind: 'paragraph', text: '第二段', source_index: 1 },
]
const slot = (id, paragraphId) => ({ id, paragraph_id: paragraphId, caption: `图 9：${id}` })
const asset = id => ({ id: `asset-${id}`, slot_id: id, stale: false, url: `${id}.png`, processed_url: `${id}-processed.png` })
const plan = slots => ({ version: 1, slots })
const task = (slots, selected = slots.map(row => row.id)) => ({
  document: { paragraphs }, plan: plan(slots), assets: slots.map(row => asset(row.id)),
  selections: Object.fromEntries(selected.map(id => [id, { asset_id: `asset-${id}`, use_processed: false }])),
})
const ids = rows => rows.map(row => row.slot.id)

for (const count of [2, 3]) {
  const slots = Array.from({ length: count }, (_, index) => slot(`same-${index}`, 'first'))
  const rows = selectedFigures(task(slots))
  assert.equal(rows.filter(row => row.paragraph.id === 'first').length, count, 'Every selected image anchored to one paragraph must render')
  assert.deepEqual(ids(rows), slots.map(row => row.id), 'Same-paragraph images retain plan order')
  assert.deepEqual(rows.map(row => row.number), Array.from({ length: count }, (_, index) => index + 1))
}

const reversedPlan = [slot('later', 'second'), slot('earlier', 'first'), slot('same-paragraph', 'first')]
assert.deepEqual(ids(selectedFigures(task(reversedPlan))), ['earlier', 'same-paragraph', 'later'], 'Document order precedes plan order')
const partial = selectedFigures(task(reversedPlan, ['later', 'same-paragraph']))
assert.deepEqual(ids(partial), ['same-paragraph', 'later'])
assert.deepEqual(partial.map(row => row.number), [1, 2], 'Partial selections must have contiguous figure numbers')
assert.deepEqual(partial.map(row => row.caption), ['图 1 same-paragraph（AI 生成示意图）', '图 2 later（AI 生成示意图）'])
const swapped = [slot('later', 'first'), slot('earlier', 'second'), slot('same-paragraph', 'first')]
assert.deepEqual(ids(selectedFigures(task(swapped))), ['later', 'same-paragraph', 'earlier'], 'Changing anchors recomputes document-order numbers')
const staleTask = task(reversedPlan)
staleTask.assets[1].stale = true
assert.deepEqual(ids(selectedFigures(staleTask)), ['same-paragraph', 'later'], 'Stale assets are excluded')
const processedTask = task([slot('processed', 'first')])
processedTask.selections.processed.use_processed = true
assert.equal(selectedFigures(processedTask)[0].asset.processed_url, 'processed-processed.png')
processedTask.assets[0].processed_url = null
assert.equal(selectedFigures(processedTask).length, 0, 'Missing processed image cannot be previewed as a valid selection')
assert.equal(figureCaption(3, '图1 科学流程（AI 生成示意图）'), '图 3 科学流程（AI 生成示意图）')
assert.equal(figureCaption(1, ''), '图 1 配图示意（AI 生成示意图）')

const base = { plan: plan([]), selectedCount: 0, approvalId: undefined, unsaved: false, scopeChanged: false, locked: false }
assert.equal(canConfirmExport(base), true, 'Saved zero-image plans need no approval or checkpoint')
assert.equal(canConfirmExport({ ...base, unsaved: true }), false)
assert.equal(canConfirmExport({ ...base, locked: true }), false)
assert.equal(canConfirmExport({ ...base, plan: null }), false)
assert.equal(canConfirmExport({ ...base, selectedCount: 1 }), false, 'A zero plan cannot insert a stale selection')
const nonempty = { ...base, plan: plan([slot('image', 'first')]), selectedCount: 1 }
assert.equal(canConfirmExport(nonempty), false, 'Nonempty plan still requires current approval')
assert.equal(canConfirmExport({ ...nonempty, approvalId: 'approval' }), true)
assert.equal(canConfirmExport({ ...nonempty, approvalId: 'approval', scopeChanged: true }), false)
assert.equal(canConfirmExport({ ...nonempty, approvalId: 'approval', selectedCount: 0 }), false)
console.log('PASS: same-paragraph 2/3 images, partial selection, swapped anchors, captions, stale/processed guards, and zero-plan export protections')
