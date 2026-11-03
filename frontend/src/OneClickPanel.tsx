import type { ReactNode } from 'react'
import type { DocumentFormat, Task } from './types'
import type { WorkflowMode } from './oneClick'
import { automationActive, canStartOneClick, oneClickExport } from './oneClick'
import Icon from './Icons'

export function WorkflowModeSwitch({ mode, disabled, onChange }: { mode: WorkflowMode; disabled: boolean; onChange: (mode: WorkflowMode) => void }) {
  return <div className="workflow-mode-bar"><div><span className="eyebrow">创作方式</span><p>{mode === 'one-click' ? '交给图序，自动完成图文文档。' : '逐步审阅，把画面调整到满意。'}</p></div><div className="workflow-mode-switch" role="group" aria-label="创作模式"><button type="button" aria-pressed={mode === 'one-click'} onClick={() => onChange('one-click')} disabled={disabled}><Icon name="spark" size={16} />一键生成</button><button type="button" aria-pressed={mode === 'manual'} onClick={() => onChange('manual')} disabled={disabled}><Icon name="settings" size={16} />人工设置</button></div></div>
}

const labels = ['阅读文章', '推荐画面', '生成图片', '插入排版', '文档就绪']
const stepIndex: Record<string, number> = { analysis: 0, approval: 1, generation: 2, selection: 3, export: 3, completed: 4 }
export default function OneClickPanel({ task, format, onFormat, providerLabel, textModel, ready, readiness, busy, onStart, onManual, children }: {
  task: Task; format: DocumentFormat; onFormat: (value: DocumentFormat) => void; providerLabel: string; textModel: string;
  ready: boolean; readiness: string; busy: boolean; onStart: () => void; onManual: () => void; children: ReactNode
}) {
  const run = task.automation
  const running = automationActive(task)
  const startable = canStartOneClick(task)
  const output = oneClickExport(task)
  const currentStep = run ? (stepIndex[run.stage] ?? 0) : -1
  const completed = !!output
  return <section className="one-click-panel card" aria-labelledby="one-click-heading">
    <div className="one-click-heading"><div><span className="eyebrow">ARTICLE TO DOCUMENT</span><h2 id="one-click-heading">一篇文章，一次完成。</h2><p>按语义推荐画风，为适合的段落配图，自动插入并生成新文档。</p></div><span className="one-click-mark"><Icon name="spark" size={28} /></span></div>
    {!run && startable && <><div className="one-click-options">{children}<label>导出格式<select value={format} onChange={event => onFormat(event.target.value as DocumentFormat)} disabled={busy}><option value="docx">Word 文档（DOCX）</option><option value="markdown">Markdown 图片包</option><option value="html">HTML 文档</option></select></label></div><div className="one-click-scope"><strong>点击即授权本次自动制作</strong><p>使用本地 {textModel || '文字模型'} 分析文章和组织画面，再交给{providerLabel}生成。根据全文建议最多5张，每张仅尝试1次；自动采用有效原图、插入图注并导出{format === 'docx' ? 'Word' : format === 'markdown' ? 'Markdown' : 'HTML'}副本。原稿保留不覆盖。</p><p>生图服务可能产生调用费用。失败或结果未知会停止，不自动重试。建议无需配图时，直接导出正文。</p></div><button className="primary one-click-start" onClick={onStart} disabled={!ready || busy}><Icon name="spark" />一键生成图文文档<Icon name="arrow" /></button>{!ready && <p className="inline-warning" role="status">{readiness}</p>}</>}
    {!run && !startable && <div className="one-click-existing"><h3>这篇文章已有制作记录</h3><p>可以切换人工设置继续处理。新导入的文章可从这里一键制作，不会覆盖已有方案。</p><button className="secondary" onClick={onManual} disabled={busy}>使用人工设置<Icon name="arrow" /></button></div>}
    {run && <><ol className="one-click-steps" aria-label="一键制作进度">{labels.map((label, index) => <li key={label} className={completed || index < currentStep ? 'done' : index === currentStep ? 'current' : ''} aria-current={!completed && index === currentStep ? 'step' : undefined}><span>{completed || index < currentStep ? <Icon name="check" size={15} /> : index + 1}</span>{label}</li>)}</ol><div className={`one-click-status ${completed ? 'complete' : ''}`} role="status" aria-live="polite"><strong>{completed ? '图文文档已就绪' : run.status === 'completed' ? '当前文档已有变化' : run.status === 'cancelled' ? '自动流程已停止' : ['failed', 'paused'].includes(run.status) ? '自动流程需要处理' : '图序正在制作'}</strong><p>{run.status === 'completed' && !completed ? '当前方案或导出记录已改变，请进入人工设置查看并重新导出。' : run.message}</p>{run.total_images > 0 && <small>已返回 {run.completed_images} / {run.total_images} 张图片</small>}{completed && run.total_images === 0 && <small>语义分析建议无需新增插图，已保留正文。</small>}</div>{output?.url && <a className="primary one-click-download" href={output.url} target="_blank" rel="noreferrer"><Icon name="download" />下载{output.format === 'docx' ? ' Word 文档' : output.format === 'markdown' ? ' Markdown 图片包' : ' HTML 文档'}</a>}<button className="secondary one-click-manual" onClick={onManual} disabled={busy}>{running ? '停止自动流程，改为人工设置' : '查看图片与文档，进入人工设置'}<Icon name="arrow" /></button>{running && <p className="scope-note">任务已保存在本机服务中。关闭网页不会重新提交；停止后保留已经返回的图片。</p>}</>}
  </section>
}
