<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
        <button
          v-if="isCostTracker"
          class="btn primary"
          data-testid="open-batch"
          type="button"
          :disabled="!selectedIds.length"
          @click="openBatch"
        >
          批量核算选中（{{ selectedIds.length }}）
        </button>
      </div>
    </header>

    <div v-if="isCostTracker" class="filter-bar batch-bar">
      <label class="filter-item">
        <span>重新打开底稿</span>
        <select v-model="activeBatchId" data-testid="batch-select" @change="reopenBatch">
          <option value="">选择已生成的底稿</option>
          <option v-for="item in batches" :key="item.batch_id" :value="item.batch_id">
            {{ item.batch_id }} · {{ item.totals.current_text }}
          </option>
        </select>
      </label>
      <span class="hint">勾选多条成本记录后开批，三项成本按同一采样位置一次读取。</span>
    </div>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-if="isCostTracker" class="select-col"></th>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-if="isCostTracker" class="select-col">
            <input
              v-model="selectedIds"
              type="checkbox"
              :value="Number(row.id)"
              :data-testid="`select-${row.id}`"
            />
          </td>
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + (isCostTracker ? 3 : 2)" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <p v-if="batchMessage" class="batch-message" data-testid="batch-message">{{ batchMessage }}</p>

    <CostBatchSheet
      v-if="isCostTracker && activeBatch"
      :batch="activeBatch"
      @recalculate="recalculateBatch"
      @confirm="confirmBatch"
    />

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import {
  confirmCostBatch,
  createEntry,
  downloadEntries,
  getCostBatch,
  listCostBatches,
  listEntries,
  moduleMeta,
  openCostBatch,
  recalculateCostBatch,
  runAction as applyAction,
} from '@/api/backend-service'
import CostBatchSheet from '@/components/CostBatchSheet.vue'
import type { CostBatch, EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const isCostTracker = computed(() => props.moduleKey === 'cost_tracker')
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const selectedIds = ref<number[]>([])
const batches = ref<CostBatch[]>([])
const activeBatch = ref<CostBatch | null>(null)
const activeBatchId = ref('')
const batchMessage = ref('')
const batchBusy = ref(false)
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    // 补数完工 / 修订用量需要实测用量，现场录入后随动作提交。
    const values: Record<string, string> = {}
    if (meta.key === 'backfill_job' && (action === '确认完成' || action === '修订用量')) {
      const current = String(row['实测用量'] || row['数据量'] || '')
      const usage = window.prompt(`${action}：请输入本次实测用量（如 40GB）`, current)
      if (usage === null) return
      values['实测用量'] = usage.trim()
    }
    const result = await applyAction(meta.key, Number(row.id), action, values)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

// ---------- 批量核算底稿 ----------
async function refreshBatches() {
  if (!isCostTracker.value) return
  try {
    batches.value = await listCostBatches()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '底稿列表读取失败'
  }
}

async function openBatch() {
  if (!selectedIds.value.length || batchBusy.value) return
  batchBusy.value = true
  batchMessage.value = ''
  try {
    const result = await openCostBatch(selectedIds.value, '批量核算底稿')
    if (!result.ok || !result.entry) throw new Error(result.message)
    activeBatch.value = result.entry
    activeBatchId.value = result.entry.batch_id
    batchMessage.value = result.message
    await refreshBatches()
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '开批失败'
  } finally {
    batchBusy.value = false
  }
}

async function reopenBatch() {
  if (!activeBatchId.value) {
    activeBatch.value = null
    return
  }
  try {
    activeBatch.value = await getCostBatch(activeBatchId.value)
    batchMessage.value = `已重新打开底稿 ${activeBatchId.value}`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '底稿读取失败'
  }
}

async function recalculateBatch() {
  if (!activeBatch.value || batchBusy.value) return
  batchBusy.value = true
  try {
    const result = await recalculateCostBatch(activeBatch.value.batch_id)
    if (!result.entry) throw new Error(result.message)
    activeBatch.value = result.entry
    batchMessage.value = result.message
    await refreshBatches()
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重算失败'
  } finally {
    batchBusy.value = false
  }
}

async function confirmBatch() {
  if (!activeBatch.value || batchBusy.value) return
  const batch = activeBatch.value
  const ids = selectedIds.value.length
    ? selectedIds.value
    : batch.assets.map((asset) => asset.entry_id)
  batchBusy.value = true
  try {
    const result = await confirmCostBatch(batch.batch_id, ids)
    if (!result.entry) throw new Error(result.message)
    activeBatch.value = result.entry
    batchMessage.value = result.message
    await refreshBatches()
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '确认失败'
  } finally {
    batchBusy.value = false
  }
}

watch(
  () => props.moduleKey,
  () => {
    selectedIds.value = []
    activeBatch.value = null
    activeBatchId.value = ''
    batchMessage.value = ''
    void refreshBatches()
  },
)

watch(rows, (current) => {
  const present = new Set(current.map((row) => Number(row.id)))
  selectedIds.value = selectedIds.value.filter((id) => present.has(id))
})

onMounted(async () => {
  await reload()
  await refreshBatches()
})
</script>

<style scoped>
.select-col { width: 36px; text-align: center; }
.batch-bar { align-items: center; }
.batch-bar .hint { color: var(--muted); font-size: 12px; }
.batch-bar select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; }
.batch-message { margin: 10px 0 0; padding: 8px 12px; background: #eff8ff; border: 1px solid #b2ddff; border-radius: 6px; font-size: 13px; color: #175cd3; }
</style>
