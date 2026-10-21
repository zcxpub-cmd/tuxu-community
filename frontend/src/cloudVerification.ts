import type { CloudProfile, CloudTestResult } from './types'

export interface CloudDraft { label: string; base_url: string; model: string; enabled: boolean; key: string; clear_key: boolean }

export function cloudDraftChanged(profile: CloudProfile, draft: CloudDraft): boolean {
  return draft.label !== profile.label || draft.base_url !== profile.base_url || draft.model !== profile.model || draft.enabled !== profile.enabled || draft.key.length > 0 || draft.clear_key
}

/** IDs only: no arbitrary provider names, descriptions or payload fields. */
export function safeModelIds(kind: CloudProfile['kind'], models: CloudTestResult['models']): string[] {
  const pattern = kind === 'ark' ? /^doubao-seedream-[a-z0-9-]+$/ : /^(?:gpt-image-[a-z0-9.-]+|dall-e-2|dall-e-3)$/
  return [...new Set((Array.isArray(models) ? models : []).map(item => item?.id).filter((id): id is string => typeof id === 'string' && id.length <= 160 && !/\s/.test(id) && pattern.test(id)))].sort().slice(0, 64)
}

export function canAdoptModelTest(startRevision: string, currentRevision: string, result: CloudTestResult, sequence: number, currentSequence: number): boolean {
  return result.ok === true && typeof result.revision === 'string' && result.revision === startRevision && result.revision === currentRevision && sequence === currentSequence
}

export function verificationLabels(profile: CloudProfile) {
  const verification = profile.verification
  return {
    configuration: profile.ready ? '配置完整' : '配置未完整',
    connection: verification?.connection === 'ok' ? '连接查询通过' : verification?.connection === 'failed' ? '连接查询未通过' : '连接尚未测试',
    model: verification?.model_listed === true ? '所填模型在列表中' : verification?.model_listed === false ? '列表未找到所填模型' : '模型列表尚未验证',
    generation: verification?.generation === 'succeeded' ? '最近实际生图成功' : verification?.generation === 'rejected' ? '最近生图被服务拒绝' : verification?.generation === 'unknown' ? '最近生图结果未确定' : '尚无实际生图验证',
  }
}
