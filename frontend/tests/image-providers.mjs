import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { approvedProvider, approvalRequest, activeJobStates, activeTaskStates, imageScopeChanged, taskImageProvider, targetReady } from '../src/imageProviders.ts'
import { taskStage } from '../src/workflow.ts'

const catalog = { profiles: [{ id: 'ark', model: 'doubao-image', ready: true, revision: 'revision-3' }, { id: 'openai', model: 'gpt-image', ready: false, revision: 'revision-5' }], built_in: { id: 'codex', ready: true } }
const legacy = { checkpoint: 'old-local', max_attempts: 2 }
assert.equal(approvedProvider(legacy), 'local')
assert.equal(imageScopeChanged(legacy, 'local', 'old-local', 'high', 2, catalog), false, 'Cloud quality cannot change an old local approval')
assert.equal(imageScopeChanged(legacy, 'ark', 'old-local', 'medium', 2, catalog), true, 'Switching from local requires fresh approval')
assert.equal(imageScopeChanged(legacy, 'local', 'new-local', 'medium', 2, catalog), true)

const cloud = { max_attempts: 2, image_target: { id: 'ark', provider: 'ark', model: 'doubao-image', config_revision: 'revision-3', quality: 'medium' } }
const changed = (approval = cloud, provider = 'ark', quality = 'medium', attempts = 2, config = catalog) => imageScopeChanged(approval, provider, 'unavailable-local', quality, attempts, config)
assert.equal(changed(), false, 'Cloud authorization is independent of the local checkpoint')
assert.equal(changed(undefined, 'openai'), true)
assert.equal(changed(undefined, 'ark', 'high'), true)
assert.equal(changed(undefined, 'ark', 'medium', 3), true)
assert.equal(changed({ ...cloud, image_target: { ...cloud.image_target, model: 'other-model' } }), true)
assert.equal(changed(undefined, 'ark', 'medium', 2, { ...catalog, profiles: [{ ...catalog.profiles[0], revision: 'revision-4' }] }), true)
assert.equal(changed(undefined, 'ark', 'medium', 2, { ...catalog, profiles: [] }), true, 'A disappeared cloud profile cannot inherit an approval')
assert.equal(imageScopeChanged(null, 'ark', '', 'medium', 1, catalog), false)
assert.equal(targetReady('ark', false, catalog), true, 'Cloud readiness does not depend on ComfyUI')
assert.equal(targetReady('openai', true, catalog), false)
assert.equal(targetReady('codex', false, catalog), true)
assert.equal(targetReady('local', false, catalog), false)
assert.deepEqual(approvalRequest(4, 'ark', 'private-old-checkpoint', 'high', 1), { version: 4, image_provider: 'ark', cloud_quality: 'high', max_attempts: 1, confirm: true })
assert.deepEqual(approvalRequest(4, 'local', 'checkpoint', 'high', 1), { version: 4, image_provider: 'local', checkpoint: 'checkpoint', max_attempts: 1, confirm: true })
const codex = { max_attempts: 1, image_target: { id: 'codex', provider: 'codex', model: 'codex-imagegen', quality: 'medium', config_revision: 'codex-bridge-v1' } }
assert.equal(imageScopeChanged(codex, 'codex', '', 'medium', 1, catalog), false, 'The actual backend Codex bridge revision remains authorized immediately after approval')
assert.equal(imageScopeChanged({ ...codex, image_target: { ...codex.image_target, config_revision: 'other-bridge' } }, 'codex', '', 'medium', 1, catalog), true, 'A genuinely changed Codex bridge still needs new authorization')
assert.ok(activeJobStates.includes('awaiting_agent') && activeTaskStates.includes('awaiting_agent'))
assert.equal(taskStage({ status: 'awaiting_agent', jobs: [], exports: [{ filename: 'old.docx' }], assets: [], approval: {} }), 4, 'An unresolved agent job resumes at waiting state ahead of old exports')
assert.equal(taskStage({ status: 'review', jobs: [{ status: 'awaiting_agent' }], exports: [], assets: [], approval: {} }), 4)

const taskChoices = new Map([['created-this-session', 'codex']])
assert.equal(taskImageProvider({ id: 'old-unapproved-draft', approval: null }, taskChoices), 'local', 'Opening an old draft never inherits the new-task Codex default')
assert.equal(taskImageProvider({ id: 'created-this-session', approval: null }, taskChoices), 'codex', 'A newly created task retains its initial recommendation through analysis and plan versions')
taskChoices.set('created-this-session', 'ark')
assert.equal(taskImageProvider({ id: 'created-this-session', approval: null }, taskChoices), 'ark', 'A chosen service survives plan revisions without consulting changing bootstrap defaults')
assert.equal(taskImageProvider({ id: 'created-this-session', approval: codex }, taskChoices), 'codex', 'An approved task restores its frozen provider ahead of transient choices')
assert.equal(taskImageProvider({ id: 'old-approved-local', approval: legacy }, taskChoices), 'local')
const historicalCloud = { id: 'historical-cloud', approval: cloud }
taskChoices.set(historicalCloud.id, taskImageProvider(historicalCloud, taskChoices))
assert.equal(taskImageProvider({ ...historicalCloud, approval: null }, taskChoices), 'ark', 'After loading cloud authorization, plan edits retain that provider when approval is invalidated')

const settings = readFileSync(new URL('../src/CloudSettings.tsx', import.meta.url), 'utf8')
const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
assert.doesNotMatch(settings + app, /localStorage|sessionStorage/, 'API credentials never persist in browser storage')
assert.match(settings, /type="password" value=\{key\}/)
assert.match(settings, /finally \{ if \(alive\.current\) \{ setKey\(''\)/, 'Save clears the ephemeral password even after an uncertain response')
assert.doesNotMatch(settings, /profile\.(?:api_key|key_file|key_env)/, 'Stored credentials cannot be echoed')
assert.doesNotMatch(settings, /result\.error|err\.message/, 'Cloud configuration errors never echo raw provider response text')
assert.match(settings, /更换服务地址需重新提供密钥/)
assert.match(app, /provider !== 'local' \|\| !regionalPlan/, 'Cloud generation bypasses local region-node readiness')
assert.match(app, /provider === 'openai' \? <label>图片质量/, 'Only the GPT compatible API exposes medium/high quality')
assert.match(app, /豆包使用 2K 尺寸，具体画质由模型决定/)
assert.match(app, /GPT 内置按画幅生成，具体质量以实际图片为准/)
assert.match(app, /文章理解仍在本机完成/)
assert.match(app, /网页本身不能独立调用 ChatGPT 账户/)
console.log('PASS: legacy local approvals; cloud provider/model/revision/quality freeze; independent readiness; waiting-agent locks/recovery; no storage/key echo; explicit outbound scope')
