import { useEffect, useRef, useState } from 'react'
import { request } from './api'
import { canAdoptModelTest, cloudDraftChanged, safeModelIds } from './cloudVerification'
import ProviderVerification from './ProviderVerification'
import type { CloudCatalog, CloudProfile, CloudTestResult } from './types'

type RefreshSource = 'manual' | 'save' | 'test'
type Refresh = (source: RefreshSource) => Promise<CloudCatalog>

function ProviderSettings({ profile, locked, refreshEpoch, onUpdate }: { profile: CloudProfile; locked: boolean; refreshEpoch: number; onUpdate: Refresh }) {
  const [label, setLabel] = useState(profile.label)
  const [baseUrl, setBaseUrl] = useState(profile.base_url)
  const [model, setModel] = useState(profile.model)
  const [enabled, setEnabled] = useState(profile.enabled)
  const [key, setKey] = useState('')
  const [clearKey, setClearKey] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [failed, setFailed] = useState(false)
  const [candidates, setCandidates] = useState<string[]>([])
  const sequence = useRef(0)
  const currentProfile = useRef(profile)
  const alive = useRef(true)
  currentProfile.current = profile
  const dirty = cloudDraftChanged(profile, { label, base_url: baseUrl, model, enabled, key, clear_key: clearKey })
  useEffect(() => { alive.current = true; return () => { alive.current = false; sequence.current++ } }, [])
  // Verification-only updates from this test preserve its new candidates. A
  // manual refresh, save, or configuration change always clears old results.
  useEffect(() => {
    sequence.current++
    setLabel(profile.label); setBaseUrl(profile.base_url); setModel(profile.model); setEnabled(profile.enabled)
    setKey(''); setClearKey(false); setCandidates([]); setMessage(''); setFailed(false)
  }, [profile.label, profile.base_url, profile.model, profile.enabled, profile.key_present, profile.revision, refreshEpoch])
  const save = async () => {
    if (locked || busy || !dirty) return
    const id = ++sequence.current
    setBusy(true); setMessage(''); setFailed(false); setCandidates([])
    try {
      await request(`/image-providers/${profile.id}`, 'PUT', { label, base_url: baseUrl, model, enabled, ...(key.trim() ? { api_key: key.trim() } : {}), ...(clearKey ? { clear_key: true } : {}) })
      await onUpdate('save')
    } catch { if (alive.current && id === sequence.current) { setFailed(true); setMessage('设置未保存。请检查 HTTPS 服务地址、模型名称与本地服务连接。') } }
    finally { if (alive.current) { setKey(''); setClearKey(false); setBusy(false) } }
  }
  const test = async () => {
    if (locked || busy || dirty || !profile.ready) return
    const id = ++sequence.current, revision = profile.revision
    setBusy(true); setMessage(''); setFailed(false); setCandidates([])
    try {
      const result = await request<CloudTestResult>(`/image-providers/${profile.id}/test`, 'POST', {})
      if (!alive.current || id !== sequence.current || result.revision !== revision || currentProfile.current.revision !== revision) return
      const refreshed = await onUpdate('test')
      const saved = refreshed.profiles.find(item => item.id === profile.id)
      if (!alive.current || !saved || id !== sequence.current || saved.revision !== revision || currentProfile.current.revision !== revision) return
      if (canAdoptModelTest(revision, saved.revision, result, id, sequence.current)) {
        const models = safeModelIds(profile.kind, result.models)
        setCandidates(models)
        setMessage(models.length ? '已读取已保存配置的候选模型。选择后请点击保存；模型可见不代表生图权限。' : '连接查询通过，未返回可选生图模型。仍可手填模型名称或已授权的 ep 接入点。')
      } else { setFailed(true); setMessage('连接查询未通过。请查看当前验证状态中的诊断，检查后再测试。') }
    } catch { if (alive.current && id === sequence.current) { setFailed(true); setMessage('连接测试未完成。请检查本地服务与已保存配置。') } }
    finally { if (alive.current) setBusy(false) }
  }
  return <div className="cloud-profile">
    <div className="cloud-profile-heading"><strong>{profile.kind === 'ark' ? '豆包 / 火山方舟' : 'GPT 兼容 API'}</strong><span>{profile.ready ? '配置完整' : '待配置'}</span></div>
    <ProviderVerification profile={profile} />
    <fieldset disabled={locked || busy}>
      <div className="cloud-config-grid">
        <label>显示名称<input value={label} maxLength={80} onChange={event => setLabel(event.target.value)} /></label>
        <label>模型名称<input value={model} maxLength={160} placeholder={profile.kind === 'ark' ? '模型标识或已授权的 ep 接入点' : '填写服务支持的图像模型标识'} onChange={event => setModel(event.target.value)} /></label>
        {candidates.length > 0 && <label className="cloud-wide">已保存配置的候选模型<select value="" onChange={event => { if (event.target.value) { const needsSave = cloudDraftChanged(profile, { label, base_url: baseUrl, model: event.target.value, enabled, key, clear_key: clearKey }); setModel(event.target.value); setMessage(needsSave ? '候选模型已带入输入框，请点击保存后再测试或生成。' : '已选择当前保存的模型，可以测试或继续生成。'); setFailed(false) } }}><option value="">选择一个候选，带入模型输入框</option>{candidates.map(id => <option key={id} value={id}>{id}</option>)}</select></label>}
        <label className="cloud-wide">API 地址<input value={baseUrl} maxLength={300} placeholder="https://…" spellCheck={false} onChange={event => setBaseUrl(event.target.value)} /></label>
        <label className="cloud-wide">API 密钥<input type="password" value={key} maxLength={1024} autoComplete="new-password" spellCheck={false} placeholder={profile.key_present ? '已保存密钥；留空保留，填写可替换' : '填写 API 密钥'} onChange={event => { setKey(event.target.value); if (event.target.value) setClearKey(false) }} /><small className="muted">密钥保存在本机后台，保存后输入框会清空。更换服务地址需重新提供密钥。</small></label>
      </div>
      <div className="cloud-options"><label className="checkbox-label"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} /><span>启用此服务</span></label>{profile.key_present && <label className="checkbox-label"><input type="checkbox" checked={clearKey} onChange={event => { setClearKey(event.target.checked); if (event.target.checked) setKey('') }} /><span>清除已保存密钥</span></label>}</div>
      {dirty && <p role="status" className="cloud-dirty">有未保存的修改，请先保存。测试与生成使用已保存配置。</p>}
      <div className="button-group"><button type="button" className="secondary" onClick={() => void save()} disabled={!dirty}>保存设置</button><button type="button" className="text-button" onClick={() => void test()} disabled={!profile.ready || dirty}>测试已保存配置</button></div>
    </fieldset>
    {message && <p role={failed ? 'alert' : 'status'} className={failed ? 'inline-warning' : 'cloud-result'}>{message}</p>}
  </div>
}

export default function CloudSettings({ catalog, locked, onChange }: { catalog?: CloudCatalog; locked: boolean; onChange: (catalog: CloudCatalog) => void }) {
  const [loadError, setLoadError] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [refreshEpoch, setRefreshEpoch] = useState(0)
  const publishedCatalog = useRef(catalog)
  const loadSequence = useRef(0)
  useEffect(() => {
    if (catalog !== publishedCatalog.current) { publishedCatalog.current = catalog; loadSequence.current++; setRefreshing(false); setRefreshEpoch(value => value + 1) }
  }, [catalog])
  const refresh: Refresh = async source => {
    const id = ++loadSequence.current
    setRefreshing(true)
    try {
      const result = await request<CloudCatalog>('/image-providers')
      if (id !== loadSequence.current) { if (publishedCatalog.current) return publishedCatalog.current; throw new Error('配置已更新') }
      publishedCatalog.current = result
      if (source !== 'test') setRefreshEpoch(value => value + 1)
      onChange(result); setLoadError(false)
      return result
    } finally { if (id === loadSequence.current) setRefreshing(false) }
  }
  return <details className="cloud-settings card" onToggle={event => { if (event.currentTarget.open) void refresh('manual').catch(() => setLoadError(true)) }}>
    <summary>云端生图 API 配置 <span>可选</span></summary>
    <div className="cloud-settings-body"><p className="muted">可以在导入文章前配置。只在确认生成后发送所选画面的描述与构图，文章理解仍在本机完成。</p><button type="button" className="text-button cloud-refresh" disabled={locked || refreshing} onClick={() => void refresh('manual').catch(() => setLoadError(true))}>{refreshing ? '刷新配置中……' : '刷新配置与验证状态'}</button>{catalog?.profiles.map(profile => <ProviderSettings key={profile.id} profile={profile} locked={locked || refreshing} refreshEpoch={refreshEpoch} onUpdate={refresh} />)}{!catalog && <p className="muted">读取配置中……</p>}{loadError && <p role="alert" className="inline-warning">配置暂未读取，请重新打开此面板或刷新服务。</p>}<p className="cloud-settings-note">GPT 内置生图需要支持生图工具的 Codex 宿主会话协作，无需在此填写密钥。普通浏览器请使用自己的 API 或本地生图服务。</p></div>
  </details>
}
