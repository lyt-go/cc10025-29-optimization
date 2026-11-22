import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  BatchActionResult,
  CostBatch,
  EntryRow,
  OverviewResult,
  PageResult,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
  values: Record<string, unknown> = {},
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action, values }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---------- 批量核算底稿 ----------
export async function openCostBatch(
  entryIds: number[],
  label = '',
  variancePolicy = '本次入账',
): Promise<BatchActionResult> {
  const response = await request('/api/cost_tracker/batches', {
    method: 'POST',
    body: JSON.stringify({ entry_ids: entryIds, label, variance_policy: variancePolicy }),
  })
  return await response.json()
}

export async function listCostBatches(): Promise<CostBatch[]> {
  const response = await request('/api/cost_tracker/batches')
  if (!response.ok) throw new Error('批量核算底稿读取失败')
  const payload = (await response.json()) as { items: CostBatch[] }
  return payload.items
}

export async function getCostBatch(batchId: string): Promise<CostBatch> {
  const response = await request(`/api/cost_tracker/batches/${batchId}`)
  if (!response.ok) throw new Error('批量核算底稿读取失败')
  return await response.json()
}

export async function recalculateCostBatch(batchId: string): Promise<BatchActionResult> {
  const response = await request(`/api/cost_tracker/batches/${batchId}/recalculate`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
  return await response.json()
}

export async function confirmCostBatch(
  batchId: string,
  entryIds: number[],
): Promise<BatchActionResult> {
  const response = await request(`/api/cost_tracker/batches/${batchId}/confirm`, {
    method: 'POST',
    body: JSON.stringify({ entry_ids: entryIds }),
  })
  return await response.json()
}
