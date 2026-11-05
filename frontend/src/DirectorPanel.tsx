import { systemText, t } from './i18n'
import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent, PointerEvent } from 'react'
import type { Director, DirectorObject, Slot } from './types'
import { alignObject, brightnessOptions, cameraOptions, depthLabels, depthOrder, directorFor, directorSummary, fitObject, lightingOptions, moveObject, objectPosition, styleChoiceLabel, styleOptions, styleRecommendationFor, updateDirector } from './director'

interface Props {
  slots: Slot[]
  selectedSlot: string
  style: string
  locked: boolean
  regionReady: boolean
  onSelect: (id: string) => void
  onChange: (id: string, config: Director) => void
  onApply: (config: Director) => void
}
type Drag = { object: DirectorObject; mode: 'move' | 'resize'; pointerId: number; clientX: number; clientY: number; canvasWidth: number; canvasHeight: number }

export default function DirectorPanel({ slots, selectedSlot, style, locked, regionReady, onSelect, onChange, onApply }: Props) {
  const slot = slots.find(item => item.id === selectedSlot) || slots[0]
  const config = directorFor(slot)
  const recommendation = styleRecommendationFor(slot)
  const [selectedObject, setSelectedObject] = useState('')
  const [showGrid, setShowGrid] = useState(true)
  const canvas = useRef<HTMLDivElement>(null)
  const drag = useRef<Drag | null>(null)
  const object = config.objects.find(item => item.id === selectedObject) || config.objects[0]
  useEffect(() => { setSelectedObject(''); drag.current = null }, [slot.id])
  useEffect(() => { if (selectedObject && !config.objects.some(item => item.id === selectedObject)) setSelectedObject('') }, [selectedObject, config.objects.map(item => item.id).join('|')])
  const edit = (patch: Partial<Director>) => { if (!locked) onChange(slot.id, updateDirector(config, patch)) }
  const editObject = (id: string, patch: Partial<DirectorObject>) => edit({ objects: config.objects.map(item => item.id === id ? fitObject(item, patch) : item) })
  const addObject = () => {
    const id = crypto.randomUUID()
    const next: DirectorObject = { id, label: t("物体 {0}", config.objects.length + 1), description: '', x: 20, y: 20, width: 30, height: 30, depth: 'midground' }
    edit({ objects: [...config.objects, next] }); setSelectedObject(id)
  }
  const startDrag = (event: PointerEvent<HTMLElement>, target: DirectorObject, mode: Drag['mode']) => {
    if (locked || event.button !== 0 || !canvas.current) return
    event.preventDefault(); event.stopPropagation(); setSelectedObject(target.id)
    const rect = canvas.current.getBoundingClientRect()
    if (!rect.width || !rect.height) return
    drag.current = { object: target, mode, pointerId: event.pointerId, clientX: event.clientX, clientY: event.clientY, canvasWidth: rect.width, canvasHeight: rect.height }
    event.currentTarget.setPointerCapture(event.pointerId)
  }
  const moveDrag = (event: PointerEvent<HTMLElement>) => {
    const current = drag.current
    if (!current || locked || current.pointerId !== event.pointerId) return
    const dx = (event.clientX - current.clientX) / current.canvasWidth * 100
    const dy = (event.clientY - current.clientY) / current.canvasHeight * 100
    editObject(current.object.id, current.mode === 'move' ? { x: current.object.x + dx, y: current.object.y + dy } : { width: Math.min(100 - current.object.x, current.object.width + dx), height: Math.min(100 - current.object.y, current.object.height + dy) })
  }
  const endDrag = (event: PointerEvent<HTMLElement>) => {
    if (drag.current?.pointerId === event.pointerId) drag.current = null
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
  }
  const keyboardMove = (event: KeyboardEvent<HTMLElement>, target: DirectorObject) => {
    if (locked || !['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return
    event.preventDefault(); event.stopPropagation(); setSelectedObject(target.id)
    const amount = event.shiftKey ? 5 : 1
    const next = moveObject(target, event.key === 'ArrowLeft' ? -amount : event.key === 'ArrowRight' ? amount : 0, event.key === 'ArrowUp' ? -amount : event.key === 'ArrowDown' ? amount : 0)
    editObject(target.id, next)
  }
  const dragProps = { onPointerMove: moveDrag, onPointerUp: endDrag, onPointerCancel: endDrag }

  return <div className="director-workbench simple-director">
    <div className="director-slot-tabs" aria-label={t("选择要调整的画面")}>{slots.map((item, index) => <button key={item.id} className={item.id === slot.id ? 'selected' : ''} onClick={() => onSelect(item.id)} disabled={locked}><span>{t("画面")}{' '}{index + 1}</span><strong>{item.caption || t("未命名画面")}</strong><small>{directorSummary(item)}</small></button>)}</div>
    <div className="director-layout">
      <section className="card director-controls"><div className="card-bar"><span>{t("选一种画面风格")}</span></div><fieldset disabled={locked}>
        <label className="compact-style-selector">{t("画面风格")}<select value={config.style_id} onChange={event => edit({ style_id: event.target.value as Director['style_id'] })}>{styleOptions.map(item => <option key={item.id} value={item.id}>{styleChoiceLabel(item.id, recommendation)}</option>)}</select></label><div className="style-presets" aria-label={t("画面风格")}>{styleOptions.map(item => <button type="button" key={item.id} className={config.style_id === item.id ? 'selected' : ''} aria-pressed={config.style_id === item.id} title={t(item.detail)} onClick={() => edit({ style_id: item.id })}><strong>{styleChoiceLabel(item.id, recommendation)}</strong></button>)}</div>
        {recommendation && <p className="style-recommendation">{systemText(recommendation.reason)}<small>{t("可自由切换画风，保存后以你的选择为准。")}</small></p>}
        {config.style_id === 'inherit' && <div className="director-inherited">{t("文章风格：")}{style}</div>}
        <label className="director-label">{t("画面想要多亮？")}</label><div className="director-segments" aria-label={t("画面明暗度")}>{brightnessOptions.map(item => <button type="button" key={item.id} className={config.brightness === item.id ? 'selected' : ''} aria-pressed={config.brightness === item.id} title={t(item.detail)} onClick={() => edit({ brightness: item.id })}>{t(item.label)}</button>)}</div>
        {config.objects.length > 0 && <label className="director-background">{t("背景（可选）")}<input maxLength={300} value={config.background ?? ''} placeholder={t("例如：白色桌面、林间空地")} onChange={event => edit({ background: event.target.value })} /><span className="director-help">{t("只描述背景；留空时根据文章自动安排")}</span></label>}
        <details className="director-advanced"><summary>{t("更多画面设置（可选）")}</summary><label>{t("还有什么想法？（可选）")}<textarea rows={2} maxLength={600} value={config.notes} placeholder={t("例如：背景干净一些，画面里不要出现文字。")} onChange={event => edit({ notes: event.target.value })} /></label><label>{t("光线感觉")}<select value={config.lighting} onChange={event => edit({ lighting: event.target.value as Director['lighting'] })}>{lightingOptions.map(item => <option key={item.id} value={item.id}>{t(item.label)}</option>)}</select></label><label>{t("看画面的角度")}<select value={config.camera} onChange={event => edit({ camera: event.target.value as Director['camera'] })}>{cameraOptions.map(item => <option key={item.id} value={item.id}>{t(item.label)}</option>)}</select></label></details>
        {slots.length > 1 && <><button className="secondary full-width" onClick={() => onApply(config)}>{t("让所有画面用这个风格")}</button><p className="director-help">{t("同时应用明暗等设置，每张图里的物体、位置和背景保留。")}</p></>}
      </fieldset></section>
      <section className="card director-scene"><div className="card-bar"><span>{t("把物体放到合适的位置")}</span><span className="pill">{slot.aspect_ratio === 'portrait' ? t("竖图") : slot.aspect_ratio === 'square' ? t("方图") : t("横图")}</span></div>
        <div className="scene-brief"><strong>{slot.caption || t("当前画面")}</strong><p>{slot.description.slice(0, 240)}</p></div>
        <div className="canvas-toolbar"><span>{config.objects.length ? t("已放置 {0} 个物体", config.objects.length) : t("可以自由摆放物体")}</span><label className="checkbox-label"><input type="checkbox" checked={showGrid} onChange={event => setShowGrid(event.target.checked)} />{t("辅助线")}</label></div>
        <div className="canvas-surround"><div ref={canvas} className={`director-canvas ${slot.aspect_ratio} mood-${config.brightness} light-${config.lighting} ${showGrid ? 'with-grid' : ''}`} role="group" aria-label={t("物体摆放画面")}>
          {config.objects.length === 0 && <div className="canvas-empty"><span className="canvas-empty-mark" aria-hidden="true">＋</span><strong>{t("想把什么放在画面里？")}</strong><small>{t("点击下方“添加物体”，再拖到合适的位置。")}</small></div>}
          {config.objects.map(item => <div key={item.id} role="button" tabIndex={locked ? -1 : 0} aria-disabled={locked} aria-label={t("画面物体 {0}，{1}", item.label, objectPosition(item))} className={`scene-object depth-${item.depth} ${item.id === object?.id ? 'selected' : ''}`} style={{ left: `${item.x}%`, top: `${item.y}%`, width: `${item.width}%`, height: `${item.height}%`, zIndex: depthOrder[item.depth] }} onClick={() => setSelectedObject(item.id)} onFocus={() => setSelectedObject(item.id)} onPointerDown={event => startDrag(event, item, 'move')} onKeyDown={event => keyboardMove(event, item)} {...dragProps}><strong>{item.label || t("未命名物体")}</strong><button className="resize-handle" type="button" disabled={locked} tabIndex={-1} aria-label={t("调整 {0} 大小", item.label)} onPointerDown={event => startDrag(event, item, 'resize')} {...dragProps} /></div>)}
        </div></div>
        <div className="canvas-guidance"><p>{t("拖动方框调整位置，右下角可以改变大小。方框帮助模型理解你的想法，实际图片仍需检查。")}</p></div>
        {!regionReady && config.objects.length > 0 && <div className="alert warning region-warning"><span>{t("物体摆放功能尚未就绪。请刷新本地模型状态，恢复后再生成。")}</span></div>}
        <div className="object-section"><div className="object-heading"><h3>{t("画面里的物体")}</h3><button className="secondary" onClick={addObject} disabled={locked || config.objects.length >= 4}>{t("＋ 添加物体")}</button></div>
          {config.objects.length > 0 && <div className="object-tabs" aria-label={t("选择要调整的物体")}>{config.objects.map(item => <button key={item.id} className={item.id === object?.id ? 'selected' : ''} onClick={() => setSelectedObject(item.id)}>{item.label || t("未命名物体")}</button>)}</div>}
          {object ? <fieldset disabled={locked} className="object-editor"><div className="object-name-row"><label>{t("这是什么？")}<input value={object.label} maxLength={80} placeholder={t("例如：一张木桌、一棵树")} onChange={event => editObject(object.id, { label: event.target.value })} /></label><button type="button" className="text-button object-remove" onClick={() => edit({ objects: config.objects.filter(item => item.id !== object.id) })}>{t("移除")}</button></div>
            <div className="placement-controls"><div><span>{t("左右")}</span>{([{ id: 'start', label: t("靠左") }, { id: 'center', label: t("居中") }, { id: 'end', label: t("靠右") }] as const).map(item => <button key={item.id} type="button" className="secondary" aria-label={t("水平{0}", item.label)} onClick={() => editObject(object.id, alignObject(object, 'horizontal', item.id))}>{t(item.label)}</button>)}</div><div><span>{t("上下")}</span>{([{ id: 'start', label: t("靠上") }, { id: 'center', label: t("居中") }, { id: 'end', label: t("靠下") }] as const).map(item => <button key={item.id} type="button" className="secondary" aria-label={t("垂直{0}", item.label)} onClick={() => editObject(object.id, alignObject(object, 'vertical', item.id))}>{t(item.label)}</button>)}</div></div>
            <label>{t("简单说说它的样子（可选）")}<textarea rows={2} maxLength={300} value={object.description} placeholder={t("填写文章已经提到或你确认的外观、动作。")} onChange={event => editObject(object.id, { description: event.target.value })} /></label>
            <details className="director-advanced"><summary>{t("微调大小和层次（可选）")}</summary><label>{t("放在画面的哪一层？")}<select value={object.depth} onChange={event => editObject(object.id, { depth: event.target.value as DirectorObject['depth'] })}>{Object.entries(depthLabels).map(([id, label]) => <option key={id} value={id}>{t(label)}</option>)}</select></label><div className="object-geometry">{([{ field: 'x', label: t("左边距") }, { field: 'y', label: t("上边距") }, { field: 'width', label: t("宽度") }, { field: 'height', label: t("高度") }] as const).map(({ field, label }) => <label key={field}>{label} (%)<input type="number" min={field === 'width' || field === 'height' ? 8 : 0} max={field === 'x' ? 100 - object.width : field === 'y' ? 100 - object.height : 100} step={0.1} value={object[field]} onChange={event => { if (event.target.value !== '') editObject(object.id, { [field]: event.target.valueAsNumber }) }} /></label>)}</div></details>
          </fieldset> : <p className="director-help">{t("不需要指定物体位置，也可以直接按画面描述生成。")}</p>}
        </div>
      </section>
    </div>
  </div>
}
