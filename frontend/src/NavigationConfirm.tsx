import { useEffect, useRef } from 'react'

export default function NavigationConfirm({ onKeep, onDiscard }: { onKeep: () => void; onDiscard: () => void }) {
  const panel = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const oldOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    panel.current?.querySelector<HTMLButtonElement>('button')?.focus()
    return () => {
      document.body.style.overflow = oldOverflow
      if (previous?.isConnected) previous.focus()
      else document.querySelector<HTMLButtonElement>('.workspace-tools button, .entry-links button')?.focus()
    }
  }, [])
  return <div className="navigation-confirm-overlay"><div ref={panel} role="dialog" aria-modal="true" aria-labelledby="navigation-confirm-title" aria-describedby="navigation-confirm-description" className="navigation-confirm-panel" onKeyDown={event => {
    if (event.key === 'Escape') { event.preventDefault(); onKeep() }
    if (event.key !== 'Tab') return
    const buttons = panel.current?.querySelectorAll<HTMLButtonElement>('button')
    if (!buttons?.length) return
    const first = buttons[0], last = buttons[buttons.length - 1]
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
  }}><span className="eyebrow">保留你的创作</span><h2 id="navigation-confirm-title">还有未保存的内容</h2><p id="navigation-confirm-description">可以继续编辑并保存，也可以放弃这些修改后离开。</p><div className="navigation-confirm-actions"><button className="primary" onClick={onKeep}>继续编辑</button><button className="text-button" onClick={onDiscard}>放弃修改并离开</button></div></div></div>
}
