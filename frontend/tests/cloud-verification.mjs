import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { beginCatalogRefresh, createCatalogGate, releaseUnpublishedRefresh } from '../src/catalogGate.ts'
import { canAdoptModelTest, cloudDraftChanged, safeModelIds, verificationLabels } from '../src/cloudVerification.ts'

const profile = { id: 'ark', kind: 'ark', label: '豆包', base_url: 'https://example.com/api/v3', model: 'doubao-seedream-4-5-251128', enabled: true, key_present: true, ready: true, revision: 'saved-revision', verification: { connection: 'untested', model_listed: null, generation: 'untested' } }
const draft = { label: profile.label, base_url: profile.base_url, model: profile.model, enabled: profile.enabled, key: '', clear_key: false }
assert.equal(cloudDraftChanged(profile, draft), false)
for (const field of ['label', 'base_url', 'model']) assert.equal(cloudDraftChanged(profile, { ...draft, [field]: `${draft[field]}-edit` }), true, `${field} changes disable testing until save`)
assert.equal(cloudDraftChanged(profile, { ...draft, enabled: false }), true)
assert.equal(cloudDraftChanged(profile, { ...draft, key: 'ephemeral-secret' }), true)
assert.equal(cloudDraftChanged(profile, { ...draft, key: ' ' }), true)
assert.equal(cloudDraftChanged(profile, { ...draft, clear_key: true }), true)
const testResult = { ok: true, model_found: true, models: [{ id: profile.model }], revision: profile.revision }
assert.equal(canAdoptModelTest(profile.revision, profile.revision, testResult, 3, 3), true, 'A matching completed test can populate candidates')
assert.equal(canAdoptModelTest(profile.revision, 'new-revision', testResult, 3, 3), false, 'Saving new configuration discards an old response')
assert.equal(canAdoptModelTest(profile.revision, profile.revision, { ...testResult, revision: 'other-revision' }, 3, 3), false)
assert.equal(canAdoptModelTest(profile.revision, profile.revision, testResult, 3, 4), false, 'Manual refresh or a newer test invalidates a late response')
assert.equal(canAdoptModelTest(profile.revision, profile.revision, { ...testResult, ok: false }, 3, 3), false)
assert.equal(canAdoptModelTest(profile.revision, profile.revision, { ...testResult, ok: 'true' }, 3, 3), false)
const models = [{ id: 'doubao-seedream-4-5-251128', description: 'private-payload' }, { id: 'doubao-seedream-3-0-t2i-250415' }, { id: 'doubao-seedream-4-5-251128' }, { id: 'gpt-image-1' }, { id: 'doubao-seedream-foo\n' }, { id: 'ep-manually-configured' }, { id: 'canary-secret' }, null, { id: 42 }]
assert.deepEqual(safeModelIds('ark', models), ['doubao-seedream-3-0-t2i-250415', 'doubao-seedream-4-5-251128'])
assert.deepEqual(safeModelIds('openai', [{ id: 'gpt-image-2' }, { id: 'gpt-image-1.5' }, { id: 'dall-e-2' }, { id: 'dall-e-3' }, { id: 'gpt-image-secret\n' }, { id: 'text-model' }]), ['dall-e-2', 'dall-e-3', 'gpt-image-1.5', 'gpt-image-2'])
assert.equal(safeModelIds('ark', Array.from({ length: 100 }, (_, i) => ({ id: `doubao-seedream-model-${i}` }))).length, 64)
assert.deepEqual(safeModelIds('ark', [{ id: `doubao-seedream-${'a'.repeat(160)}` }]), [])
assert.deepEqual(safeModelIds('ark', undefined), [])
assert.deepEqual(verificationLabels(profile), { configuration: '配置完整', connection: '连接尚未测试', model: '模型列表尚未验证', generation: '尚无实际生图验证' })
assert.equal(verificationLabels({ ...profile, verification: { connection: 'ok', model_listed: true, generation: 'untested' } }).generation, '尚无实际生图验证', 'A listed model never becomes an actual generation success')
assert.equal(verificationLabels({ ...profile, verification: { connection: 'ok', model_listed: true, generation: 'rejected' } }).generation, '最近生图被服务拒绝')
assert.equal(verificationLabels({ ...profile, verification: { connection: 'failed', model_listed: null, generation: 'unknown' } }).generation, '最近生图结果未确定')
assert.equal(verificationLabels({ ...profile, verification: { connection: 'ok', model_listed: false, generation: 'succeeded' } }).generation, '最近实际生图成功')


// Consume the same publisher gate as App: a delayed terminal GET and bootstrap
// cannot overwrite a newer settings save/refresh, regardless of response order.
const catalogA = { profiles: [{ ...profile, revision: 'revision-A' }], built_in: { id: 'codex', ready: true } }
const catalogB = { ...catalogA, profiles: [{ ...profile, revision: 'revision-B' }] }
const gate = createCatalogGate()
let visibleCatalog
const publish = (catalog, epoch) => { if (!gate.publish(catalog, epoch)) return false; visibleCatalog = gate.latest(); return true }
assert.equal(publish(catalogA), true)
const pendingTerminalA = gate.capture(), pendingBootstrapA = gate.capture()
assert.equal(publish(catalogB), true, 'Settings saves publish through the shared gate')
assert.equal(publish(catalogA, pendingTerminalA), false, 'A delayed terminal response must not restore stale configuration A')
assert.equal(publish(catalogA, pendingBootstrapA), false, 'An older bootstrap cloud payload also cannot overwrite B')
assert.equal(visibleCatalog, catalogB)
const currentTerminalB = gate.capture()
assert.equal(publish({ ...catalogB }, currentTerminalB), true, 'A current terminal response may update verification')
const refreshEntries = new Map()
const cancelledEntry = beginCatalogRefresh(refreshEntries, 'same-job:failed')
assert.ok(cancelledEntry)
assert.equal(beginCatalogRefresh(refreshEntries, 'same-job:failed'), null, 'Pending reads are deduplicated')
releaseUnpublishedRefresh(refreshEntries, 'same-job:failed', cancelledEntry)
const nextEntry = beginCatalogRefresh(refreshEntries, 'same-job:failed')
assert.ok(nextEntry, 'Cancelled or discarded reads remain eligible when the task is reopened')
releaseUnpublishedRefresh(refreshEntries, 'same-job:failed', cancelledEntry)
assert.equal(refreshEntries.get('same-job:failed'), nextEntry, 'The old promise finally cannot remove a newer pending read')
nextEntry.published = true
releaseUnpublishedRefresh(refreshEntries, 'same-job:failed', nextEntry)
assert.equal(beginCatalogRefresh(refreshEntries, 'same-job:failed'), null, 'Only a successfully published read is permanently deduplicated')

const settings = readFileSync(new URL('../src/CloudSettings.tsx', import.meta.url), 'utf8')
const display = readFileSync(new URL('../src/ProviderVerification.tsx', import.meta.url), 'utf8')
const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
assert.match(settings, /disabled=\{!profile.ready \|\| dirty\}/)
assert.match(settings, /选择后请点击保存/)
assert.match(settings, /setModel\(event.target.value\)/)
assert.match(settings, /if \(source !== 'test'\) setRefreshEpoch/, 'This test refresh preserves only its own newly verified candidates')
assert.match(settings, /profile.revision, refreshEpoch\]/, 'Revision/save/manual refresh invalidates old candidates')
assert.match(settings, /result.revision !== revision/)
assert.doesNotMatch(settings + display, /result\.error|err\.message|response\.data|error_code/, 'No raw diagnostic/error payload is displayed')
assert.match(display, /verification\.message/, 'Chinese diagnosis comes from the backend finite safe projection')
assert.match(app, /publishCatalog\(catalog, expectedCatalogEpoch\)/, 'Terminal publication is guarded by the shared App catalog epoch')
assert.match(app, /onChange=\{catalog => \{ publishCatalog\(catalog\)/, 'Settings publishes advance that same epoch')
assert.match(app, /finally\(release\)/)
assert.match(app, /latestJob.kind !== 'generation'/)
assert.match(app, /request<CloudCatalog>\('\/image-providers'\)\.then/, 'Actual terminal API jobs refresh only local verification records')
assert.match(app, /chosenProfile && <ProviderVerification profile=\{chosenProfile\}/, 'Generation view uses the selected API verification')
console.log('PASS: dirty-save gate; revision/sequence race rejection; bounded ID-only candidates; manual-save selection; separate config/list/actual-generation status; safe diagnosis; test-refresh candidate preservation; cross-publisher catalog race and cancelled read recovery')
