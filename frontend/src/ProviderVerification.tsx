import { currentLocale, systemText, t } from './i18n'
import { verificationLabels } from './cloudVerification'
import type { CloudProfile } from './types'

function timestamp(value?: string) {
  if (!value || !Number.isFinite(Date.parse(value))) return ''
  return new Date(value).toLocaleString(currentLocale() === 'en' ? 'en-US' : 'zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export default function ProviderVerification({ profile }: { profile: CloudProfile }) {
  const labels = verificationLabels(profile)
  const verification = profile.verification
  return <div className="provider-verification" aria-label={t("{0}验证状态", t(profile.label))}><div><span>{t("配置")}</span><strong>{t(labels.configuration)}</strong></div><div><span>{t("连接与列表")}</span><strong>{t(labels.connection)}</strong><small>{t(labels.model)}{timestamp(verification?.connection_at) ? ` · ${timestamp(verification?.connection_at)}` : ''}</small></div><div><span>{t("实际生图")}</span><strong>{t(labels.generation)}</strong>{timestamp(verification?.generation_at) && <small>{timestamp(verification?.generation_at)}</small>}</div>{verification?.message && <p className="provider-diagnosis">{systemText(verification.message)}</p>}<p className="provider-verification-note">{t("模型可见不代表生图权限；最近记录不保证权限长期有效。")}</p></div>
}
