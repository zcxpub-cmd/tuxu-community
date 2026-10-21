import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

const root = fileURLToPath(new URL('..', import.meta.url))
const app = readFileSync(resolve(root, 'src/App.tsx'), 'utf8')
assert.doesNotMatch(app, /\.(?:prompts|prompt|negative_prompt|regions)\b/, 'The view must never read private generation text from tasks or assets')
assert.doesNotMatch(app, /\/prompts\b|savePrompts|promptDrafts|promptDirty/, 'The client exposes no raw-prompt editor or write endpoint')
assert.match(app, /prompt_summaries/, 'Confirmation uses only the public Chinese intent summary')
const bundles = readdirSync(resolve(root, 'dist/assets')).filter(name => name.endsWith('.js'))
assert.ok(bundles.length > 0, 'Check the deployed production build, not only the source')
for (const filename of bundles) {
  const code = readFileSync(resolve(root, 'dist/assets', filename), 'utf8')
  assert.doesNotMatch(code, /\/prompts\b|Director controls:|正向提示词|负向提示词|局部提示词/, 'Deployed JavaScript contains no generation template or prompt-editing controls')
}
const director = readFileSync(resolve(root, 'src/DirectorPanel.tsx'), 'utf8')
assert.doesNotMatch(director, /\.(?:prompts|prompt|negative_prompt|regions)\b/)
assert.doesNotMatch(director, /preset-swatch/, 'Style choices are plain options, not simulated sample thumbnails')
assert.match(director, /compact-style-selector/, 'Narrow views retain all styles through a compact selector')
assert.match(director, /details className="director-advanced"/, 'Optional camera, lighting and exact geometry start collapsed')
console.log('PASS: source and deployed bundle expose no raw prompts, templates or prompt editing; Chinese intent and compact/director controls remain available')
