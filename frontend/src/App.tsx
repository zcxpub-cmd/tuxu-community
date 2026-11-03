import { Fragment, useEffect, useRef, useState } from 'react'
import gsap from 'gsap'
import { useGSAP } from '@gsap/react'
import CinematicEntry from './CinematicEntry'
import NavigationConfirm from './NavigationConfirm'
import ThemeSwitcher from './ThemeSwitcher'
import { useTheme } from './theme'
import OneClickPanel, { WorkflowModeSwitch } from './OneClickPanel'
import { automationActive, canStartOneClick, oneClickKey, readWorkflowMode, saveWorkflowMode } from './oneClick'
import type { WorkflowMode } from './oneClick'
import { createTaskPollGate } from './taskPollGate'
import Icon from './Icons'
import { getInitialScene, pickVisitScene } from './sceneLibrary'
import type { ReactNode } from 'react'
import { request, setToken } from './api'
import { beginCatalogRefresh, createCatalogGate, releaseUnpublishedRefresh } from './catalogGate'
import type { CatalogRefreshEntry } from './catalogGate'
import { resolveGenerationKey } from './generationKey'
import { canConfirmExport, insertableParagraph, selectedFigures } from './documentPreview'
import { applyVisualSettings, directorFor, objectPosition, planHasRegions, summarizeDirector } from './director'
import DirectorPanel from './DirectorPanel'
import CloudSettings from './CloudSettings'
import ProviderVerification from './ProviderVerification'
import { activeJobStates, activeTaskStates, approvalRequest, imageScopeChanged, approvedProvider, taskImageProvider, targetModel, targetReady } from './imageProviders'
import { completedStages, stages, taskStage } from './workflow'
import type { Asset, Bootstrap, CloudCatalog, CloudQuality, Director, DocumentFormat, Genre, ImageProvider, Plan, Selection, Slot, Task } from './types'

const genres: Record<Genre, string> = { academic: '科普', news: '新闻 / 传播', novel: '小说', report: '报告' }
const statusNames: Record<string, string> = { imported: '已导入', analyzing: '正在阅读', plan_ready: '方案待确认', approved: '已授权', generating: '正在生成', awaiting_agent: '等待智能体图片', review: '待选图', exported: '已导出', failed: '需要处理', cancelled: '已取消' }
const sample = { title: '深海观测：让海底变化留下记录', text: '深海观测：让海底变化留下记录\n\n深海看似平静，却存在流动、温度变化和沉积物运动。研究者通过观测设备获取这些变化的记录，再结合不同来源的数据理解海底环境。\n\n一套简化的观测系统可以包含三个功能模块：温度观测、压力观测和流速观测。它们分别提供不同的信息，并通过时间记录帮助研究者进行对照。这里介绍的是功能关系，不对应某一台具体仪器的尺寸和安装方式。\n\n观测工作从明确问题开始。研究者选择观测位置与指标，检查设备，再开展记录。回收后，对数据做质量检查，最后进行分析。这些步骤可以用流程示意帮助读者理解。\n\n单个观测点提供局部信息，多个观测点之间的对照则有助于识别变化的空间差异。图像可以表现观测点与信息联系，但不能把示意布局当成真实部署位置。\n\n本文是为配图工作台编写的自制演示稿，没有实验数据和现场照片。所有插图都应标注为概念示意，不应生成可被误认为实验结果的画面。' }
function Card({ children, className = '' }: { children: ReactNode; className?: string }) { return <section className={`card ${className}`}>{children}</section> }
function Pill({ children, tone = '' }: { children: ReactNode; tone?: string }) { return <span className={`pill ${tone}`}>{children}</span> }
function date(value: string) { return new Date(value).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' }) }
export function NativeImageSizeNotice({ notice }: { notice?: string | null }) {
  return notice?.trim() ? <p className="scope-note">{notice}</p> : null
}
export function NativeImageConfirmation({ provider }: { provider: ImageProvider }) {
  return provider === 'codex' ? <p className="scope-note">原生图片工具可能返回与请求不同的像素尺寸；保持所选横向、纵向或方形方向，完整保留图片，选图时显示实际尺寸。</p> : null
}

gsap.registerPlugin(useGSAP)
export default function App() {
  const appearance = useTheme()
  const [workflowMode, setWorkflowMode] = useState<WorkflowMode>(readWorkflowMode)
  const [automaticFormat, setAutomaticFormat] = useState<DocumentFormat>('docx')
  const [view, setView] = useState<'home' | 'studio'>('home')
  useEffect(() => saveWorkflowMode(workflowMode), [workflowMode])
  const [scene, setScene] = useState(getInitialScene)
  const [motionEnabled, setMotionEnabled] = useState(true)
  const [navigationPending, setNavigationPending] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsSignal, setSettingsSignal] = useState(0)
  const pendingNavigation = useRef<(() => void) | null>(null)
  const studioRoot = useRef<HTMLDivElement>(null)
  const [boot, setBoot] = useState<Bootstrap | null>(null)
  const [tasks, setTasks] = useState<Task[]>([])
  const [task, setTask] = useState<Task | null>(null)
  const [stage, setStage] = useState(0)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [title, setTitle] = useState('')
  const [text, setText] = useState('')
  const [genre, setGenre] = useState<Genre>('report')
  const [style, setStyle] = useState('简洁自然的编辑插画，克制配色，清晰构图')
  const [inputMode, setInputMode] = useState<'paste' | 'file'>('paste')
  const [draft, setDraft] = useState<Plan | null>(null)
  const [planDirty, setPlanDirty] = useState(false)
  const [selectedSlot, setSelectedSlot] = useState('')
  const [textModel, setTextModel] = useState('')
  const [checkpoint, setCheckpoint] = useState('')
  const [imageProvider, setImageProvider] = useState<ImageProvider | ''>('')
  const [cloudQuality, setCloudQuality] = useState<CloudQuality>('medium')
  const [attempts, setAttempts] = useState(2)
  const [confirmed, setConfirmed] = useState(false)
  const [insertConfirmed, setInsertConfirmed] = useState(false)
  const [processedViews, setProcessedViews] = useState<Record<string, boolean>>({})
  const [dragging, setDragging] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const historyButton = useRef<HTMLButtonElement>(null)
  const historyDialog = useRef<HTMLDivElement>(null)
  const activeTask = useRef<string | null>(null)
  const keys = useRef(new Map<string, string>())
  const taskProviders = useRef(new Map<string, ImageProvider>())
  const statusRefreshedJobs = useRef(new Map<string, CatalogRefreshEntry>())
  const catalogGate = useRef(createCatalogGate())
  const taskPollGate = useRef(createTaskPollGate())
  const active = !!task && (automationActive(task) || activeTaskStates.includes(task.status) || task.jobs?.some(j => activeJobStates.includes(j.status)))
  const locked = !!busy || active
  const latestJob = task?.jobs?.at(-1)
  const currentAssets = task?.assets?.filter(a => !a.stale) || []
  const selections = task?.selections || {}
  const figures = task ? selectedFigures(task) : []
  const selectedCount = figures.length
  const approvalId = task?.approval?.id || task?.approval?.approval_id
  const unsaved = planDirty
  const unsavedImport = !task && !!(title.trim() || text.trim())
  const hasUnsavedDraft = unsaved || unsavedImport
  const provider: ImageProvider = imageProvider || boot?.defaults.image_provider || 'local'
  const cloudCatalog = boot?.models.cloud
  const scopeChanged = imageScopeChanged(task?.approval, provider, checkpoint, cloudQuality, attempts, cloudCatalog)
  const checkpointReady = !!boot?.models.comfy.available && !!checkpoint && boot.models.comfy.checkpoints.includes(checkpoint)
  const regionalPlan = planHasRegions(task?.plan || null)
  const regionReady = provider !== 'local' || !regionalPlan || !!boot?.models.comfy.region_control?.available
  const imageReady = targetReady(provider, checkpointReady, cloudCatalog)
  const chosenModel = targetModel(provider, checkpoint, cloudCatalog)
  const chosenProfile = cloudCatalog?.profiles.find(profile => profile.id === provider)
  const providerLabel = provider === 'local' ? '本地模型' : provider === 'codex' ? boot?.models.local_agent?.label || cloudCatalog?.built_in.label || '本机智能体' : cloudCatalog?.profiles.find(profile => profile.id === provider)?.label || (provider === 'ark' ? '豆包 / 火山方舟' : 'GPT 兼容 API')
  const oneClickReady = !!boot?.models.ollama.available && !!textModel && imageReady && (provider !== 'codex' || !!(boot.models.local_agent?.available && boot.models.local_agent.enabled))
  const oneClickReadiness = !boot?.models.ollama.available || !textModel ? '请先启动本地文字模型，或在生图连接设置中刷新服务状态。' : provider === 'codex' && !boot?.models.local_agent?.enabled ? '请先配置并启用本机智能体自动生图，或选择其他已就绪的生图服务。' : '所选生图服务尚未就绪，请在生图连接设置中完成配置。'
  const awaitingAgent = task?.status === 'awaiting_agent' || task?.jobs?.some(job => job.status === 'awaiting_agent')
  const zeroPlan = !!task?.plan && task.plan.slots.length === 0
  const exportReady = canConfirmExport({ plan: task?.plan || null, selectedCount, approvalId, unsaved, scopeChanged, locked })
  const completeStages = completedStages(task, { unsaved, scopeChanged, selectedCount })
  useEffect(() => {
    if (!hasUnsavedDraft) return
    const beforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', beforeUnload)
    return () => window.removeEventListener('beforeunload', beforeUnload)
  }, [hasUnsavedDraft])
  useGSAP(() => {
    if (view !== 'studio' || !studioRoot.current || !motionEnabled || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    gsap.from('.section-heading', { y: 8, opacity: .65, duration: .24, ease: 'power2.out', clearProps: 'transform,opacity' })
  }, { scope: studioRoot, dependencies: [view, stage, task?.id, motionEnabled], revertOnUpdate: true })
  useEffect(() => {
    if (view === 'studio') window.scrollTo({ top: 0, left: 0, behavior: 'instant' })
  }, [view, stage, task?.id])
  useEffect(() => {
    if (settingsOpen && view === 'studio') document.getElementById('interface-settings')?.scrollIntoView({ block: 'start', behavior: 'instant' })
  }, [settingsSignal, view, settingsOpen])
  const guardNavigation = (action: () => void) => {
    if (hasUnsavedDraft) { pendingNavigation.current = action; setNavigationPending(true); return }
    action()
  }
  const closeNavigation = () => { pendingNavigation.current = null; setNavigationPending(false) }
  const discardAndContinue = () => { const action = pendingNavigation.current; closeNavigation(); action?.() }
  const goHome = () => { if (view !== 'home') setScene(pickVisitScene()); setHistoryOpen(false); setView('home') }
  const goStudio = () => setView('studio')
  const openHistory = () => { setView('studio'); setHistoryOpen(true) }
  const openSettings = () => { setView('studio'); setSettingsOpen(true); setSettingsSignal(value => value + 1) }


  const commit = (value: Task, nextStage?: number) => {
    setTask(value); activeTask.current = value.id
    setTasks(previous => [value, ...previous.filter(t => t.id !== value.id)].sort((a, b) => b.updated_at.localeCompare(a.updated_at)))
    if (nextStage !== undefined) setStage(nextStage)
  }
  const publishCatalog = (catalog: CloudCatalog, expectedEpoch?: number) => {
    if (!catalogGate.current.publish(catalog, expectedEpoch)) return false
    setBoot(previous => previous ? { ...previous, models: { ...previous.models, cloud: catalog } } : previous)
    return true
  }
  const commitNew = (value: Task) => { taskProviders.current.set(value.id, boot?.defaults.image_provider || 'local'); commit(value, 0) }
  const perform = async (label: string, operation: () => Promise<void>) => {
    taskPollGate.current.begin()
    setBusy(label); setError(''); setNotice('')
    try { await operation() } catch (err) {
      setError(err instanceof Error ? err.message : '操作未完成，请稍后重试。')
      if (activeTask.current) {
        try { const value = await request<Task>(`/tasks/${activeTask.current}`); if (value.id === activeTask.current) commit(value) } catch { /* preserve the primary failure */ }
      }
    } finally { taskPollGate.current.end(); setBusy('') }
  }
  const bootstrap = async () => {
    const expectedCatalogEpoch = catalogGate.current.capture()
    const data = await request<Bootstrap>('/bootstrap')
    if (data.models.cloud) catalogGate.current.publish(data.models.cloud, expectedCatalogEpoch)
    const currentCatalog = catalogGate.current.latest()
    setToken(data.token); setBoot(currentCatalog ? { ...data, models: { ...data.models, cloud: currentCatalog } } : data); setTasks(data.tasks || [])
    setTextModel(previous => previous || data.defaults.text_model)
    setImageProvider(previous => activeTask.current ? previous : data.defaults.image_provider || 'local')
    setCheckpoint(previous => data.models.comfy.checkpoints.includes(previous) ? previous : data.models.comfy.checkpoints.includes(data.defaults.image_checkpoint) ? data.defaults.image_checkpoint : data.models.comfy.checkpoints[0] || '')
    if (!task && !text) setStyle(data.defaults.style || style)
  }
  useEffect(() => { void perform('连接本地服务', bootstrap) }, []) // bootstrap performs no inference
  useEffect(() => {
    if (!historyOpen) return
    const previousFocus = document.activeElement as HTMLElement | null
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    historyDialog.current?.querySelector<HTMLButtonElement>('button')?.focus()
    return () => {
      document.body.style.overflow = previousOverflow
      if (previousFocus?.isConnected && previousFocus.getClientRects().length) previousFocus.focus()
      else document.querySelector<HTMLButtonElement>('.workspace-tools button')?.focus()
    }
  }, [historyOpen])
  useEffect(() => {
    setDraft(task?.plan ? structuredClone(task.plan) : null); setPlanDirty(false)
    setConfirmed(false); setInsertConfirmed(false)
    setAttempts(task?.approval?.max_attempts ?? Math.min(10, Math.max(1, (task?.plan?.slots.length || 1) * 2)))
    if (task?.approval?.checkpoint) setCheckpoint(task.approval.checkpoint)
    const restoredProvider = task ? taskImageProvider(task, taskProviders.current) : boot?.defaults.image_provider || 'local'
    if (task) taskProviders.current.set(task.id, restoredProvider)
    setImageProvider(restoredProvider)
    setCloudQuality(task?.approval?.image_target?.quality === 'high' ? 'high' : 'medium')
    setSelectedSlot(task?.plan?.slots[0]?.id || '')
  }, [task?.id, task?.plan?.version, task?.approval?.id, task?.approval?.approval_id])
  useEffect(() => {
    if (!active || !task) return
    let cancelled = false, polling = false
    const id = task.id
    const timer = window.setInterval(async () => {
      if (polling) return
      polling = true
      const pollEpoch = taskPollGate.current.capture()
      try {
        const value = await request<Task>(`/tasks/${id}`)
        if (!cancelled && activeTask.current === id && taskPollGate.current.accepts(pollEpoch)) {
          commit(value)
          if (value.automation?.status === 'completed' && value.status === 'exported') setStage(5)
          else if (value.status === 'review') setStage(4)
          else if (value.status === 'plan_ready' && latestJob?.kind === 'analysis') setStage(1)
        }
      } catch (err) { if (!cancelled) setError(`状态暂未更新：${err instanceof Error ? err.message : '服务连接中断'}。任务记录保留，可重新连接。`) }
      finally { polling = false }
    }, 1600)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [task?.id, active])
  useEffect(() => {
    const savedProvider = approvedProvider(task?.approval)
    if (!task || !latestJob || latestJob.kind !== 'generation' || !['ark', 'openai'].includes(savedProvider) || !['completed', 'failed', 'cancelled'].includes(latestJob.status)) return
    const signature = `${task.id}:${latestJob.id}:${latestJob.status}`
    const entry = beginCatalogRefresh(statusRefreshedJobs.current, signature)
    if (!entry) return
    const expectedCatalogEpoch = catalogGate.current.capture()
    let cancelled = false
    const id = task.id
    const release = () => releaseUnpublishedRefresh(statusRefreshedJobs.current, signature, entry)
    // This reads local verification records; it never tests or resubmits the provider.
    void request<CloudCatalog>('/image-providers').then(catalog => {
      if (!cancelled && activeTask.current === id && statusRefreshedJobs.current.get(signature) === entry) entry.published = publishCatalog(catalog, expectedCatalogEpoch)
    }).catch(() => { /* keep the previous verified catalog */ }).finally(release)
    return () => { cancelled = true; release() }
  }, [task?.id, latestJob?.id, latestJob?.status, task?.approval?.image_target?.id, task?.approval?.image_provider])
  const selectTask = (id: string) => { if (id === task?.id) { setView('studio'); return }; guardNavigation(() => { setView('studio'); void perform('恢复任务', async () => { const value = await request<Task>(`/tasks/${id}`); commit(value, taskStage(value)) }) }) }
  const newTaskUnchecked = () => { setTask(null); activeTask.current = null; setStage(0); setError(''); setNotice(''); setTitle(''); setText(''); setDraft(null); setInputMode('paste'); setImageProvider(boot?.defaults.image_provider || 'local'); setCloudQuality('medium'); setConfirmed(false); setPlanDirty(false) }
  const newTask = () => { if (busy) { setView('studio'); return }; guardNavigation(() => { newTaskUnchecked(); setView('studio') }) }
  const upload = (file?: File) => { if (unsavedImport && file) guardNavigation(() => uploadUnchecked(file)); else uploadUnchecked(file) }
  const uploadUnchecked = (file?: File) => {
    if (!file || locked) return
    if (file.size > 10 * 1024 * 1024 || !/\.(docx|md|txt)$/i.test(file.name)) { setError('请选择不超过 10 MB 的 DOCX、Markdown 或 TXT 文件。'); return }
    void perform('导入原稿', async () => {
      const form = new FormData(); form.append('file', file); form.append('genre', genre); form.append('style', style); if (title.trim()) form.append('title', title.trim())
      commitNew(await request<Task>('/tasks/import', 'POST', form)); setNotice('原稿已导入并保留，只在副本中插图。')
    })
  }
  const analyze = () => task && guardNavigation(() => void perform('提交全文分析', async () => {
    const result = await request<{ task: Task }>(`/tasks/${task.id}/analyze`, 'POST', { model: textModel })
    commit(result.task, 1)
  }))
  const changeWorkflowMode = (next: WorkflowMode) => {
    if (busy || (next === workflowMode && !(next === 'manual' && automationActive(task)))) return
    if (planDirty) { setError('请先保存当前方案修改，再切换创作方式。'); return }
    if (next === 'manual' && automationActive(task) && task) {
      void perform('停止自动流程，保留已有结果', async () => {
        const value = await request<Task>(`/tasks/${task.id}/cancel`, 'POST', {})
        commit(value, taskStage(value)); setWorkflowMode('manual')
        setNotice('已停止自动后续步骤。已提交的图片会按原请求保存；返回结果后可继续人工处理。')
      })
      return
    }
    setWorkflowMode(next)
    if (next === 'manual' && task) setStage(taskStage(task))
  }
  const startOneClick = () => task && canStartOneClick(task) && !locked && oneClickReady && void perform('提交一键制作任务', async () => {
    const value = await request<Task>(`/tasks/${task.id}/one-click`, 'POST', {
      confirm: true, idempotency_key: oneClickKey(task), model: textModel, image_provider: provider,
      checkpoint, cloud_quality: cloudQuality, format: automaticFormat, max_images: 5,
    })
    commit(value); setNotice('一键任务已保存，将按本次授权自动生成图片并导出文档。')
  })
  const editSlot = (id: string, field: keyof Slot, value: string) => {
    if (!draft) return
    setDraft({ ...draft, slots: draft.slots.map(slot => slot.id === id ? { ...slot, [field]: value } : slot) }); setPlanDirty(true)
  }
  const editDirector = (id: string, config: Director) => {
    if (!draft || locked) return
    setDraft({ ...draft, slots: draft.slots.map(slot => slot.id === id ? { ...slot, director: config } : slot) }); setPlanDirty(true); setConfirmed(false); setInsertConfirmed(false)
  }
  const applyDirector = (config: Director) => {
    if (!draft || locked) return
    setDraft({ ...draft, slots: applyVisualSettings(draft.slots, config) }); setPlanDirty(true); setConfirmed(false); setInsertConfirmed(false)
    setNotice('视觉设置已应用到所有图位；各图物体与位置保留，请保存后再授权。')
  }
  const savePlan = (nextStage = 1) => task && draft && void perform('保存配图方案', async () => {
    commit(await request<Task>(`/tasks/${task.id}/plan`, 'PUT', { version: draft.version, style: draft.style, slots: draft.slots }), nextStage)
    setNotice(draft.slots.length ? '新方案已保存；旧授权和选图已失效，请重新确认。' : '零图方案已保存。可保留正文并导出新文档，无需生图模型或生成授权。')
  })
  const focusSlot = (slot: Slot) => { setSelectedSlot(slot.id); document.getElementById(`paragraph-${slot.paragraph_id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }) }
  const approve = () => task?.plan && confirmed && !unsaved && !locked && imageReady && regionReady && void perform('保存生成授权', async () => {
    commit(await request<Task>(`/tasks/${task.id}/approve`, 'POST', approvalRequest(task.plan!.version, provider, checkpoint, cloudQuality, attempts)), 3)
    setConfirmed(false)
    setNotice('画面设置已确认，可以开始生成。')
  })
  const generate = () => task?.plan && approvalId && !unsaved && !locked && imageReady && regionReady && !scopeChanged && void perform('提交真实生成任务', async () => {
    const scope = `generation:${task.id}:${task.plan!.version}:${approvalId}`
    // Unresolved submissions retain their persisted key. Only confirmed terminal
    // jobs without unresolved intents may start a retry with a new key.
    const key = resolveGenerationKey(task.jobs || [], task.plan!.version, approvalId, keys.current.get(scope))
    keys.current.set(scope, key)
    const result = await request<{ task: Task }>(`/tasks/${task.id}/generate`, 'POST', { version: task.plan!.version, approval_id: approvalId, idempotency_key: key })
    commit(result.task, 4)
  })
  const choose = (asset: Asset) => task && !asset.stale && void perform('保存选图', async () => {
    const useProcessed = !!((processedViews[asset.id] ?? selections[asset.slot_id]?.use_processed) && asset.processed_url)
    const updated: Record<string, Selection> = { ...selections, [asset.slot_id]: { asset_id: asset.id, use_processed: useProcessed } }
    commit(await request<Task>(`/tasks/${task.id}/select`, 'POST', { selections: updated })); setInsertConfirmed(false)
  })
  const removeSelection = (slotId: string) => task && void perform('取消选图', async () => {
    const updated = { ...selections }; delete updated[slotId]
    commit(await request<Task>(`/tasks/${task.id}/select`, 'POST', { selections: updated })); setInsertConfirmed(false)
  })
  const process = (asset: Asset) => task && void perform('降噪与插值放大', async () => {
    commit(await request<Task>(`/tasks/${task.id}/process`, 'POST', { asset_id: asset.id, denoise: true, upscale: 2 })); setProcessedViews(previous => ({ ...previous, [asset.id]: true }))
  })
  const exportFile = (format: 'docx' | 'markdown' | 'html') => task && exportReady && insertConfirmed && void perform(zeroPlan ? '保留原稿并导出新文档' : '插入并导出新文档', async () => {
    const scope = `export:${task.id}:${task.plan?.version}:${format}:${JSON.stringify(selections)}`
    const key = keys.current.get(scope) || crypto.randomUUID(); keys.current.set(scope, key)
    const result = await request<{ task: Task; url: string; filename: string }>(`/tasks/${task.id}/export`, 'POST', { format, confirm_insert: true, idempotency_key: key })
    commit(result.task, 5); setNotice(`已生成 ${result.filename}，可在下方打开或下载。`)
  })
  const header = (eyebrow: string, heading: string, description: string, actions?: ReactNode) => <div className="section-heading"><div><div className="eyebrow">{eyebrow}</div><h2>{heading}</h2><p>{description}</p></div>{actions}</div>
  const sourcePanel = (preview = false) => task && <Card className="source-card"><div className="card-bar"><span><Icon name="book" />{preview ? '插入预览' : '原文对照'}</span><span className="muted">{task.document.character_count.toLocaleString()} 字符</span></div><div className="article-scroll">{task.document.paragraphs.map(paragraph => {
    const paragraphSlots = (preview ? task.plan : draft)?.slots.filter(item => item.paragraph_id === paragraph.id) || []
    const paragraphFigures = figures.filter(figure => figure.paragraph.id === paragraph.id)
    const activeParagraph = paragraphSlots.some(slot => selectedSlot === slot.id)
    return <div key={paragraph.id} id={`paragraph-${paragraph.id}`} className={`article-paragraph ${paragraph.kind === 'heading' ? 'article-heading' : ''} ${activeParagraph ? 'highlight' : ''}`}>
      <span className="paragraph-index">{paragraph.source_index + 1}</span>
      <div onClick={() => paragraphSlots[0] && setSelectedSlot(paragraphSlots[0].id)}>{paragraph.text}</div>
      {!preview && paragraphSlots.map(slot => <button key={slot.id} className="paragraph-tag" onClick={() => focusSlot(slot)}>图位 {draft!.slots.indexOf(slot) + 1}</button>)}
      {preview && paragraphFigures.map(({ slot, asset, selection, caption }) => <figure className="inline-figure" key={slot.id}><img src={selection.use_processed && asset.processed_url ? asset.processed_url : asset.url} alt={slot.caption || '选中插图'} /><figcaption>{caption}</figcaption></figure>)}
    </div>
  })}</div><div className="card-foot">{preview ? '已选图片按正文顺序连续编号；同段按方案图位顺序。最终分页请在 WPS / Word 检查。' : '原稿只读 · 图位与原文段落绑定'}</div></Card>

  const navigationDialog = navigationPending ? <NavigationConfirm onKeep={closeNavigation} onDiscard={discardAndContinue} /> : null
  return <>{view === 'home' && <div inert={navigationPending}><CinematicEntry scene={scene} motionEnabled={motionEnabled} onStart={newTask} onStudio={goStudio} onResume={openHistory} onSettings={openSettings} /></div>}
    <div ref={studioRoot} className="app-shell cinematic-studio" hidden={view === 'home'} style={view === 'home' ? { display: 'none' } : undefined}>
    <aside className="sidebar" inert={historyOpen || navigationPending}><button type="button" className="brand nav-home" onClick={goHome} aria-label="返回图序首页"><span className="studio-wordmark">图序</span><span className="studio-wordmark-latin">TUXU</span></button>
      <button className="new-task" onClick={newTask} disabled={!!busy}><Icon name="plus" />新建配图任务<span>⌘</span></button>
      <div className="mobile-actions"><button className="mobile-new-task" onClick={newTask} disabled={!!busy} aria-label="新建配图任务"><Icon name="plus" size={15} />新建</button><button ref={historyButton} className="mobile-history-button" onClick={() => setHistoryOpen(true)} disabled={!!busy} aria-haspopup="dialog" aria-expanded={historyOpen} aria-controls="mobile-task-history"><Icon name="book" size={15} />历史</button></div>
      <div className="sidebar-label">我的工作记录 <span>{tasks.length.toString().padStart(2, '0')}</span></div>
      <div className="task-list">{tasks.length ? tasks.map(item => <button className={`task-item ${task?.id === item.id ? 'is-active' : ''}`} key={item.id} onClick={() => selectTask(item.id)} disabled={!!busy}><Icon name="file" /><div><strong>{item.title || '未命名文章'}</strong><span>{genres[item.genre] || item.genre} · {date(item.updated_at)}</span></div><i className={item.status === 'failed' ? 'status-dot warning' : 'status-dot'} /></button>) : <div className="no-history">第一篇文章，从这里开始。<br />你的方案与图片会保存在本机。</div>}</div>
      <div className="local-status"><div className="sidebar-label">模型状态 <button className="icon-button" title="刷新服务与模型状态" onClick={() => void perform('刷新模型状态', bootstrap)} disabled={!!busy}><Icon name="refresh" size={14} /></button></div><div><i className={`status-dot ${boot?.models.ollama.available ? '' : 'warning'}`} />文章理解<span>{boot?.models.ollama.available ? '可用' : '未就绪'}</span></div><div><i className={`status-dot ${imageReady ? '' : 'warning'}`} />图片生成<span>{imageReady ? provider === 'codex' ? '自动生图' : provider === 'local' ? '已连接' : '已配置' : '未就绪'}</span></div><p><Icon name="shield" size={14} />本机服务 · 原稿保持完整</p></div>
    </aside>
    <main className="workspace" inert={historyOpen || navigationPending}><header className="topbar"><div><span className="breadcrumb">工作台</span><span className="slash">/</span><span>{task?.title || '新的视觉计划'}</span></div><div className="workspace-tools"><ThemeSwitcher {...appearance} /><button type="button" className="text-button" onClick={goHome}><Icon name="home" size={16} />首页</button><button type="button" className="settings-button" onClick={openSettings}><Icon name="settings" size={18} />设置</button><Pill tone="green">本地工作台</Pill></div></header>
      <div className="workspace-inner"><div className="work-title"><div><span className="eyebrow">WORDS INTO VISUALS</span><h1>{task ? task.title : '好文章，值得一个好画面。'}</h1><p>{task ? `${genres[task.genre]} · ${task.document.format.toUpperCase()} · ${task.plan ? `方案 v${task.plan.version}` : '原稿已保存'}` : '先读懂文字，再决定在哪里、用怎样的图。'}</p></div>{task && <Pill tone={task.status === 'failed' ? 'orange' : 'green'}>{statusNames[task.status] || task.status}</Pill>}</div>
        <WorkflowModeSwitch mode={workflowMode} disabled={!!busy} onChange={changeWorkflowMode} />
        {workflowMode === 'manual' && <nav className="stepper" aria-label="配图制作步骤">{stages.map((label, index) => <button key={label} className={stage === index ? 'current' : completeStages[index] ? 'complete' : ''} aria-current={stage === index ? 'step' : undefined} aria-label={zeroPlan && index >= 2 && index <= 4 ? `${label}，零图方案无需此步骤` : undefined} onClick={() => setStage(index)} disabled={!task && index > 0}><span>{completeStages[index] ? <Icon name="check" size={14} /> : zeroPlan && index >= 2 && index <= 4 ? '—' : String(index + 1).padStart(2, '0')}</span>{label}</button>)}</nav>}
        {error && <div role="alert" className="alert error"><strong>操作尚未完成</strong><span>{error}</span><button className="icon-button" onClick={() => setError('')} aria-label="关闭错误提示"><Icon name="close" /></button></div>}
        {task?.error && !error && <div role="alert" className="alert error"><strong>模型任务未完成</strong><span>{task.error}</span><button className="text-button" onClick={() => { changeWorkflowMode('manual'); setStage(task.plan ? 3 : 0) }}>检查模型与设置</button></div>}
        {workflowMode === 'manual' && unsaved && stage > 1 && <div className="alert warning"><span>当前有未保存的修改。请先保存画面修改，再确认授权和继续制作。</span><button className="text-button" onClick={() => setStage(2)}>返回修改</button></div>}
        {workflowMode === 'manual' && scopeChanged && stage > 1 && <div className="alert warning"><span>生成服务、模型、配置或尝试上限已变化，请重新确认授权。</span><button className="text-button" onClick={() => setStage(3)}>重新确认</button></div>}
        {notice && <div role="status" className="alert success"><Icon name="check" /><span>{notice}</span></div>}
        {(active || busy) && <div className="activity"><span className="spinner" /><div><strong>{busy || (automationActive(task) ? task?.automation?.message : '') || latestJob?.message || '任务正在运行'}</strong>{active && <small>{awaitingAgent ? '等待智能体返回真实图片，状态会自动更新。取消可撤销尚未执行的任务；已提交或结果未知的请求不会重新生成。' : '真实模型任务进行中，状态与结果将自动更新。可以查看原文和已有记录。'}</small>}</div>{active && <button className="text-button" onClick={() => task && void perform('请求取消任务', async () => { commit(await request<Task>(`/tasks/${task.id}/cancel`, 'POST', {})); setNotice('已请求取消；已提交的后端任务会按实际状态记录。') })} disabled={!!busy}>请求取消</button>}</div>}
        {!boot && !busy && <Card className="connection-card"><Icon name="shield" size={28} /><h2>连接本地工作台</h2><p>本地服务暂未连接。启动应用服务后，重新连接即可恢复已有任务。</p><button className="primary" onClick={() => void perform('连接本地服务', bootstrap)}><Icon name="refresh" />重新连接</button></Card>}

        {settingsOpen && <section id="interface-settings" className="card interface-settings" aria-label="界面与动效"><div className="card-bar"><span>界面与动效</span><button className="icon-button" onClick={() => setSettingsOpen(false)} aria-label="关闭界面设置"><Icon name="close" /></button></div><ThemeSwitcher {...appearance} /><p>日间、夜间或跟随系统；首页保持电影式背景与原有文字配色。</p><label className="checkbox-label"><input type="checkbox" checked={motionEnabled} onChange={event => setMotionEnabled(event.target.checked)} />电影式动态背景</label><p>关闭后显示静态背景；也会尊重系统的减少动态效果设置。每次进入首页随机更换画面，浏览过程中保持当前场景。</p></section>}
        <CloudSettings openSignal={settingsSignal} catalog={cloudCatalog} localAgent={boot?.models.local_agent} locked={locked} onChange={catalog => { publishCatalog(catalog) }} onAgentRefresh={bootstrap} />

        {stage === 0 && !task && <div className="import-layout"><Card className="import-card">{header('01 / ARTICLE', '把文章交给图序', '导入原稿或直接粘贴，图片会在新副本中插入。')}<div className="tabs"><button className={inputMode === 'paste' ? 'selected' : ''} onClick={() => setInputMode('paste')}>粘贴文字</button><button className={inputMode === 'file' ? 'selected' : ''} onClick={() => setInputMode('file')}>导入文件</button></div><fieldset disabled={locked}><label>文章标题<input placeholder="给这篇文章起一个标题" value={title} onChange={e => setTitle(e.target.value)} /></label>{inputMode === 'paste' ? <label>文章正文<textarea className="article-input" placeholder="粘贴完整文章，包含标题和段落……" value={text} maxLength={10000} onChange={e => setText(e.target.value)} /><span className="input-meta">{text.length.toLocaleString()} / 10,000 字符<button type="button" className="text-button" onClick={() => guardNavigation(() => { setTitle(sample.title); setText(sample.text); setGenre('academic'); setNotice('已填入自制演示文章，尚未进行 AI 分析或生成。') })}>试用一篇自制样稿 <Icon name="arrow" size={13} /></button></span></label> : <div className={`dropzone ${dragging ? 'dragging' : ''}`} onDragOver={e => { e.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={e => { e.preventDefault(); setDragging(false); upload(e.dataTransfer.files[0]) }}><Icon name="upload" size={32} /><h3>拖入文章，或者点击选择</h3><p>DOCX · Markdown · TXT / 最大 10 MB</p><button className="secondary" onClick={() => fileInput.current?.click()}>选择文件</button><input ref={fileInput} type="file" className="hidden" accept=".docx,.md,.txt" onChange={e => { upload(e.target.files?.[0]); e.target.value = '' }} /></div>}<div className="form-row"><label>内容类型<select value={genre} onChange={e => setGenre(e.target.value as Genre)}>{Object.entries(genres).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><label className="grow">全篇视觉风格<input value={style} onChange={e => setStyle(e.target.value)} placeholder="例如：简洁科学插画，暖白背景" /></label></div>{inputMode === 'paste' && <button className="primary import-submit" disabled={!boot || !text.trim()} onClick={() => void perform('保存文章', async () => commitNew(await request<Task>('/tasks/paste', 'POST', { title: title.trim() || '未命名文章', text, genre, style })))}>保存文章，进入工作台 <Icon name="arrow" /></button>}</fieldset></Card><aside className="import-aside"><div className="editorial-visual"><div className="visual-orbit orbit-one" /><div className="visual-orbit orbit-two" /><span className="visual-paper"><i /><i /><i /><b /><i /><i /></span><span className="visual-corner corner-one" /><span className="visual-corner corner-two" /><span className="visual-note">让画面为文字服务</span></div><span className="eyebrow">A THOUGHTFUL PROCESS</span><h3>每一张图，<br />都有出现的理由。</h3><p>理解全文，找到信息重点。<br />你保留修改、选择和确认的权利。</p><ul><li><Icon name="book" />原文与图位一一对照</li><li><Icon name="shield" />确认方案后才生成</li><li><Icon name="file" />可编辑文档，原稿不覆盖</li></ul><small>科普与新闻的生成画面用于明确标识的示意，不作为实验或现场证据。</small></aside></div>}

        {workflowMode === 'one-click' && task && <OneClickPanel task={task} format={automaticFormat} onFormat={setAutomaticFormat} providerLabel={providerLabel} textModel={textModel} ready={oneClickReady} readiness={oneClickReadiness} busy={!!busy} onStart={startOneClick} onManual={() => changeWorkflowMode('manual')}><label>文章理解模型<input value={textModel} onChange={event => setTextModel(event.target.value)} disabled={locked} /></label><label>图片生成服务<select value={provider} onChange={event => setImageProvider(event.target.value as ImageProvider)} disabled={locked}><option value="local">本地 ComfyUI</option><option value="codex">{boot?.models.local_agent?.label || '本机智能体'}</option><option value="ark">豆包 / 火山方舟</option><option value="openai">GPT 兼容 API</option></select></label>{provider === 'local' && <label>本地生图模型<select value={checkpoint} onChange={event => setCheckpoint(event.target.value)} disabled={locked}><option value="">选择模型</option>{boot?.models.comfy.checkpoints.map(name => <option key={name} value={name}>{name}</option>)}</select></label>}{provider === 'openai' && <label>图片质量<select value={cloudQuality} onChange={event => setCloudQuality(event.target.value as CloudQuality)} disabled={locked}><option value="medium">标准</option><option value="high">高清</option></select></label>}</OneClickPanel>}

        {workflowMode === 'manual' && stage === 0 && task && <>{header('01 / ARTICLE', '原稿已就位', '检查文章与本地模型，开始真实的全文阅读。', <button className="primary" onClick={analyze} disabled={locked || !boot?.models.ollama.available || !textModel}><Icon name="spark" />{task.plan ? '重新分析全文' : '开始全文分析'}</button>)}<div className="document-layout">{sourcePanel()}<Card className="settings-card"><h3>这次配图的设置</h3><div className="detail-row"><span>内容类型</span><strong>{genres[task.genre]}</strong></div><div className="detail-row"><span>原稿字符</span><strong>{task.document.character_count.toLocaleString()}</strong></div><div className="detail-row"><span>已有段落</span><strong>{task.document.paragraphs.length}</strong></div><label>文章理解模型<input value={textModel} onChange={e => setTextModel(e.target.value)} disabled={locked} /></label><label>视觉风格<div className="read-box">{task.style}</div></label><div className={`model-message ${boot?.models.ollama.available ? '' : 'not-ready'}`}><Icon name="shield" /><div>{boot?.models.ollama.available ? '将通过本地 Ollama 阅读文章' : '文章理解模型尚未就绪'}<small>{boot?.models.ollama.error || '点击“开始全文分析”即授权本次向该本地模型发送文章。'}</small></div></div>{task.document.warnings?.length > 0 && <div className="alert warning"><span>{task.document.warnings.join('；')}</span></div>}</Card></div></>}

        {workflowMode === 'manual' && stage === 1 && <>{header('02 / VISUAL PLAN', '先确定，哪些文字需要画面。', '图位建议可以修改、删除或补充。保存改动会使旧授权失效。', draft && <div className="button-group"><button className="secondary" onClick={() => savePlan()} disabled={locked || !planDirty}>{draft.slots.length ? '保存方案' : '保存零图方案'}{planDirty ? ' *' : ''}</button><button className="primary" onClick={() => setStage(draft.slots.length ? 2 : 5)} disabled={locked || unsaved}>{draft.slots.length ? '进入画面导演台' : '保留原稿并导出'} <Icon name="arrow" /></button></div>)}{!draft ? <Card className="empty-state"><Icon name="book" size={32} /><h3>{active ? '正在阅读文章' : '还没有配图方案'}</h3><p>{active ? '等待真实模型完成全文分析，方案会自动出现在这里。' : '在文章与设置中选择本地文本模型，开始全文分析。'}</p><button className="secondary" onClick={() => setStage(0)}>返回文章</button></Card> : <><Card className="plan-summary"><Icon name="spark" /><div><strong>全文视觉提要</strong><p>{draft.summary}</p></div><Pill>{draft.coverage.length} / {task?.document.paragraphs.length} 段已覆盖</Pill></Card><div className="plan-layout">{sourcePanel()}<div className="slot-column"><label className="style-control">统一视觉风格<textarea rows={2} value={draft.style} disabled={locked} onChange={e => { setDraft({ ...draft, style: e.target.value }); setPlanDirty(true) }} /></label>{draft.slots.length === 0 && <Card className="empty-state"><h3>当前方案：保留文字，无需新增插图</h3><p>保存零图方案后，可直接保留正文并导出新文档，无需生图模型与生成授权。也可按需要添加有明确作用的图位。</p><button className="secondary" disabled={locked} onClick={() => planDirty ? savePlan(5) : setStage(5)}>{planDirty ? '保存零图方案并准备导出' : '保留原稿并导出'}</button></Card>}{draft.slots.map((slot, index) => <Card key={slot.id} className={`slot-card ${selectedSlot === slot.id ? 'focused' : ''}`}><div className="slot-top"><button className="slot-title" onClick={() => focusSlot(slot)}><span>{String(index + 1).padStart(2, '0')}</span>图位建议 <Icon name="arrow" size={15} /></button><button className="icon-button" title="删除此图位" disabled={locked} onClick={() => { setDraft({ ...draft, slots: draft.slots.filter(item => item.id !== slot.id) }); setPlanDirty(true) }}><Icon name="trash" size={16} /></button></div><fieldset disabled={locked}><label>对应原文<select value={slot.paragraph_id} onChange={e => editSlot(slot.id, 'paragraph_id', e.target.value)}>{task?.document.paragraphs.filter(insertableParagraph).map(item => <option value={item.id} key={item.id}>{item.text.slice(0, 38)}{item.text.length > 38 ? '…' : ''}</option>)}</select></label><label>为什么需要这张图<textarea rows={2} value={slot.reason} onChange={e => editSlot(slot.id, 'reason', e.target.value)} /></label><label>画面描述<textarea rows={3} value={slot.description} onChange={e => editSlot(slot.id, 'description', e.target.value)} /></label><div className="form-row"><label className="grow">图注<input value={slot.caption} onChange={e => editSlot(slot.id, 'caption', e.target.value)} /></label><label>比例<select value={slot.aspect_ratio} onChange={e => editSlot(slot.id, 'aspect_ratio', e.target.value)}><option value="landscape">横向</option><option value="square">方形</option><option value="portrait">纵向</option></select></label></div><label>事实边界与风险<input value={slot.risk || ''} onChange={e => editSlot(slot.id, 'risk', e.target.value)} /></label></fieldset></Card>)}<button className="add-slot" disabled={locked || draft.slots.length >= 5} onClick={() => { const paragraph = task?.document.paragraphs.find(insertableParagraph); if (!paragraph) return; const slot: Slot = { id: crypto.randomUUID(), paragraph_id: paragraph.id, reason: '', description: '', caption: '', aspect_ratio: 'landscape', risk: '' }; setDraft({ ...draft, slots: [...draft.slots, slot] }); setSelectedSlot(slot.id); setPlanDirty(true) }}><Icon name="plus" />添加一个图位 <span>{draft.slots.length} / 5</span></button></div></div></>}</>}

        {workflowMode === 'manual' && stage === 2 && <>{header('03 / PICTURE DIRECTION', '先布置画面，再让模型生成。', '选一种喜欢的风格和明暗，再把物体拖到希望出现的位置。保存后即可确认生成。', draft && draft.slots.length > 0 && <div className="button-group"><button className="secondary" onClick={() => savePlan(2)} disabled={locked || !planDirty}>保存画面设置{planDirty ? ' *' : ''}</button><button className="primary" onClick={() => setStage(3)} disabled={locked || unsaved}>确认画面，进入授权 <Icon name="arrow" /></button></div>)}{!draft ? <Card className="empty-state"><h3>请先分析文章并确定图位</h3><button className="secondary" onClick={() => setStage(1)}>查看配图方案</button></Card> : draft.slots.length === 0 ? <Card className="empty-state"><Icon name="book" size={32} /><h3>零图方案无需布置画面</h3><p>保存方案后可以保留正文，直接导出原稿副本。</p><button className="primary" onClick={() => planDirty ? savePlan(5) : setStage(5)} disabled={locked}>{planDirty ? '保存零图方案并准备导出' : '保留原稿并导出'} <Icon name="arrow" /></button></Card> : <DirectorPanel slots={draft.slots} selectedSlot={selectedSlot} style={draft.style} locked={locked} regionReady={provider !== 'local' || !!boot?.models.comfy.region_control?.available} regionError={provider === 'local' ? boot?.models.comfy.region_control?.error || undefined : undefined} onSelect={setSelectedSlot} onChange={editDirector} onApply={applyDirector} />}</>}

        {workflowMode === 'manual' && stage === 3 && <>{header('04 / CONFIRM & GENERATE', '确认这些画面，然后开始生成。', '你只需检查画面内容、风格与摆放位置。修改方向请返回画面设置。')}{!task?.plan ? <Card className="empty-state"><h3>请先完成配图方案</h3><button className="secondary" onClick={() => setStage(1)}>查看方案</button></Card> : zeroPlan ? <Card className="empty-state"><Icon name="book" size={32} /><h3>这篇文章无需新增图片</h3><p>可以直接保留正文，导出原稿副本。</p><button className="primary" onClick={() => setStage(5)} disabled={locked || unsaved}>保留原稿并导出 <Icon name="arrow" /></button></Card> : <div className="approval-layout"><Card className="approval-card"><div className="card-bar"><span><Icon name="shield" />本次生成</span><Pill tone={task.approval ? 'green' : 'orange'}>{task.approval ? '已确认' : '待确认'}</Pill></div><fieldset disabled={locked}><div className="scope-grid"><div><span>当前方案</span><strong>v{task.plan.version}</strong></div><div><span>候选图片</span><strong>{task.plan.slots.length}<small> 张</small></strong></div><div><span>生成方式</span><strong className="provider-scope-name">{provider === 'local' ? '本地' : provider === 'codex' ? '本机智能体' : '云端 API'}</strong></div></div><label>选择生图服务<select value={provider} onChange={event => { const next = event.target.value as ImageProvider; taskProviders.current.set(task.id, next); setImageProvider(next); setConfirmed(false) }}><option value="local">本地模型</option>{(['ark', 'openai'] as const).map(id => { const profile = cloudCatalog?.profiles.find(item => item.id === id); return <option key={id} value={id}>{profile?.label || (id === 'ark' ? '豆包 / 火山方舟' : 'GPT 兼容 API')}{profile?.ready ? '' : '（待配置）'}</option> })}<option value="codex">本机智能体{cloudCatalog?.built_in.ready ? '' : '（未就绪）'}</option></select></label>{provider !== 'local' && <div className="cloud-model-note"><strong>{providerLabel}</strong><span>{provider === 'codex' ? '图片工具由所连智能体决定' : chosenModel || '请先填写可用模型名称'}</span><small>{provider === 'codex' ? '已连接并启用的本机智能体会自动执行已批准的生图任务。实际图片工具、算力与费用由所连智能体决定。' : '配置完整只表示可尝试连接，模型权限与效果以实际调用为准。'}</small></div>}{chosenProfile && <ProviderVerification profile={chosenProfile} />}<details className="director-advanced"><summary>生成设置（可选）</summary>{provider === 'local' ? <label>本地生图模型<select value={checkpoint} onChange={event => { setCheckpoint(event.target.value); setConfirmed(false) }}><option value="">请选择已检测到的模型</option>{checkpoint && !boot?.models.comfy.checkpoints.includes(checkpoint) && <option value={checkpoint}>{checkpoint}（当前未检测到）</option>}{boot?.models.comfy.checkpoints.map(item => <option key={item} value={item}>{item}</option>)}</select></label> : provider === 'openai' ? <label>图片质量<select value={cloudQuality} onChange={event => { setCloudQuality(event.target.value as CloudQuality); setConfirmed(false) }}><option value="medium">标准</option><option value="high">高清</option></select></label> : <p className="muted cloud-quality-note">{provider === 'ark' ? '豆包使用 2K 尺寸，具体画质由模型决定。' : '按所选画幅，质量由本机智能体决定。'}</p>}<label>最多尝试几次<input type="number" min={task.plan.slots.length} max={10} value={attempts} onChange={event => { setAttempts(Math.min(10, Math.max(task.plan!.slots.length, Number(event.target.value)))); setConfirmed(false) }} /></label></details><div className="scope-note">每张图先生成一个候选，失败与重试计入尝试上限。{provider === 'local' ? '当前在本机生成。' : provider === 'codex' ? '由已启用的本机智能体自动执行，不自动切换服务或重提未知请求。' : '确认后调用所选云端服务，不自动切换服务或重提未知请求。'}</div><NativeImageConfirmation provider={provider} />{!imageReady && <div className="alert warning"><span>生图服务尚未就绪。{provider === 'local' ? boot?.models.comfy.error || '请先准备可用模型，再刷新状态。' : provider === 'codex' ? boot?.models.local_agent?.message || '请先在本机完成智能体连接，并在生图连接设置中启用。' : '请在生图连接设置中填写地址、模型、密钥，并启用此服务。'}</span></div>}{regionalPlan && !regionReady && <div className="alert warning"><span>物体摆放功能尚未就绪。{boot?.models.comfy.region_control?.error || '请刷新本地模型状态。'}通道恢复后才能生成。</span></div>}<label className="checkbox-label"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} /><span>{provider === 'local' ? '我确认右侧画面内容和本次生成范围，授权在本机生成这些图片。' : `我确认画面和生成范围，授权将当前画面描述与构图发送至${providerLabel}生成图片。文章分析及提示词仍由本地模型完成。`}</span></label><button className="primary full-width" onClick={approve} disabled={!confirmed || !imageReady || !regionReady || planDirty}>{task.approval ? '按当前设置重新确认' : '确认画面设置'}<Icon name="check" /></button>{scopeChanged && <p className="muted">生成设置有变化，请重新确认。</p>}</fieldset><div className="card-foot">调整画面设置后，需要重新确认。</div></Card><div className="intent-column">{task.plan.slots.map((slot, index) => <Card className="intent-card" key={slot.id}><div className="card-bar"><span>画面 {index + 1} · {slot.caption || '未命名画面'}</span><button className="text-button" onClick={() => { setSelectedSlot(slot.id); setStage(2) }} disabled={locked}>修改画面</button></div><div className="intent-body"><div className="intent-style">{summarizeDirector(directorFor(slot))}</div><p>{task.prompt_summaries?.[slot.id]?.description || slot.description.slice(0, 240)}</p>{slot.director?.objects.length ? <ul className="intent-objects">{slot.director.objects.map(object => <li key={object.id}><span>{object.label}</span><small>{objectPosition(object)}</small></li>)}</ul> : <small className="muted">按照画面描述自然构图</small>}</div></Card>)}<button className="primary full-width" onClick={generate} disabled={locked || !approvalId || planDirty || !imageReady || !regionReady || scopeChanged}><Icon name="spark" />{task.status === 'failed' ? '在已确认范围内重试' : provider === 'codex' ? `让本机智能体生成 ${task.plan.slots.length} 张图片` : `开始生成 ${task.plan.slots.length} 张图片`}<Icon name="arrow" /></button>{!approvalId && <p className="inline-warning">请先在左侧确认这些画面。</p>}</div></div>}</>}

        {workflowMode === 'manual' && stage === 4 && <>{header('05 / IMAGE REVIEW', '选一张真正适合文章的图。', '查看真实原图，按需处理。过期方案的图片保留记录，但不能选入当前文档。', <button className="primary" onClick={() => setStage(5)} disabled={!exportReady}>{zeroPlan ? '保留原稿并导出' : `已选 ${selectedCount} 张 · 预览插入`} <Icon name="arrow" /></button>)}{zeroPlan && <Card className="empty-state"><Icon name="book" size={32} /><h3>当前方案无需新增插图</h3><p>可以直接保留正文并导出新文档。历史图片仍保留记录，不会插入零图方案。</p><button className="secondary" onClick={() => setStage(5)} disabled={locked || unsaved}>前往确认与导出</button></Card>}{!zeroPlan && currentAssets.length === 0 && <Card className="empty-state"><Icon name="image" size={36} /><h3>{awaitingAgent ? '正在等待智能体图片' : active ? '真实画面正在生成' : '暂时没有可用候选图'}</h3><p>{awaitingAgent ? '收到并验证智能体返回的真实图片后，这里会自动显示。请查看任务状态；刷新或取消不会重提结果未知的请求。' : active ? '图片完成并取回后会显示在这里，提交队列还不代表已经生成成功。' : '返回授权与生成，检查模型状态并提交任务。'}</p><button className="secondary" onClick={() => setStage(3)}>查看生成设置</button></Card>}<div className="asset-grid">{task?.assets?.map(asset => {
          const slot = task.plan?.slots.find(item => item.id === asset.slot_id)
          const processed = !!((processedViews[asset.id] ?? selections[asset.slot_id]?.use_processed) && asset.processed_url)
          const selected = selections[asset.slot_id]?.asset_id === asset.id
          return <Card className={`asset-card ${selected ? 'asset-selected' : ''} ${asset.stale ? 'asset-stale' : ''}`} key={asset.id}><div className="asset-preview"><img src={processed ? asset.processed_url! : asset.url} alt={slot?.caption || '生成候选图'} onError={e => { e.currentTarget.classList.add('image-unavailable') }} /><Pill tone={asset.stale ? 'orange' : ''}>{asset.stale ? '旧版本 · 不可选用' : processed ? '处理后' : '原始生成'}</Pill></div><div className="asset-body"><div className="asset-title"><h3>{slot?.caption || '历史候选图'}</h3>{selected && <Pill tone="green"><Icon name="check" size={12} />已选{selections[asset.slot_id]?.use_processed ? '处理图' : '原图'}</Pill>}</div><div className="image-switch"><button className={!processed ? 'selected' : ''} onClick={() => setProcessedViews({ ...processedViews, [asset.id]: false })}>原图 {asset.width} × {asset.height}</button><button className={processed ? 'selected' : ''} disabled={!asset.processed_url} onClick={() => setProcessedViews({ ...processedViews, [asset.id]: true })}>处理图{asset.processed_width ? ` ${asset.processed_width} × ${asset.processed_height}` : ''}</button></div><p className="asset-method">{processed ? '轻度中值降噪 + Lanczos 传统插值放大，非 AI 超分。' : '保留真实原始尺寸；处理后可以随时切回原图。'}</p><NativeImageSizeNotice notice={asset.native_size_notice} /><div className="asset-actions"><button className="secondary" onClick={() => process(asset)} disabled={locked || asset.stale || !!asset.processed_url}>降噪与放大</button><button className="primary" onClick={() => choose(asset)} disabled={locked || asset.stale || !approvalId || unsaved || scopeChanged}>{selected ? `更新为${processed ? '处理图' : '原图'}` : '选用这张图'}</button>{selected && <button className="icon-button" title="取消选用" disabled={locked} onClick={() => removeSelection(asset.slot_id)}><Icon name="close" /></button>}</div><details><summary>图片信息</summary><dl><dt>生成时间</dt><dd>{new Date(asset.created_at).toLocaleString('zh-CN')}</dd>{asset.director && <><dt>画面设置</dt><dd>{summarizeDirector(asset.director)}</dd>{asset.director.objects.map(object => <Fragment key={object.id}><dt>{object.label}</dt><dd>{objectPosition(object)}</dd></Fragment>)}</>}</dl></details></div></Card>
        })}</div></>}

        {workflowMode === 'manual' && stage === 5 && <>{header('06 / DOCUMENT DELIVERY', zeroPlan ? '保留文章，导出一个新版本。' : '把画面放回文章。', zeroPlan ? '零图方案已保存。确认保持正文，导出原稿副本，无需生成授权。' : '确认位置、图注与编号，导出可继续编辑的新版本。')}<div className="document-layout">{sourcePanel(true)}<div className="delivery-column"><Card className="export-card"><h3>插入与文件交付</h3><div className="export-number"><strong>{selectedCount.toString().padStart(2, '0')}</strong><span>{zeroPlan ? '无新增插图 · 保留正文' : '张图片待插入'}<br /><small>原稿始终不覆盖</small></span></div>{!zeroPlan && !selectedCount && <p className="inline-warning">请先选用至少一张当前版本的图片。</p>}<label className="checkbox-label"><input type="checkbox" checked={insertConfirmed} disabled={!exportReady} onChange={e => setInsertConfirmed(e.target.checked)} /><span>{zeroPlan ? '我确认已保存的零图方案，保留正文并导出无新增插图的原稿副本。' : '我确认选中图片、图注和位置，将其插入原稿副本并导出。'}</span></label><div className="export-formats">{(['docx', 'markdown', 'html'] as const).map(format => <button key={format} className="export-option" onClick={() => exportFile(format)} disabled={!exportReady || !insertConfirmed}><Icon name={format === 'html' ? 'image' : 'file'} /><span><strong>{format === 'docx' ? 'Word 文档' : format === 'markdown' ? 'Markdown 图片包' : 'HTML 预览'}</strong><small>{format === 'docx' ? 'DOCX · 可编辑、可在 WPS 检查' : format === 'markdown' ? 'ZIP · 文章与相对路径图片' : 'HTML · 查看插入效果'}</small></span><Icon name="download" /></button>)}</div><div className="scope-note">网页预览与文档分页可能不同。导出后检查正文、图位、编号与分页，再用于正式交付。</div></Card><Card className="history-card"><h3>已保存的导出记录</h3>{task?.exports?.length ? [...task.exports].reverse().map((item, index) => <a className="export-history" key={`${item.filename}-${index}`} href={item.url || `/api/exports/${task.id}/${encodeURIComponent(item.filename)}`} target="_blank" rel="noreferrer"><Icon name="file" /><span><strong>{item.filename}</strong><small>{item.created_at ? new Date(item.created_at).toLocaleString('zh-CN') : item.format.toUpperCase()}</small></span><Icon name="download" size={15} /></a>) : <p className="muted">完成导出后，文件会保存在本地并显示在这里。</p>}</Card></div></div></>}
        <footer className="workspace-footer"><span>图序社区版 v0.5.0</span><span>开源基础模板 · 生成画面与文档请人工检查</span></footer>
      </div>
    </main>
    {historyOpen && <div className="mobile-history-overlay" onClick={() => setHistoryOpen(false)}><div id="mobile-task-history" ref={historyDialog} className="mobile-history-panel" role="dialog" aria-modal="true" aria-label="历史任务" onClick={event => event.stopPropagation()} onKeyDown={event => {
      if (event.key === 'Escape') { event.preventDefault(); setHistoryOpen(false); return }
      if (event.key !== 'Tab') return
      const buttons = historyDialog.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')
      if (!buttons?.length) return
      const first = buttons[0], last = buttons[buttons.length - 1]
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
    }}><div className="history-drawer-heading"><div><span className="eyebrow">保存在这台电脑</span><h2>历史任务 <small>{tasks.length}</small></h2></div><button className="icon-button" onClick={() => setHistoryOpen(false)} aria-label="关闭历史任务"><Icon name="close" /></button></div><div className="history-drawer-list">{tasks.length ? tasks.map(item => <button className={`task-item ${task?.id === item.id ? 'is-active' : ''}`} key={item.id} onClick={() => { selectTask(item.id); setHistoryOpen(false) }} disabled={!!busy}><Icon name="file" /><div><strong>{item.title || '未命名文章'}</strong><span>{genres[item.genre] || item.genre} · {date(item.updated_at)}</span></div><Icon name="arrow" size={14} /></button>) : <p className="no-history">还没有历史任务。保存第一篇文章后，就可以从这里继续。</p>}</div></div></div>}
  </div>{navigationDialog}</>
}
