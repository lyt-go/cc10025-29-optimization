/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type CostBatchItem = {
  item: string
  label: string
  source_module: string
  job_id: number
  job_no: string
  baseline_usage: string
  current_usage: string
  baseline_amount: number
  current_amount: number
  variance: number
  variance_text: string
  revision: number
  measurement_kind: string
  usage_basis: string
  usage_confirmed_at: string
  sampling_point: string
  applied_amount: number
  pending_delta: number
  accounted: number
}

export type CostBatchAsset = {
  asset_id: string
  entry_id: number
  record_id: string
  baseline_total: number
  current_total: number
  variance_total: number
  included_amount: number
  carried_amount: number
  applied_total: number
  decision: string
  decision_reason: string
  effective_decision?: string
  confirmed_at: string
  items: CostBatchItem[]
}

export type CostBatchTotals = {
  baseline: number
  current: number
  variance: number
  included: number
  carried: number
  batch_total: number
  asset_count: number
  ledger_count: number
  baseline_text: string
  current_text: string
  variance_text: string
  included_text: string
  carried_text: string
  batch_total_text: string
}

export type CostBatch = {
  batch_id: string
  label: string
  status: string
  opened_at: string
  sampling_point: string
  read_count: number
  read_stats: Record<string, number>
  variance_policy: string
  assets: CostBatchAsset[]
  ledger: Record<string, string | number>[]
  recalc_count: number
  recalculated_at: string
  totals: CostBatchTotals
  confirmation_times: {
    asset_id: string
    confirmed_at: string
    current_revision: number
    amount: number
  }[]
}

export type BatchActionResult = ActionResult & { entry?: CostBatch }

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}
