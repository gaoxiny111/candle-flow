<script setup lang="ts">
import { ref, watch } from 'vue'
import { apiErrorText, fetchFundamentalAnalysis, type FundamentalAnalysisReport } from '@/api'

const props = defineProps<{ symbol: string }>()

const loading = ref(false)
const error = ref('')
const report = ref<FundamentalAnalysisReport | null>(null)

const MODULE_ORDER = [
  'profitability',
  'growth',
  'cashflow',
  'solvency',
  'efficiency',
  'industry',
  'risk',
] as const

const LEVEL_LABEL: Record<string, string> = {
  A: '优秀',
  'A-': '优秀',
  'B+': '良好',
  B: '良好',
  'B-': '良好',
  C: '中性',
  D: '较差',
  E: '危险',
}

function levelTone(level: string) {
  if (level.startsWith('A') || level.startsWith('B')) return 'good'
  if (level === 'D' || level === 'E') return 'bad'
  return 'mid'
}

function scoreTone(score: number) {
  if (score >= 80) return 'good'
  if (score >= 60) return 'mid'
  return 'bad'
}

async function load() {
  if (!props.symbol) return
  loading.value = true
  error.value = ''
  report.value = null
  try {
    const { data } = await fetchFundamentalAnalysis(props.symbol)
    report.value = data.data ?? null
    if (!report.value) error.value = '暂无分析结果'
  } catch (e) {
    error.value = apiErrorText(e)
  } finally {
    loading.value = false
  }
}

watch(() => props.symbol, load, { immediate: true })
</script>

<template>
  <div class="fundamental-panel card">
    <div class="panel-head">
      <h3>基本面分析</h3>
      <button class="btn-ghost" type="button" :disabled="loading" @click="load">
        {{ loading ? '分析中…' : '刷新' }}
      </button>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <p v-else-if="loading" class="muted">正在拉取财报并运行分析模型…</p>

    <template v-else-if="report">
      <div class="hero">
        <div class="hero-score" :class="scoreTone(report.composite_score)">
          <span class="num">{{ report.composite_score }}</span>
          <span class="lbl">综合分</span>
        </div>
        <div class="hero-meta">
          <div class="title">{{ report.name || report.symbol }}</div>
          <div class="sub">
            <span v-if="report.industry">{{ report.industry }}</span>
            <span v-if="report.report_dates?.length"> · 报告期 {{ report.report_dates.slice(-1)[0] }}</span>
          </div>
          <div class="rating">
            评级
            <strong :class="levelTone(report.final_rating)">{{ report.final_rating }}</strong>
            {{ LEVEL_LABEL[report.final_rating] || '' }}
          </div>
          <div v-if="report.market?.price" class="market">
            现价 {{ report.market.price?.toFixed(2) }}
            <span v-if="report.market.pe_ttm"> · PE {{ report.market.pe_ttm?.toFixed(1) }}</span>
            <span v-if="report.market.pe_percentile != null">
              · PE分位 {{ report.market.pe_percentile?.toFixed(0) }}%
            </span>
          </div>
        </div>
      </div>

      <div v-if="report.warnings?.length" class="warnings">
        <div class="warn-title">风险提示</div>
        <ul>
          <li v-for="(w, i) in report.warnings" :key="i">{{ w }}</li>
        </ul>
      </div>

      <div class="modules">
        <section
          v-for="key in MODULE_ORDER"
          :key="key"
          v-show="report.modules?.[key]"
          class="module-card"
        >
          <div class="module-head">
            <span>{{ report.modules[key].module_name }}</span>
            <span class="module-score" :class="scoreTone(report.modules[key].score)">
              {{ report.modules[key].score }} · {{ report.modules[key].level }}
            </span>
          </div>
          <ul v-if="report.modules[key].indicators?.length" class="indicators">
            <li v-for="ind in report.modules[key].indicators" :key="ind.name">
              <span class="ind-name">{{ ind.name }}</span>
              <span class="ind-val">{{ ind.value }}</span>
              <span class="ind-score" :class="scoreTone(ind.score)">{{ ind.score.toFixed(0) }}</span>
              <span v-if="ind.comment" class="ind-comment">{{ ind.comment }}</span>
            </li>
          </ul>
        </section>
      </div>

      <section v-if="report.valuation?.relative" class="valuation card-inner">
        <h4>相对估值</h4>
        <div class="val-grid">
          <div v-for="(v, k) in report.valuation.relative" :key="k" class="val-item">
            <span class="val-key">{{ k }}</span>
            <span class="val-signal" :class="v.signal === '低估' ? 'good' : v.signal === '高估' ? 'bad' : 'mid'">
              {{ v.signal || '—' }}
            </span>
            <span v-if="v.current != null" class="val-num">{{ v.current }}</span>
            <span v-if="v.percentile_5y != null" class="val-sub">分位 {{ v.percentile_5y }}%</span>
          </div>
        </div>
      </section>

      <section v-if="report.valuation?.dcf" class="valuation card-inner">
        <h4>DCF 内在价值</h4>
        <p v-if="report.valuation.dcf.note" class="muted">{{ report.valuation.dcf.note }}</p>
        <template v-else>
          <p>
            每股内在价值
            <strong>{{ report.valuation.dcf.intrinsic_value_per_share ?? '—' }}</strong>
            <span v-if="report.valuation.dcf.margin_of_safety_pct != null">
              · 安全边际 {{ report.valuation.dcf.margin_of_safety_pct }}%
            </span>
          </p>
        </template>
      </section>

      <pre v-if="report.summary" class="summary">{{ report.summary }}</pre>
    </template>
  </div>
</template>

<style scoped>
.fundamental-panel { padding: var(--space-md); }
.panel-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: var(--space-md); }
.panel-head h3 { margin: 0; font-size: 16px; }
.btn-ghost {
  border: 1px solid var(--border-color);
  background: transparent;
  padding: 4px 12px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 13px;
}
.err { color: #f5222d; font-size: 14px; }
.muted { color: var(--text-secondary); font-size: 14px; }
.hero { display: flex; gap: var(--space-md); margin-bottom: var(--space-md); align-items: center; }
.hero-score {
  width: 88px; height: 88px; border-radius: 12px;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  background: var(--bg-secondary, #f5f5f5);
}
.hero-score.good { background: #f6ffed; color: #389e0d; }
.hero-score.mid { background: #fffbe6; color: #d48806; }
.hero-score.bad { background: #fff1f0; color: #cf1322; }
.hero-score .num { font-size: 28px; font-weight: 700; line-height: 1; }
.hero-score .lbl { font-size: 12px; margin-top: 4px; }
.hero-meta .title { font-size: 18px; font-weight: 600; }
.hero-meta .sub { font-size: 13px; color: var(--text-secondary); margin-top: 4px; }
.rating { margin-top: 8px; font-size: 14px; }
.rating strong.good { color: #389e0d; }
.rating strong.bad { color: #cf1322; }
.market { font-size: 13px; color: var(--text-secondary); margin-top: 4px; }
.warnings {
  background: #fff7e6; border: 1px solid #ffd591; border-radius: 8px;
  padding: 10px 12px; margin-bottom: var(--space-md); font-size: 13px;
}
[data-theme='dark'] .warnings { background: #2b2111; border-color: #ad6800; }
.warnings ul { margin: 6px 0 0; padding-left: 18px; }
.modules { display: flex; flex-direction: column; gap: 10px; margin-bottom: var(--space-md); }
.module-card {
  border: 1px solid var(--border-color); border-radius: 8px; padding: 10px 12px;
}
.module-head { display: flex; justify-content: space-between; font-weight: 600; font-size: 14px; margin-bottom: 6px; }
.module-score { font-weight: 600; font-size: 13px; }
.module-score.good { color: #389e0d; }
.module-score.mid { color: #d48806; }
.module-score.bad { color: #cf1322; }
.indicators { list-style: none; margin: 0; padding: 0; font-size: 13px; }
.indicators li {
  display: grid; grid-template-columns: 1fr auto auto; gap: 8px;
  padding: 4px 0; border-top: 1px dashed var(--border-color);
}
.ind-name { color: var(--text-secondary); }
.ind-val { font-variant-numeric: tabular-nums; }
.ind-score { font-weight: 600; min-width: 28px; text-align: right; }
.ind-score.good { color: #389e0d; }
.ind-score.mid { color: #d48806; }
.ind-score.bad { color: #cf1322; }
.ind-comment { grid-column: 1 / -1; font-size: 12px; color: var(--text-secondary); }
.valuation { margin-top: 10px; padding: 10px 0; border-top: 1px solid var(--border-color); }
.valuation h4 { margin: 0 0 8px; font-size: 14px; }
.val-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px; }
.val-item { font-size: 13px; padding: 8px; background: var(--bg-secondary, #fafafa); border-radius: 6px; }
.val-key { display: block; font-weight: 600; }
.val-signal.good { color: #389e0d; }
.val-signal.bad { color: #cf1322; }
.val-num { display: block; margin-top: 2px; }
.val-sub { font-size: 12px; color: var(--text-secondary); }
.summary {
  margin-top: var(--space-md); padding: 10px; background: var(--bg-secondary, #fafafa);
  border-radius: 8px; font-size: 12px; white-space: pre-wrap; color: var(--text-secondary);
}
</style>
