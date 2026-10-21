import type { Task } from './types'

export const stages = ['文章与设置', '配图方案', '画面导演台', '授权与生成', '选图与处理', '插入与导出']

/** Historical tasks resume at their saved result; the new director step adds no data migration. */
export function taskStage(task: Task): number {
  return task.status === 'awaiting_agent' || task.jobs?.some(job => job.status === 'awaiting_agent') ? 4 : task.exports?.length ? 5 : task.assets?.some(asset => !asset.stale) ? 4 : task.approval ? 3 : task.plan ? 1 : 0
}
