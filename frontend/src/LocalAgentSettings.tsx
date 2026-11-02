import { useEffect, useRef, useState } from 'react'
import { request } from './api'
import type { LocalAgentStatus } from './types'

export default function LocalAgentSettings({ status, locked, onRefresh }: { status?: LocalAgentStatus; locked: boolean; onRefresh: () => Promise<void> }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const toggle = async (enabled: boolean) => {
    if (locked || busy || !status || enabled && !status.available) return
    setBusy(true); setError('')
    try {
      await request<LocalAgentStatus>('/local-agent', 'POST', { enabled })
      await onRefresh()
    } catch {
      // A lost response is uncertain: refresh status, never repeat the mutation.
      try { await onRefresh() } catch { /* preserve the unknown state message */ }
      if (alive.current) setError('开关状态未确认，请刷新模型状态后检查。')
    } finally { if (alive.current) setBusy(false) }
  }
  return <section className="local-agent-settings cloud-profile" aria-label="本机智能体设置">
    <div className="cloud-profile-heading"><strong>{status?.label || '本机智能体'}</strong><span>{!status?.available ? '未连接' : status.enabled ? '已启用' : '已连接 · 未启用'}</span></div>
    <p className="local-agent-message">{status?.message || '尚未连接可自动生图的本机智能体，请先按本机配置说明完成连接。'}</p>
    <p className="cloud-settings-note">支持 Codex、通用命令行及本机 HTTP 接入。其他智能体需具备生图能力，并按本机适配器说明完成一次配置。</p>
    <label className="checkbox-label"><input type="checkbox" checked={!!status?.enabled} disabled={locked || busy || !status || !status.available && !status.enabled} onChange={event => void toggle(event.target.checked)} /><span>{busy ? '正在保存智能体状态……' : '启用已连接的本机智能体自动生图'}</span></label>
    <p className="cloud-settings-note">文章分析及提示词由本地模型完成。生图可走已连接的本机智能体，图像实际算力由智能体提供方决定，可能联网或产生费用；不代表离线免费。确认画面并开始生成后，才会提交本次图片任务。</p>
    {error && <p role="alert" className="inline-warning">{error}</p>}
  </section>
}
