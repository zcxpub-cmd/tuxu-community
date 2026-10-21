import { verificationLabels } from './cloudVerification'
import type { CloudProfile } from './types'

function timestamp(value?: string) {
  if (!value || !Number.isFinite(Date.parse(value))) return ''
  return new Date(value).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export default function ProviderVerification({ profile }: { profile: CloudProfile }) {
  const labels = verificationLabels(profile)
  const verification = profile.verification
  return <div className="provider-verification" aria-label={`${profile.label}验证状态`}><div><span>配置</span><strong>{labels.configuration}</strong></div><div><span>连接与列表</span><strong>{labels.connection}</strong><small>{labels.model}{timestamp(verification?.connection_at) ? ` · ${timestamp(verification?.connection_at)}` : ''}</small></div><div><span>实际生图</span><strong>{labels.generation}</strong>{timestamp(verification?.generation_at) && <small>{timestamp(verification?.generation_at)}</small>}</div>{verification?.message && <p className="provider-diagnosis">{verification.message}</p>}<p className="provider-verification-note">模型可见不代表生图权限；最近记录不保证权限长期有效。</p></div>
}
