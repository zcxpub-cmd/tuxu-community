let token = ''
export const setToken = (value: string) => { token = value }
export async function request<T>(path: string, method = 'GET', data?: unknown): Promise<T> {
  const headers: Record<string, string> = {}
  if (method !== 'GET') headers['X-Local-Token'] = token
  const form = data instanceof FormData
  if (data && !form) headers['Content-Type'] = 'application/json'
  const response = await fetch(`/api${path}`, { method, headers, body: data ? (form ? data : JSON.stringify(data)) as BodyInit : undefined })
  const type = response.headers.get('content-type') || ''
  const result = type.includes('json') ? await response.json() : await response.text()
  if (!response.ok) throw new Error(typeof result === 'object' ? result.detail || `请求失败（${response.status}）` : `本地服务返回 ${response.status}，请检查服务状态。`)
  return result as T
}
