import assert from 'node:assert/strict'
import { alignObject, applyVisualSettings, defaultDirector, directorFor, fitObject, moveObject, objectPosition, planHasRegions, styleOptions, updateDirector } from '../src/director.ts'
import { stages, taskStage } from '../src/workflow.ts'
import { canConfirmExport } from '../src/documentPreview.ts'

const rectangle = { id: 'lamp', label: '台灯', description: '深绿台灯，照向桌面', x: 10, y: 20, width: 30, height: 40, depth: 'foreground' }
const original = JSON.stringify(rectangle)
assert.deepEqual(moveObject(rectangle, 400, -400), { ...rectangle, x: 70, y: 0 }, 'Dragging past a canvas edge keeps the full area inside it')
assert.deepEqual(fitObject(rectangle, { width: 99, height: 95 }), { ...rectangle, x: 1, y: 5, width: 99, height: 95 }, 'Increasing size still produces a fully visible region')
assert.deepEqual(fitObject(rectangle, { width: -5, height: 0 }), { ...rectangle, width: 8, height: 8 }, 'Tiny or negative areas cannot be sent by numeric editing')
assert.deepEqual(fitObject(rectangle, { x: NaN, y: Infinity, width: NaN, height: -Infinity }), rectangle, 'Nonfinite input never contaminates persisted geometry')
assert.equal(moveObject(rectangle, 1, -5).x, 11)
assert.equal(moveObject(rectangle, 1, -5).y, 15)
assert.deepEqual(fitObject(rectangle, { x: 13.37, width: 31.66 }), { ...rectangle, x: 13.4, width: 31.7 }, 'Relative positions use stable decimal precision')
assert.equal(JSON.stringify(rectangle), original, 'Editing helpers do not mutate the previous saved object')
for (const horizontal of ['start', 'center', 'end']) {
  for (const vertical of ['start', 'center', 'end']) {
    const aligned = alignObject(alignObject(rectangle, 'horizontal', horizontal), 'vertical', vertical)
    assert.ok(aligned.x >= 0 && aligned.y >= 0 && aligned.x + aligned.width <= 100 && aligned.y + aligned.height <= 100, 'Quick placement stays inside the canvas')
  }
}
assert.equal(objectPosition(alignObject(alignObject(rectangle, 'horizontal', 'start'), 'vertical', 'start')), '左上方')
assert.equal(objectPosition(alignObject(alignObject(rectangle, 'horizontal', 'center'), 'vertical', 'center')), '居中')
assert.equal(objectPosition(alignObject(alignObject(rectangle, 'horizontal', 'end'), 'vertical', 'end')), '右下方')
assert.deepEqual(alignObject({ ...rectangle, width: 100, height: 100, x: 0, y: 0 }, 'horizontal', 'end'), { ...rectangle, width: 100, height: 100, x: 0, y: 0 }, 'Full-width region cannot be shifted outside by a quick placement')

const legacySlot = { id: 'old', caption: '历史画面' }
const legacySaved = JSON.stringify(legacySlot)
const viewed = directorFor(legacySlot)
viewed.objects.push(rectangle)
assert.equal(JSON.stringify(legacySlot), legacySaved, 'Viewing an old task must leave its saved hash and approval intact')
assert.equal(directorFor(legacySlot).objects.length, 0, 'Default arrays are isolated across old slots')
assert.equal(styleOptions.length, 9)
assert.equal(new Set(styleOptions.map(item => item.id)).size, 9)
assert.equal(defaultDirector().style_id, 'inherit', 'Legacy visual styles remain in effect until an explicit preset is chosen')
assert.equal(Object.hasOwn(defaultDirector(), 'background'), false, 'Default directors must not add a new key to old drafts')
const oldConfigured = { director: { ...defaultDirector(), objects: [rectangle] } }
assert.equal(Object.hasOwn(directorFor(oldConfigured), 'background'), false, 'Viewing an existing regional director retains an absent background key')
const withBackground = { ...defaultDirector(), objects: [rectangle], background: '白色桌面' }
const savedBackground = JSON.stringify(withBackground)
const backgroundDraft = directorFor({ director: withBackground })
backgroundDraft.background = '林间空地'
assert.equal(JSON.stringify(withBackground), savedBackground, 'A background draft cannot mutate the saved plan or approval fingerprint')
assert.equal(updateDirector(withBackground, { brightness: 'high' }).background, '白色桌面', 'Visual edits retain the current background')
const withTwoObjects = { ...withBackground, objects: [rectangle, { ...rectangle, id: 'book' }] }
assert.equal(updateDirector(withTwoObjects, { objects: [rectangle] }).background, '白色桌面', 'Removing one of two objects retains the regional background')
const withoutObjects = updateDirector(withBackground, { objects: [] })
assert.equal(Object.hasOwn(withoutObjects, 'background'), false, 'Deleting the final object cannot send a hidden nonempty background for rejection')
assert.deepEqual(withoutObjects.objects, [])
assert.equal(JSON.stringify(withBackground), savedBackground, 'Object removal leaves the previous saved director untouched')

const first = { id: 'first', director: { ...defaultDirector(), objects: [rectangle] } }
const secondObject = { ...rectangle, id: 'tree', label: '树木', x: 60, width: 20, depth: 'background' }
const second = { id: 'second', director: { ...defaultDirector(), objects: [secondObject] } }
const source = { ...first.director, style_id: 'watercolor', brightness: 'high', lighting: 'backlit', camera: 'topdown', notes: '留白，保留本文事实' }
const slots = [first, second, legacySlot]
const beforeApply = JSON.stringify(slots)
const applied = applyVisualSettings(slots, source)
assert.deepEqual(applied.map(slot => slot.director.style_id), ['watercolor', 'watercolor', 'watercolor'])
assert.deepEqual(applied.map(slot => slot.director.brightness), ['high', 'high', 'high'])
assert.deepEqual(applied[0].director.objects, [rectangle])
assert.deepEqual(applied[1].director.objects, [secondObject], 'Batch application never copies another image’s objects or placement')
assert.deepEqual(applied[2].director.objects, [], 'An old slot remains free of invented objects')
applied[0].director.objects[0].label = '编辑后的台灯'
assert.equal(JSON.stringify(slots), beforeApply, 'Batch edits cannot modify the prior plan or its approval fingerprint')
assert.equal(applied[1].director.objects[0].label, '树木')
const backgroundSlots = [
  { ...first, director: { ...first.director, background: '白色桌面' } },
  { ...second, director: { ...second.director, background: '林间空地' } },
  first,
  { ...first, id: 'blank', director: { ...first.director, background: '' } },
  legacySlot,
]
const backgroundBeforeApply = JSON.stringify(backgroundSlots)
const backgroundApplied = applyVisualSettings(backgroundSlots, { ...source, background: '不能复制到其他图的背景' })
assert.deepEqual(backgroundApplied.map(slot => slot.director.background), ['白色桌面', '林间空地', undefined, '', undefined], 'Applying styles preserves each slot background, including explicit blank and absent values')
assert.equal(Object.hasOwn(backgroundApplied[2].director, 'background'), false, 'Batch styles must not inject a source background into an old configured slot')
assert.equal(Object.hasOwn(backgroundApplied[4].director, 'background'), false, 'Batch styles must not invent a background for an object-free legacy slot')
assert.deepEqual(backgroundApplied[1].director.objects, [secondObject], 'Background preservation also preserves each target geometry')
backgroundApplied[1].director.objects[0].x = 1
assert.equal(JSON.stringify(backgroundSlots), backgroundBeforeApply, 'Batch styles and backgrounds retain independent object drafts')
assert.equal(planHasRegions({ slots }), true)
assert.equal(planHasRegions({ slots: [legacySlot] }), false)
assert.equal(planHasRegions({ slots: [] }), false)
assert.equal(planHasRegions(null), false)

assert.deepEqual(stages, ['文章与设置', '配图方案', '画面导演台', '授权与生成', '选图与处理', '插入与导出'])
const task = { plan: null, approval: null, assets: [], exports: [] }
assert.equal(taskStage(task), 0)
assert.equal(taskStage({ ...task, plan: { slots: [] } }), 1, 'A saved zero-image plan still opens at the plan and can go directly to stage 5')
assert.equal(taskStage({ ...task, plan: { slots }, approval: { id: 'legacy-approval' } }), 3, 'Old approved tasks resume at the shifted approval stage')
assert.equal(taskStage({ ...task, assets: [{ stale: false }] }), 4)
assert.equal(taskStage({ ...task, assets: [{ stale: true }] }), 0, 'Stale historical images cannot imply current candidates')
assert.equal(taskStage({ ...task, exports: [{ url: 'legacy.docx' }] }), 5)

const exportState = { plan: { slots: [first] }, selectedCount: 1, approvalId: 'approval', unsaved: true, scopeChanged: false, locked: false }
assert.equal(canConfirmExport(exportState), false, 'An unsaved director change blocks a previously approved selected asset from export')
assert.equal(canConfirmExport({ ...exportState, unsaved: false }), true)
assert.equal(canConfirmExport({ ...exportState, plan: { slots: [] }, selectedCount: 0, approvalId: undefined, unsaved: false }), true, 'Zero-image route remains independent from region controls and approval')
console.log('PASS: director drag/resize/bounds/nonfinite precision, simple nine-position shortcuts and Chinese summaries, optional background legacy compatibility/removal/batch isolation, immutable legacy reads, six-stage history, region detection and unsaved/zero-plan export guards')
