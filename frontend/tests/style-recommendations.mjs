import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import { applyVisualSettings, defaultDirector, directorFor, styleChoiceLabel, styleOptions, styleRecommendationFor, updateDirector } from '../src/director.ts'

const recommendation = { style_id: 'scientific', reason: '推荐科研示意，便于表达观测模块之间的关系。', source: 'local_model' }
const slot = { id: 'sample', caption: '观测系统概念示意', description: '三个功能模块的信息关系，不代表真实设备部署。', aspect_ratio: 'landscape', director: { ...defaultDirector(), style_id: 'scientific' }, style_recommendation: recommendation }
const old = { id: 'old', caption: '旧图位', description: '', aspect_ratio: 'landscape' }
assert.equal(styleOptions.length, 14)
assert.equal(styleRecommendationFor(old), undefined)
assert.equal(directorFor(old).style_id, 'inherit', 'Legacy slots do not acquire a recommendation or a new default when read')
for (const invalid of [{ ...recommendation, style_id: 'unknown' }, { ...recommendation, style_id: 'inherit' }, { ...recommendation, reason: ' ' }]) {
  assert.equal(styleRecommendationFor({ ...slot, style_recommendation: invalid }), undefined)
}
assert.equal(styleChoiceLabel('scientific', styleRecommendationFor(slot)), '科研示意（推荐）')
assert.equal(styleChoiceLabel('watercolor', styleRecommendationFor(slot)), '水彩')
const before = JSON.stringify(slot)
const manual = { ...slot, director: updateDirector(directorFor(slot), { style_id: 'watercolor' }) }
const restored = JSON.parse(JSON.stringify(manual))
assert.equal(directorFor(restored).style_id, 'watercolor', 'A saved and reopened manual style takes precedence over semantic metadata')
assert.equal(styleRecommendationFor(restored).style_id, 'scientific', 'Changing a style keeps the original recommendation visible as a choice')
assert.equal(JSON.stringify(slot), before, 'Reading and editing do not modify the previously approved slot')
assert.equal(applyVisualSettings([slot], { ...defaultDirector(), style_id: 'ink' })[0].director.style_id, 'ink', 'An explicit batch style also wins over each recommendation')

const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
try {
  const { default: DirectorPanel } = await server.ssrLoadModule('/src/DirectorPanel.tsx')
  const render = value => renderToStaticMarkup(createElement(DirectorPanel, { slots: [value], selectedSlot: value.id, style: '自然插画', locked: false, regionReady: true, onSelect() {}, onChange() {}, onApply() {} }))
  const recommended = render(slot)
  assert.equal((recommended.match(/科研示意（推荐）/g) || []).length, 2, 'Both desktop button and mobile dropdown mark the recommended choice')
  assert.match(recommended, /推荐科研示意，便于表达观测模块之间的关系。/)
  assert.match(recommended, /可自由切换画风，保存后以你的选择为准/)
  const switched = render(restored)
  assert.match(switched, /<option(?=[^>]*value="watercolor")(?=[^>]*selected="")[^>]*>水彩<\/option>/, 'After save/reopen the mobile select keeps the manual choice')
  assert.match(switched, /<button(?=[^>]*class="selected")(?=[^>]*aria-pressed="true")[^>]*><strong>水彩<\/strong>/, 'The desktop selected button agrees with the saved choice')
  assert.equal((switched.match(/科研示意（推荐）/g) || []).length, 2)
  assert.doesNotMatch(render(old), /（推荐）|style-recommendation/, 'Old slots have neither invented metadata nor a misleading recommendation badge')
} finally { await server.close() }
console.log('PASS: 14 styles, valid recommendation labels and reason in desktop/mobile markup, manual and batch choices persist over recommendation, immutable legacy reads')
