import { systemText, t } from './i18n'
import type { ReactNode } from 'react'
import type { DocumentFormat, Task } from './types'
import type { WorkflowMode } from './oneClick'
import { automationActive, canStartOneClick, oneClickExport } from './oneClick'
import Icon from './Icons'

export function WorkflowModeSwitch({ mode, disabled, onChange }: { mode: WorkflowMode; disabled: boolean; onChange: (mode: WorkflowMode) => void }) {
  return <div className="workflow-mode-bar"><div><span className="eyebrow">{t("创作方式")}</span><p>{mode === 'one-click' ? t("交给图序，自动完成图文文档。") : t("逐步审阅，把画面调整到满意。")}</p></div><div className="workflow-mode-switch" role="group" aria-label={t("创作模式")}><button type="button" aria-pressed={mode === 'one-click'} onClick={() => onChange('one-click')} disabled={disabled}><Icon name="spark" size={16} />{t("一键生成")}</button><button type="button" aria-pressed={mode === 'manual'} onClick={() => onChange('manual')} disabled={disabled}><Icon name="settings" size={16} />{t("人工设置")}</button></div></div>
}

export default function OneClickPanel({ task, format, onFormat, providerLabel, textModel, visualReviewEnabled = false, ready, readiness, busy, onStart, onManual, children }: {
  task: Task; format: DocumentFormat; onFormat: (value: DocumentFormat) => void; providerLabel: string; textModel: string;
  visualReviewEnabled?: boolean; ready: boolean; readiness: string; busy: boolean; onStart: () => void; onManual: () => void; children: ReactNode
}) {
  const run = task.automation
  const running = automationActive(task)
  const startable = canStartOneClick(task)
  const output = oneClickExport(task)
  const checking = run?.stage === 'visual_review' || task.assets.some(asset => !!asset.visual_review)
  const labels = [t("阅读文章"), t("推荐画面"), t("生成图片"), ...(checking ? [t("检查画面")] : []), t("插入排版"), t("文档就绪")]
  const stepIndex: Record<string, number> = { analysis: 0, approval: 1, generation: 2, visual_review: 3, selection: checking ? 4 : 3, export: checking ? 4 : 3, completed: checking ? 5 : 4 }
  const currentStep = run ? (stepIndex[run.stage] ?? 0) : -1
  const completed = !!output
  return <section className="one-click-panel card" aria-labelledby="one-click-heading">
    <div className="one-click-heading"><div><span className="eyebrow">ARTICLE TO DOCUMENT</span><h2 id="one-click-heading">{t("一篇文章，一次完成。")}</h2><p>{t("按语义推荐画风，为适合的段落配图，自动插入并生成新文档。")}</p></div><span className="one-click-mark"><Icon name="spark" size={28} /></span></div>
    {!run && startable && <><div className="one-click-options">{children}<label>{t("导出格式")}<select value={format} onChange={event => onFormat(event.target.value as DocumentFormat)} disabled={busy}><option value="docx">{t("Word 文档（DOCX）")}</option><option value="markdown">{t("Markdown 图片包")}</option><option value="html">{t("HTML 文档")}</option></select></label></div><div className="one-click-scope"><strong>{t("点击即授权本次自动制作")}</strong><p>{t("使用本地")}{' '}{textModel || t("文字模型")}{' '}{t("分析文章和组织画面，再交给")}{providerLabel}{t("生成。根据全文建议最多5张，每张仅尝试1次；自动采用有效原图、插入图注并导出")}{format === 'docx' ? 'Word' : format === 'markdown' ? 'Markdown' : 'HTML'}{t("副本。原稿保留不覆盖。")}</p><p>{visualReviewEnabled ? t("已启用本地图片检查：生成后用本地模型核对画面；有差异或无法确认时停止自动后续步骤，交由你审阅。") : t("图片检查未启用，可在设置中开启。生成结果仍需要人工核对。")}</p><p>{t("生图服务可能产生调用费用。失败或结果未知会停止，不自动重试。建议无需配图时，直接导出正文。")}</p></div><button className="primary one-click-start" onClick={onStart} disabled={!ready || busy}><Icon name="spark" />{t("一键生成图文文档")}<Icon name="arrow" /></button>{!ready && <p className="inline-warning" role="status">{readiness}</p>}</>}
    {!run && !startable && <div className="one-click-existing"><h3>{t("这篇文章已有制作记录")}</h3><p>{t("可以切换人工设置继续处理。新导入的文章可从这里一键制作，不会覆盖已有方案。")}</p><button className="secondary" onClick={onManual} disabled={busy}>{t("使用人工设置")}<Icon name="arrow" /></button></div>}
    {run && <><ol className="one-click-steps" aria-label={t("一键制作进度")}>{labels.map((label, index) => <li key={label} className={completed || index < currentStep ? 'done' : index === currentStep ? 'current' : ''} aria-current={!completed && index === currentStep ? 'step' : undefined}><span>{completed || index < currentStep ? <Icon name="check" size={15} /> : index + 1}</span>{label}</li>)}</ol><div className={`one-click-status ${completed ? 'complete' : ''}`} role="status" aria-live="polite"><strong>{completed ? t("图文文档已就绪") : run.status === 'completed' ? t("当前文档已有变化") : run.status === 'cancelled' ? t("自动流程已停止") : ['failed', 'paused'].includes(run.status) ? t("自动流程需要处理") : t("图序正在制作")}</strong><p>{run.status === 'completed' && !completed ? t("当前方案或导出记录已改变，请进入人工设置查看并重新导出。") : systemText(run.message)}</p>{run.total_images > 0 && <small>{t("已返回")}{' '}{run.completed_images} / {run.total_images}{' '}{t("张图片")}</small>}{completed && run.total_images === 0 && <small>{t("语义分析建议无需新增插图，已保留正文。")}</small>}</div>{output?.url && <a className="primary one-click-download" href={output.url} target="_blank" rel="noreferrer"><Icon name="download" />{t("下载")}{output.format === 'docx' ? t(" Word 文档") : output.format === 'markdown' ? t(" Markdown 图片包") : t(" HTML 文档")}</a>}<button className="secondary one-click-manual" onClick={onManual} disabled={busy}>{running ? t("停止自动流程，改为人工设置") : t("查看图片与文档，进入人工设置")}<Icon name="arrow" /></button>{running && <p className="scope-note">{t("任务已保存在本机服务中。关闭网页不会重新提交；停止后保留已经返回的图片。")}</p>}</>}
  </section>
}
