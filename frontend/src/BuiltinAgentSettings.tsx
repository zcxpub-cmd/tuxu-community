import { t } from './i18n'
import { useEffect, useState } from 'react'
import type { BuiltinAgentCatalog, BuiltinAgentDraft } from './types'
import { builtinDraftChanged, builtinModels } from './builtinAgent'
import Icon from './Icons'

export default function BuiltinAgentSettings({ catalog, models, available, locked, onSave }: {
  catalog?: BuiltinAgentCatalog; models?: string[]; available: boolean; locked: boolean;
  onSave: (draft: BuiltinAgentDraft) => Promise<boolean>
}) {
  const [model, setModel] = useState(catalog?.model || '')
  const [enabled, setEnabled] = useState(!!catalog?.visual_review_enabled)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  useEffect(() => {
    setModel(catalog?.model || ''); setEnabled(!!catalog?.visual_review_enabled); setSaved(false)
  }, [catalog?.revision])
  const dirty = !!catalog && builtinDraftChanged(catalog, { model, visual_review_enabled: enabled, expected_revision: catalog.revision })
  const options = builtinModels(catalog, models)
  const save = async () => {
    if (!catalog || locked || saving || !dirty || !model) return
    setSaving(true); setSaved(false)
    try { if (await onSave({ model, visual_review_enabled: enabled, expected_revision: catalog.revision })) setSaved(true) }
    finally { setSaving(false) }
  }
  return <section className="builtin-agent-settings card" aria-labelledby="builtin-agent-heading">
    <div className="card-bar"><span id="builtin-agent-heading"><Icon name="spark" size={17} />{t("图序内置视觉导演")}</span><span className={`builtin-status ${catalog?.visual_review_enabled ? 'enabled' : ''}`}>{catalog?.visual_review_enabled ? t("图片检查已启用") : t("图片检查未启用 · 推荐开启")}</span></div>
    <div className="builtin-agent-body"><p>{t("阅读文章、编写画面描述、检查图片，三个角色共用一个本地模型，依次工作。图片生成服务仍在下方单独配置。")}</p>
      {!catalog ? <p role="status" className="muted">{t("正在读取内置导演设置……")}</p> : <fieldset disabled={locked || saving}>
        <div className="builtin-agent-controls"><label>{t("本地导演模型")}<select value={model} onChange={event => { setModel(event.target.value); setSaved(false) }} disabled={!options.length}>{!options.length && <option value="">{t("请先启动 Ollama")}</option>}{options.map(name => <option key={name} value={name}>{name}{models?.length && !models.includes(name) ? t("（当前未检测到）") : ''}</option>)}</select><small>{t("使用本机 Ollama；图片检查需要具备看图能力的模型。")}</small></label>
        <label className="builtin-review-toggle"><input type="checkbox" role="switch" checked={enabled} onChange={event => { setEnabled(event.target.checked); setSaved(false) }} /><span><strong>{t("生成后检查图片")}</strong><small>{enabled ? t("用本地模型核对主体、数量、摆放、画风和文字。") : t("默认关闭，建议开启；会增加一次本地看图处理。")}</small></span></label></div>
        <p className="builtin-agent-note">{t("保存后只对新授权生效，已有方案和已提交任务按原记录执行。检查不确定时停止自动后续步骤，交由你审阅，不会自动重画。")}</p>
        {dirty && <p className="builtin-agent-dirty" role="status">{t("有未保存的修改；当前任务仍使用已保存设置。")}</p>}
        <button type="button" className="secondary" disabled={!dirty || !model || saving} onClick={() => void save()}>{saving ? t("正在保存……") : t("保存导演设置")}</button>
      </fieldset>}
      {!available && <p className="inline-warning" role="status">{t("本地 Ollama 尚未就绪，请启动后刷新模型状态。保存配置不代表模型已可用。")}</p>}
      {saved && <p className="builtin-agent-saved" role="status">{t("导演设置已保存，仅对新授权生效。")}</p>}
    </div>
  </section>
}
