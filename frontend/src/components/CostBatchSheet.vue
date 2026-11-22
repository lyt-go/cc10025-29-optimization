<template>
  <section class="batch-sheet" data-testid="cost-batch-sheet">
    <header class="batch-head">
      <div>
        <h3>批量核算底稿 {{ batch.batch_id }}</h3>
        <p class="batch-desc">
          {{ batch.label }} · 采样位置 {{ batch.sampling_point }} ·
          第 {{ batch.recalc_count }} 次重算 · 源作业读取 {{ batch.read_stats.backfill_measurement_calls ?? 0 }} 次
        </p>
      </div>
      <div class="batch-actions">
        <button class="btn" data-testid="batch-recalculate" type="button" @click="$emit('recalculate')">
          重算
        </button>
        <button class="btn primary" data-testid="batch-confirm" type="button" @click="$emit('confirm')">
          确认选中资产
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">开批口径合计</span>
        <strong class="stat-value">{{ batch.totals.baseline_text }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">最新口径合计</span>
        <strong class="stat-value">{{ batch.totals.current_text }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">差额合计</span>
        <strong class="stat-value">{{ batch.totals.variance_text }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">本次入账 / 结转下一批</span>
        <strong class="stat-value">{{ batch.totals.included_text }} / {{ batch.totals.carried_text }}</strong>
      </article>
    </div>

    <table class="data-table batch-assets">
      <thead>
        <tr>
          <th>资产</th>
          <th>计费项</th>
          <th>开批用量</th>
          <th>最新用量</th>
          <th>修订</th>
          <th>开批金额</th>
          <th>最新金额</th>
          <th>差额</th>
          <th>已入账</th>
          <th>用量确认时间</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="asset in batch.assets" :key="asset.asset_id">
          <tr class="asset-row">
            <td>{{ asset.asset_id }}（{{ asset.record_id }}）</td>
            <td colspan="4">
              {{ asset.decision }} · {{ asset.decision_reason }}
            </td>
            <td>{{ asset.baseline_total.toFixed(2) }}</td>
            <td>{{ asset.current_total.toFixed(2) }}</td>
            <td>{{ asset.variance_total.toFixed(2) }}</td>
            <td>{{ asset.applied_total.toFixed(2) }}</td>
            <td>{{ asset.confirmed_at ? formatTime(asset.confirmed_at) : '未确认' }}</td>
          </tr>
          <tr v-for="item in asset.items" :key="`${asset.asset_id}-${item.item}`">
            <td></td>
            <td>{{ item.label }}</td>
            <td>{{ item.baseline_usage || '—' }}</td>
            <td>{{ item.current_usage || '—' }}</td>
            <td>{{ item.revision || '—' }} {{ item.measurement_kind }}</td>
            <td>{{ item.baseline_amount.toFixed(2) }}</td>
            <td>{{ item.current_amount.toFixed(2) }}</td>
            <td :class="{ 'diff-up': item.variance > 0, 'diff-down': item.variance < 0 }">
              {{ item.variance_text }}
            </td>
            <td>{{ item.applied_amount.toFixed(2) }}</td>
            <td>{{ item.usage_confirmed_at ? formatTime(item.usage_confirmed_at) : item.usage_basis }}</td>
          </tr>
        </template>
      </tbody>
    </table>

    <p class="batch-foot">
      整组合计 {{ batch.totals.batch_total_text }} ·
      台账 {{ batch.totals.ledger_count }} 行 ·
      采样位置 {{ batch.sampling_point }}
    </p>
  </section>
</template>

<script setup lang="ts">
import type { CostBatch } from '@/data/types'

defineProps<{ batch: CostBatch }>()
defineEmits<{ recalculate: []; confirm: [] }>()

function formatTime(value: string): string {
  if (!value) return '—'
  return value.replace('T', ' ').replace('Z', '').slice(0, 19)
}
</script>

<style scoped>
.batch-sheet { margin-top: 16px; background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 14px; }
.batch-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.batch-head h3 { margin: 0; font-size: 15px; }
.batch-desc { margin: 4px 0 10px; color: var(--muted); font-size: 12px; }
.batch-actions { display: flex; gap: 8px; }
.batch-assets { margin-top: 10px; }
.asset-row td { background: #f8fafc; font-weight: 600; }
.diff-up { color: #b42318; }
.diff-down { color: #067647; }
.batch-foot { margin: 8px 0 0; color: var(--muted); font-size: 12px; }
</style>
