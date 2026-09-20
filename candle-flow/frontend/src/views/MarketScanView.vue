<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  apiErrorText,
  fetchMarketScan,
  fetchMarketScanOverlay,
  type MarketScanCoverage,
  type MarketScanData,
  type MarketScanItem,
  type MarketScanOverlayData,
  type MarketScanOverlayFields,
  type MarketScanSort,
} from '@/api'

/** 五维展示顺序与后端 DIM_KEYS 一一对应；短标签只用于列头，title 里给全称。 */
const DIMS: { key: Exclude<MarketScanSort, 'composite_score'>; label: string; full: string }[] = [
  { key: 'profitability', label: '盈利', full: '盈利能力' },
  { key: 'growth', label: '成长', full: '成长性' },
  { key: 'cashflow', label: '现金流', full: '现金流质量' },
  { key: 'solvency', label: '偿债', full: '偿债能力' },
  { key: 'valuation', label: '估值', full: '估值合理性' },
]

const SORT_OPTIONS: { value: MarketScanSort; label: string }[] = [
  { value: 'composite_score', label: '综合分（权威口径）' },
  ...DIMS.map((d) => ({ value: d.key as MarketScanSort, label: `按${d.full}排序` })),
]

const loading = ref(false)
const error = ref('')
const report = ref<MarketScanData | null>(null)

const form = ref({
  sort_by: 'composite_score' as MarketScanSort,
  industry: '',
  min_composite: '' as string,
  min_market_cap_yi: '' as string,
  exclude_st: true,
  top: 50,
})

const coverage = computed<MarketScanCoverage | null>(() => report.value?.coverage ?? null)
const items = computed<MarketScanItem[]>(() => report.value?.items ?? [])
const isDimSort = computed(() => form.value.sort_by !== 'composite_score')
const dimSortLabel = computed(
  () => DIMS.find((d) => d.key === form.value.sort_by)?.full ?? '',
)

/** 覆盖率不足或快照滞后都要显式提示——榜单只覆盖已构建样本，不能当成全市场。 */
const coverageWarning = computed(() => {
  const c = coverage.value
  if (!c) return ''
  const msgs: string[] = []
  if (!c.covered) return '因子库为空，请先构建后才能出榜。'
  if (!c.complete) {
    msgs.push(`当前只覆盖 ${c.covered} / ${c.universe} 只（${c.coverage_pct}%），未覆盖标的不在榜内`)
  }
  if (c.stale_days != null && c.stale_days > 3) {
    msgs.push(`快照最近构建于 ${formatTime(c.latest_built_at)}，已滞后 ${c.stale_days} 天`)
  }
  return msgs.join('；')
})

const coverageTone = computed(() => {
  const c = coverage.value
  if (!c || !c.covered) return 'bad'
  if (!c.complete || (c.stale_days ?? 0) > 3) return 'warn'
  return 'ok'
})

function num(v: number | null | undefined): string {
  return v == null ? '—' : Number(v).toFixed(1)
}

function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`)
  if (Number.isNaN(d.getTime())) return iso.slice(0, 16).replace('T', ' ')
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function scoreTone(score: number | null | undefined): string {
  if (score == null) return 'plain'
  if (score >= 85) return 'hot'
  if (score >= 70) return 'warm'
  if (score >= 55) return 'mid'
  return 'cool'
}

function ratingTone(rating: string | null | undefined): string {
  const r = (rating || '').toUpperCase()
  if (!r) return 'plain'
  if (r.startsWith('A')) return 'strong'
  if (r.startsWith('B')) return 'medium'
  if (r.startsWith('C')) return 'weak'
  return 'risk'
}

function dimValue(row: MarketScanItem, label: string): number | null {
  return row.dim_scores?.[label] ?? null
}

function pctWidth(v: number | null | undefined): string {
  if (v == null) return '0%'
  return `${Math.max(0, Math.min(100, Number(v)))}%`
}

async function load() {
  loading.value = true
  error.value = ''
  const q = form.value
  try {
    const { data } = await fetchMarketScan({
      top: Number(q.top) || 50,
      sort_by: q.sort_by,
      industry: q.industry.trim() || undefined,
      min_composite: q.min_composite === '' ? undefined : Number(q.min_composite),
      min_market_cap_yi: q.min_market_cap_yi === '' ? undefined : Number(q.min_market_cap_yi),
      exclude_st: q.exclude_st,
    })
    report.value = data.data ?? null
  } catch (e) {
    report.value = null
    error.value = apiErrorText(e, '榜单加载失败')
  } finally {
    loading.value = false
  }
}

function resetFilters() {
  const sortBy = form.value.sort_by
  form.value = {
    sort_by: sortBy,
    industry: '',
    min_composite: '',
    min_market_cap_yi: '',
    exclude_st: true,
    top: 50,
  }
  overlayOn.value = false
  overlay.value = null
  load()
}

// ── 技术共振叠加（三层漏斗第二、三层）─────────────────────
const overlayOn = ref(false)
const overlayLoading = ref(false)
const overlayError = ref('')
const overlay = ref<MarketScanOverlayData | null>(null)
const onlySignaled = ref(false)

/** 激活叠加后榜单数据源切到 overlay items（= 榜单 items + 技术字段），综合分不变。 */
const displayItems = computed<(MarketScanItem & Partial<MarketScanOverlayFields>)[]>(() => {
  if (overlayOn.value && overlay.value) {
    const rows = overlay.value.items
    return onlySignaled.value
      ? rows.filter((r) => r.buy_signal === 'strong_buy' || r.buy_signal === 'watch' || r.buy_signal === 'short_term' || r.pattern_name)
      : rows
  }
  return items.value
})

const overlayStats = computed(() => overlay.value?.stats ?? null)
const signalCounts = computed(() => overlay.value?.signal_counts ?? {})
const industryConfluence = computed(() => overlay.value?.industry_confluence ?? [])

const SIGNAL_LABELS: Record<string, string> = {
  strong_buy: '强买入',
  watch: '观察',
  short_term: '短线博弈',
  neutral: '中性',
  insufficient_data: '数据不足',
  unknown: '分析失败',
}

function signalTone(sig: string | null | undefined): string {
  if (sig === 'strong_buy') return 'strong'
  if (sig === 'watch') return 'medium'
  if (sig === 'short_term') return 'weak'
  return 'plain'
}

async function loadOverlay() {
  overlayLoading.value = true
  overlayError.value = ''
  const q = form.value
  try {
    const { data } = await fetchMarketScanOverlay({
      top: Math.min(Number(q.top) || 50, 120),
      sort_by: q.sort_by,
      industry: q.industry.trim() || undefined,
      min_composite: q.min_composite === '' ? undefined : Number(q.min_composite),
      min_market_cap_yi: q.min_market_cap_yi === '' ? undefined : Number(q.min_market_cap_yi),
      exclude_st: q.exclude_st,
    })
    overlay.value = data.data ?? null
    overlayOn.value = true
  } catch (e) {
    overlayError.value = apiErrorText(e, '技术共振叠加失败')
    overlayOn.value = false
  } finally {
    overlayLoading.value = false
  }
}

function toggleOverlay() {
  if (overlayOn.value) {
    overlayOn.value = false
    overlayError.value = ''
    return
  }
  if (overlay.value && overlay.value.filters.min_composite === (form.value.min_composite === '' ? null : Number(form.value.min_composite))) {
    overlayOn.value = true
    return
  }
  loadOverlay()
}

onMounted(load)
</script>

<template>
  <div class="scan-view">
    <header class="page-head">
      <h1>基本面榜单</h1>
      <p class="lead">
        取盘后预构建的全市场因子库排序。分数与「个股分析」同一口径
        （盈利能力 0.20 / 成长性 0.16 / 偿债能力 0.16 / 现金流质量 0.32 / 估值合理性 0.16，
        含 E 档打折、风险乘数与公告事件扣分），榜单层不重算、不另设权重。
      </p>
    </header>

    <section class="card coverage" :class="coverageTone">
      <div class="coverage-head">
        <h2>因子库覆盖</h2>
        <button class="btn-secondary" type="button" :disabled="loading" @click="load">
          {{ loading ? '加载中…' : '刷新' }}
        </button>
      </div>
      <div v-if="coverage" class="coverage-body">
        <div class="coverage-figures">
          <span class="figure">
            <b>{{ coverage.covered }}</b>
            <em>/ {{ coverage.universe }} 只</em>
          </span>
          <span class="figure">
            <b>{{ coverage.coverage_pct }}%</b>
            <em>覆盖率</em>
          </span>
          <span class="figure">
            <b>{{ coverage.remaining }}</b>
            <em>待构建</em>
          </span>
          <span class="figure">
            <b>{{ formatTime(coverage.latest_built_at) }}</b>
            <em>最近构建</em>
          </span>
        </div>
        <div class="bar">
          <div class="bar-fill" :style="{ width: pctWidth(coverage.coverage_pct) }" />
        </div>
        <p v-if="coverageWarning" class="coverage-warn">{{ coverageWarning }}</p>
        <p v-else class="coverage-ok">已覆盖全部沪深标的，榜单即为全市场排序。</p>
        <p class="coverage-hint">
          缺口源自单次构建预算截断（单只约 13s）。补齐：
          <code>POST /api/v1/fundamentals/factors/rebuild?budget_sec=10800&amp;max_symbols=4300</code>
          ，或在服务端把 <code>FACTOR_BUILD_DEADLINE_SEC</code> 放宽后等夜间批跑。
        </p>
      </div>
      <p v-else class="coverage-warn">覆盖率读取失败。</p>
    </section>

    <section class="card filters">
      <label class="field">
        <span>排序维度</span>
        <select v-model="form.sort_by">
          <option v-for="opt in SORT_OPTIONS" :key="opt.value" :value="opt.value">
            {{ opt.label }}
          </option>
        </select>
      </label>
      <label class="field">
        <span>行业包含</span>
        <input
          v-model="form.industry"
          type="text"
          placeholder="如 银行 / 煤炭"
          @keyup.enter="load"
        />
      </label>
      <label class="field">
        <span>综合分 ≥</span>
        <input v-model="form.min_composite" type="number" min="0" max="100" placeholder="不限" />
      </label>
      <label class="field">
        <span>市值 ≥（亿）</span>
        <input v-model="form.min_market_cap_yi" type="number" min="0" placeholder="不限" />
      </label>
      <label class="field">
        <span>条数</span>
        <select v-model.number="form.top">
          <option v-for="n in [20, 50, 100, 200, 500]" :key="n" :value="n">{{ n }}</option>
        </select>
      </label>
      <label class="field checkbox">
        <input v-model="form.exclude_st" type="checkbox" />
        <span>剔除 ST / 退市</span>
      </label>
      <div class="actions">
        <button class="btn-primary" type="button" :disabled="loading" @click="load">
          {{ loading ? '加载中…' : '查询' }}
        </button>
        <button class="btn-secondary" type="button" :disabled="loading" @click="resetFilters">
          重置
        </button>
      </div>
    </section>

    <p v-if="error" class="error">{{ error }}</p>

    <section v-if="report" class="card result">
      <div class="result-head">
        <div>
          <h2>
            榜单
            <template v-if="isDimSort">（按{{ dimSortLabel }}排序）</template>
            <template v-if="overlayOn">· 技术共振已叠加</template>
          </h2>
          <p class="sub">
            命中 {{ report.matched }} 只，显示 {{ displayItems.length }} 只 ·
            分位基准 {{ report.percentile_base }} 只
            <template v-if="report.filters.industry"> · 行业含「{{ report.filters.industry }}」</template>
          </p>
        </div>
        <button
          class="btn-secondary"
          type="button"
          :disabled="overlayLoading"
          :class="{ active: overlayOn }"
          @click="toggleOverlay"
        >
          {{ overlayLoading ? '共振计算中…' : overlayOn ? '已叠加技术面 ✓ 关闭' : '叠加技术面' }}
        </button>
      </div>

      <p v-if="overlayError" class="error overlay-error">{{ overlayError }}</p>

      <div v-if="overlayOn && overlay" class="overlay-summary">
        <div class="sig-counts">
          <span v-if="signalCounts.strong_buy" class="sig-chip strong">强买入 {{ signalCounts.strong_buy }}</span>
          <span v-if="signalCounts.watch" class="sig-chip medium">观察 {{ signalCounts.watch }}</span>
          <span v-if="signalCounts.short_term" class="sig-chip weak">短线博弈 {{ signalCounts.short_term }}</span>
          <span v-if="signalCounts.neutral" class="sig-chip plain">中性 {{ signalCounts.neutral }}</span>
          <label class="field checkbox inline">
            <input v-model="onlySignaled" type="checkbox" />
            <span>仅看有信号/共振</span>
          </label>
        </div>
        <p v-if="industryConfluence.length" class="industry-conf">
          板块效应：
          <span v-for="ic in industryConfluence" :key="ic.industry" class="ind-chip">
            {{ ic.industry }} {{ ic.signaled }}/{{ ic.total }}<template v-if="ic.strong_buy">（强买 {{ ic.strong_buy }}）</template>
          </span>
        </p>
        <p v-if="overlayStats" class="overlay-note">
          K线可用 {{ overlayStats.kline_ok }}/{{ overlay?.count ?? overlayStats.kline_ok }} · 形态共振命中 {{ overlayStats.pattern_hits }} 只 ·
          {{ overlayStats.peg_note }}
        </p>
      </div>

      <div v-if="displayItems.length" class="table-wrap">
        <table class="scan-table">
          <thead>
            <tr>
              <th class="col-rank">#</th>
              <th>标的</th>
              <th class="col-industry">行业</th>
              <th class="col-score">综合分</th>
              <th>评级</th>
              <th
                v-for="d in DIMS"
                :key="d.key"
                class="col-dim"
                :class="{ active: form.sort_by === d.key }"
                :title="d.full"
              >
                {{ d.label }}
              </th>
              <th v-if="overlayOn" class="col-signal" title="强买入=基本面≥80+PEG<1.5+站上MA20+放量20%；观察=高分+PEG>2+回踩MA60缩量；短线博弈=基本面<60但突破MA20放量">
                买点信号
              </th>
              <th v-if="overlayOn" class="col-pattern" title="bullish 形态 × 趋势/动量/波动/量价/结构正交共振（含周线趋势多周期确认）；组合分=形态分+有效共振数×6，与信号页同源">
                形态共振
              </th>
              <th class="col-pct">分位</th>
              <th class="col-num hide-mobile">PE</th>
              <th class="col-num hide-mobile">PB</th>
              <th class="col-num hide-mobile">市值(亿)</th>
              <th class="col-num hide-mobile">股息率%</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, idx) in displayItems" :key="row.symbol">
              <td class="col-rank rank">{{ idx + 1 }}</td>
              <td class="col-symbol">
                <div class="sym-cell">
                  <span class="code">{{ row.symbol.split('.')[0] }}</span>
                  <span class="name">{{ row.name || '—' }}</span>
                </div>
              </td>
              <td class="col-industry industry">{{ row.industry || '—' }}</td>
              <td class="col-score">
                <span class="score" :class="scoreTone(row.composite_score)">
                  {{ num(row.composite_score) }}
                </span>
              </td>
              <td>
                <span class="rating" :class="ratingTone(row.final_rating)">
                  {{ row.final_rating || '—' }}
                </span>
              </td>
              <td
                v-for="d in DIMS"
                :key="d.key"
                class="col-dim"
                :class="{ active: form.sort_by === d.key }"
              >
                {{ num(dimValue(row, d.full)) }}
              </td>
              <td v-if="overlayOn" class="col-signal">
                <span class="rating" :class="signalTone(row.buy_signal)" :title="(row.buy_reasons || []).join('；')">
                  {{ SIGNAL_LABELS[row.buy_signal || ''] ?? '—' }}
                </span>
              </td>
              <td v-if="overlayOn" class="col-pattern">
                <template v-if="row.pattern_name">
                  <span class="pattern-name" :title="row.confluence_hits || ''">{{ row.pattern_name }}</span>
                  <span class="pattern-conf">共振 {{ num(row.confluence_effective) }} · 组合 {{ num(row.combined_score) }}</span>
                </template>
                <span v-else class="pattern-none">—</span>
              </td>
              <td class="col-pct">
                <div class="pct">
                  <span class="pct-num">{{ num(row.market_pct) }}</span>
                  <span class="pct-track"><i :style="{ width: pctWidth(row.market_pct) }" /></span>
                  <span class="pct-ind">行业 {{ num(row.industry_pct) }}</span>
                </div>
              </td>
              <td class="col-num hide-mobile">{{ num(row.pe_ttm) }}</td>
              <td class="col-num hide-mobile">{{ num(row.pb) }}</td>
              <td class="col-num hide-mobile">{{ num(row.market_cap_yi) }}</td>
              <td class="col-num hide-mobile">{{ num(row.dividend_yield) }}</td>
              <td class="col-action">
                <RouterLink class="detail-link" :to="`/chart/${row.symbol}`">详情</RouterLink>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="empty">
        当前条件下无命中。可放宽综合分/市值下限，或确认因子库是否已构建。
      </p>

      <ul v-if="overlayOn && overlay && overlay.notes.length" class="notes">
        <li v-for="(note, i) in overlay.notes" :key="`o${i}`">{{ note }}</li>
      </ul>
      <ul v-else-if="report.notes.length" class="notes">
        <li v-for="(note, i) in report.notes" :key="i">{{ note }}</li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.scan-view { max-width: 1400px; }

.page-head h1 { margin: 0 0 var(--space-sm); font-size: 1.6rem; letter-spacing: -0.02em; }
.lead { color: var(--text-secondary); font-size: 14px; line-height: 1.6; margin: 0 0 var(--space-lg); }

.card { margin-bottom: var(--space-md); }

/* ── 技术共振叠加 ─────────────────────────────── */
.result-head .btn-secondary.active {
  border-color: var(--color-primary);
  color: var(--color-primary);
}
.overlay-error { margin: 0 0 var(--space-sm); }
.overlay-summary {
  display: flex;
  flex-direction: column;
  gap: var(--space-xs);
  padding: var(--space-sm) var(--space-md);
  margin-bottom: var(--space-sm);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: color-mix(in srgb, var(--color-primary) 4%, transparent);
}
.sig-counts { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-xs); }
.sig-chip {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
}
.sig-chip.strong { background: rgba(245, 34, 45, 0.12); color: var(--color-up); }
.sig-chip.medium { background: rgba(250, 140, 22, 0.14); color: #d46b08; }
.sig-chip.weak { background: rgba(24, 144, 255, 0.12); color: var(--color-primary); }
.sig-chip.plain { background: var(--border-color); color: var(--text-secondary); }
.field.checkbox.inline { margin-left: var(--space-sm); }
.industry-conf { margin: 0; font-size: 12.5px; color: var(--text-secondary); }
.ind-chip {
  display: inline-block;
  margin-right: var(--space-xs);
  padding: 1px 8px;
  border: 1px solid var(--border-color);
  border-radius: 6px;
}
.overlay-note { margin: 0; font-size: 12px; color: var(--text-secondary); }
.col-signal { white-space: nowrap; }
.col-pattern { min-width: 150px; }
.pattern-name { font-weight: 600; display: block; }
.pattern-conf { display: block; font-size: 11.5px; color: var(--text-secondary); }
.pattern-none { color: var(--text-secondary); }

/* ── 覆盖率 ───────────────────────────────── */
.coverage-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-md);
  margin-bottom: var(--space-md);
}
.coverage-head h2 { margin: 0; font-size: 1.05rem; }
.coverage.ok { border-color: color-mix(in srgb, var(--color-down) 35%, var(--border-color)); }
.coverage.warn { border-color: color-mix(in srgb, var(--color-warning, #c47b1a) 45%, transparent); }
.coverage.bad { border-color: color-mix(in srgb, var(--color-up) 45%, transparent); }
.coverage-figures {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-lg);
  margin-bottom: var(--space-sm);
}
.figure { display: flex; flex-direction: column; gap: 2px; }
.figure b { font-size: 1.15rem; font-variant-numeric: tabular-nums; }
.figure em { font-style: normal; font-size: 12px; color: var(--text-secondary); }
.bar {
  height: 8px;
  border-radius: 999px;
  background: var(--bg-page);
  overflow: hidden;
  margin-bottom: var(--space-sm);
}
.bar-fill { height: 100%; background: var(--color-primary); transition: width 0.25s ease; }
.coverage-warn,
.coverage-ok,
.coverage-hint { margin: 0; font-size: 13px; line-height: 1.6; }
.coverage-warn { color: #d46b08; }
.coverage-ok { color: var(--text-secondary); }
.coverage-hint { margin-top: 6px; color: var(--text-secondary); }
.coverage-hint code {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--bg-page);
  border: 1px solid var(--border-color);
  font-size: 12px;
  word-break: break-all;
}

/* ── 筛选器 ───────────────────────────────── */
.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: var(--space-md);
}
.field { display: flex; flex-direction: column; gap: 4px; }
.field > span { font-size: 12px; color: var(--text-secondary); }
.field input[type='text'],
.field input[type='number'] { min-width: 130px; }
.field.checkbox {
  flex-direction: row;
  align-items: center;
  gap: var(--space-sm);
  padding-bottom: 9px;
}
.field.checkbox span { font-size: 13px; color: var(--text-primary); }
.actions { display: flex; gap: var(--space-sm); margin-left: auto; }

/* ── 结果 ─────────────────────────────────── */
.result-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-md);
  flex-wrap: wrap;
  margin-bottom: var(--space-md);
}
.result-head h2 { margin: 0; font-size: 1.05rem; }
.sub { margin: 6px 0 0; font-size: 13px; color: var(--text-secondary); }

.table-wrap { overflow-x: auto; }
.scan-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.scan-table th,
.scan-table td {
  text-align: right;
  padding: 8px 8px;
  border-bottom: 1px solid var(--border-color);
  white-space: nowrap;
}
.scan-table th { color: var(--text-secondary); font-weight: 500; }
.scan-table th.col-rank,
.scan-table td.col-rank,
.scan-table td.col-symbol,
.scan-table th:nth-child(2),
.scan-table td.col-industry,
.scan-table th.col-industry { text-align: left; }
.col-symbol { text-align: left; }
.sym-cell { display: flex; flex-direction: column; gap: 2px; }
.code { font-weight: 600; font-variant-numeric: tabular-nums; }
.name { font-size: 12px; color: var(--text-secondary); }
.rank { color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.industry { color: var(--text-secondary); font-size: 12px; }
.col-dim { width: 3.2rem; font-variant-numeric: tabular-nums; color: var(--text-secondary); }
.col-dim.active { background: rgba(24, 144, 255, 0.08); color: var(--text-primary); }
.col-pct { width: 5.5rem; }
.pct { display: flex; flex-direction: column; align-items: flex-end; gap: 3px; }
.pct-num { font-variant-numeric: tabular-nums; }
.pct-track {
  display: block;
  width: 100%;
  height: 4px;
  border-radius: 999px;
  background: var(--bg-page);
  overflow: hidden;
}
.pct-track i { display: block; height: 100%; background: var(--color-primary); }
.pct-ind { font-size: 11px; color: var(--text-secondary); }
.col-num { font-variant-numeric: tabular-nums; }
.col-action { text-align: right; }

.score {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 2.9rem;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.score.hot { background: rgba(245, 34, 45, 0.12); color: var(--color-up); }
.score.warm { background: rgba(250, 140, 22, 0.14); color: #d46b08; }
.score.mid { background: rgba(24, 144, 255, 0.12); color: var(--color-primary); }
.score.cool,
.score.plain { background: var(--bg-page); color: var(--text-secondary); }

.rating {
  display: inline-block;
  padding: 2px 7px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
  background: var(--bg-page);
  color: var(--text-secondary);
}
.rating.strong { background: rgba(245, 34, 45, 0.12); color: var(--color-up); }
.rating.medium { background: rgba(250, 140, 22, 0.14); color: #d46b08; }
.rating.weak { background: rgba(24, 144, 255, 0.12); color: var(--color-primary); }
.rating.risk { background: rgba(82, 196, 26, 0.14); color: var(--color-down); }

.detail-link {
  display: inline-flex;
  align-items: center;
  padding: 4px 10px;
  border-radius: 6px;
  border: 1px solid var(--border-color);
  background: var(--bg-light);
  font-size: 12px;
  white-space: nowrap;
}
.detail-link:hover { border-color: var(--color-primary); color: var(--color-primary); }

.empty { color: var(--text-secondary); font-size: 14px; margin: 0; }
.error { color: var(--color-up); margin: 0 0 var(--space-md); }

.notes {
  margin: var(--space-md) 0 0;
  padding-left: 1.1rem;
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.7;
}

@media (max-width: 768px) {
  .actions { margin-left: 0; width: 100%; }
  .actions button { flex: 1; }
  .field input[type='text'],
  .field input[type='number'] { min-width: 0; width: 100%; }
  .field { flex: 1 1 45%; }
  .scan-table th,
  .scan-table td { padding: 8px 6px; }
}
</style>
