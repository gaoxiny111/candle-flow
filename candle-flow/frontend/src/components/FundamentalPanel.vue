<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { apiErrorText, fetchFundamentalAnalysis, type FundamentalAnalysisReport } from '@/api'
import { isEtfSymbol } from '@/utils/symbol'

const props = defineProps<{ symbol: string }>()

const loading = ref(false)
const error = ref('')
const report = ref<FundamentalAnalysisReport | null>(null)
const skipEtf = computed(() => isEtfSymbol(props.symbol))

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

function levelTone(level: string | null | undefined) {
  if (!level) return 'mid'
  if (level.startsWith('A') || level.startsWith('B')) return 'good'
  if (level === 'D' || level === 'E') return 'bad'
  return 'mid'
}

function scoreTone(score: number | null | undefined) {
  if (score == null) return 'mid'
  if (score >= 80) return 'good'
  if (score >= 60) return 'mid'
  return 'bad'
}

async function load() {
  if (!props.symbol) return
  if (isEtfSymbol(props.symbol)) {
    loading.value = false
    error.value = ''
    report.value = null
    return
  }
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

const displayWarnings = computed(() => {
  const list = report.value?.warnings ?? []
  const skip = new Set<string>()
  if (report.value?.cashflow_veto) skip.add('现金流不合格，暂不具备价值投资条件')
  if (report.value?.compliance_veto) {
    skip.add('命中生存级重大风险事件，财务打分不适用；请优先关注合规与生存风险')
    skip.add('命中重大风险事件，财务打分不适用；请优先关注合规与生存风险')
  }
  skip.add('命中观察级风险，警惕情绪杀跌；不等于公司生存危机')
  // 行业 N/A 已在模块头展示，避免风险提示重复刷屏
  skip.add('N/A（样本不足）')
  // 重大风险事件明细已在顶部卡片展示
  return list.filter((w) => !skip.has(w) && !w.startsWith('【') && !w.startsWith('〔观察〕'))
})
const majorRiskEvents = computed(() => report.value?.major_risks?.events ?? [])
const observeRiskEvents = computed(() => report.value?.major_risks?.observe_events ?? [])

// ── 五维雷达图（纯 SVG） ──────────────────────────────────
const RADAR_LABELS = ['盈利能力', '成长性', '现金流质量', '偿债能力', '估值合理性']
const RADAR_SIZE = 240 // SVG viewBox 边长
const RADAR_CX = RADAR_SIZE / 2
const RADAR_CY = RADAR_SIZE / 2
const RADAR_R = 90 // 数据半径

const radarData = computed(() => {
  const dims = report.value?.dim_scores ?? {}
  return RADAR_LABELS.map((name) => ({
    name,
    score: dims[name] ?? 0,
  }))
})

/** 生成每个顶点的极坐标 (x,y)，radius 按 score/100 映射 */
function radarPoint(i: number, radius: number): { x: number; y: number } {
  const n = RADAR_LABELS.length
  const angle = (-Math.PI / 2) + (i * 2 * Math.PI) / n
  return {
    x: RADAR_CX + radius * Math.cos(angle),
    y: RADAR_CY + radius * Math.sin(angle),
  }
}

const radarPolygonPoints = computed(() => {
  return radarData.value
    .map((d, i) => {
      const r = (Math.max(0, Math.min(100, d.score)) / 100) * RADAR_R
      const p = radarPoint(i, r)
      return `${p.x.toFixed(1)},${p.y.toFixed(1)}`
    })
    .join(' ')
})

/** 同心圆（5圈：20/40/60/80/100） */
const radarRings = [20, 40, 60, 80, 100]

/** 轴端点（最外圈） */
const radarAxisEnds = computed(() =>
  RADAR_LABELS.map((_, i) => radarPoint(i, RADAR_R)),
)

/** 标签位置（最外圈外 22px） */
const radarLabelPos = computed(() =>
  RADAR_LABELS.map((_, i) => radarPoint(i, RADAR_R + 22)),
)

/** 分数标注位置（数据点外 12px） */
const radarScorePos = computed(() =>
  radarData.value.map((d, i) => {
    const r = (Math.max(0, Math.min(100, d.score)) / 100) * RADAR_R
    const base = radarPoint(i, r)
    const outer = radarPoint(i, r + 14)
    // 往标签方向偏
    const dx = outer.x - base.x
    const dy = outer.y - base.y
    return {
      x: base.x + dx * 0.35,
      y: base.y + dy * 0.35,
    }
  }),
)

function bullBearTone(view: string | undefined) {
  if (view === '看多') return 'good'
  if (view === '中性') return 'mid'
  if (view === '偏空' || view === '看空') return 'bad'
  return 'mid'
}
</script>

<template>
  <div class="fundamental-panel card">
    <div class="panel-head">
      <h3>基本面分析</h3>
      <button class="btn-ghost" type="button" :disabled="loading || skipEtf" @click="load">
        {{ loading ? '分析中…' : '刷新' }}
      </button>
    </div>

    <p v-if="skipEtf" class="muted">ETF 不适用个股基本面评分，股息率、综合分与评级不计算。</p>
    <p v-else-if="error" class="err">{{ error }}</p>
    <p v-else-if="loading" class="muted">正在拉取财报并运行分析模型…</p>

    <template v-else-if="report && !report.skipped">
      <div class="hero">
        <div class="hero-score" :class="scoreTone(report.composite_score)">
          <span class="num">{{ report.composite_score }}</span>
          <span class="lbl">综合分</span>
        </div>
        <div class="hero-meta">
          <div class="title">{{ report.name || report.symbol }}</div>
          <div class="sub">
            <span v-if="report.industry">{{ report.industry }}</span>
            <span v-if="report.report_dates?.length">
              · 年报序列截至 {{ report.report_dates.slice(-1)[0] }}
            </span>
            <span v-if="report.latest_report">
              · 同比口径 {{ report.latest_report }}
            </span>
          </div>
          <div class="rating">
            评级
            <strong :class="levelTone(report.final_rating)">{{ report.final_rating }}</strong>
            {{ (report.final_rating && LEVEL_LABEL[report.final_rating]) || '' }}
          </div>
          <div v-if="report.market?.price" class="market">
            现价 {{ report.market.price?.toFixed(2) }}
            <span v-if="report.market.pe_ttm"> · PE {{ report.market.pe_ttm?.toFixed(1) }}</span>
            <span v-if="report.market.pe_percentile != null">
              · PE分位 {{ report.market.pe_percentile?.toFixed(0) }}%
            </span>
            <span v-else-if="report.market.pe_percentile_na" class="na">
              · PE分位 {{ report.market.pe_percentile_na }}
            </span>
          </div>
        </div>
      </div>

      <!-- 五维雷达 + 多空判断 -->
      <div v-if="report.dim_scores" class="radar-block">
        <div class="radar-wrap">
          <svg
            :viewBox="`0 0 ${RADAR_SIZE} ${RADAR_SIZE}`"
            class="radar-svg"
            aria-label="五维分析雷达图"
          >
            <!-- 同心圆 -->
            <polygon
              v-for="ring in radarRings"
              :key="ring"
              :points="RADAR_LABELS.map((_, i) => {
                const p = radarPoint(i, (ring / 100) * RADAR_R)
                return `${p.x.toFixed(1)},${p.y.toFixed(1)}`
              }).join(' ')"
              fill="none"
              stroke="var(--border-color, #e5e5e5)"
              stroke-width="1"
            />
            <!-- 轴线 -->
            <line
              v-for="(end, i) in radarAxisEnds"
              :key="`axis-${i}`"
              :x1="RADAR_CX"
              :y1="RADAR_CY"
              :x2="end.x"
              :y2="end.y"
              stroke="var(--border-color, #e5e5e5)"
              stroke-width="1"
            />
            <!-- 数据多边形 -->
            <polygon
              :points="radarPolygonPoints"
              fill="#e74c3c"
              fill-opacity="0.25"
              stroke="#e74c3c"
              stroke-width="2"
              stroke-linejoin="round"
            />
            <!-- 顶点小圆点 -->
            <circle
              v-for="(d, i) in radarData"
              :key="`dot-${i}`"
              :cx="radarPoint(i, (Math.max(0, Math.min(100, d.score)) / 100) * RADAR_R).x"
              :cy="radarPoint(i, (Math.max(0, Math.min(100, d.score)) / 100) * RADAR_R).y"
              r="3.5"
              fill="white"
              stroke="#e74c3c"
              stroke-width="2"
            />
            <!-- 维度标签 -->
            <text
              v-for="(pos, i) in radarLabelPos"
              :key="`lbl-${i}`"
              :x="pos.x"
              :y="pos.y"
              text-anchor="middle"
              dominant-baseline="central"
              font-size="12"
              font-weight="600"
              fill="var(--text-primary, #333)"
            >{{ RADAR_LABELS[i] }}</text>
            <!-- 顶点分数 -->
            <text
              v-for="(pos, i) in radarScorePos"
              :key="`sc-${i}`"
              :x="pos.x"
              :y="pos.y"
              text-anchor="middle"
              dominant-baseline="central"
              font-size="10"
              font-weight="700"
              fill="#e74c3c"
            >{{ radarData[i].score.toFixed(0) }}</text>
          </svg>
        </div>

        <div class="bull-bear-wrap">
          <!-- 短期 -->
          <div class="bull-card">
            <div class="bull-label">短期（1-2周）</div>
            <div class="bull-view" :class="bullBearTone(report.short_term_view?.view)">
              {{ report.short_term_view?.view ?? '—' }}
            </div>
            <div class="bull-score">{{ report.short_term_view?.score?.toFixed(0) ?? '—' }} / 100</div>
            <ul v-if="report.short_term_view?.signals?.length" class="bull-signals">
              <li v-for="(s, i) in report.short_term_view.signals" :key="i">{{ s }}</li>
            </ul>
          </div>
          <!-- 中长期 -->
          <div class="bull-card">
            <div class="bull-label">中长期</div>
            <div class="bull-view" :class="bullBearTone(report.long_term_view?.view)">
              {{ report.long_term_view?.view ?? '—' }}
            </div>
            <div class="bull-score">{{ report.long_term_view?.score?.toFixed(0) ?? '—' }} / 100</div>
            <ul v-if="report.long_term_view?.signals?.length" class="bull-signals">
              <li v-for="(s, i) in report.long_term_view.signals" :key="i">{{ s }}</li>
            </ul>
          </div>
        </div>
      </div>

      <div v-if="report.compliance_veto" class="veto-banner compliance" role="alert">
        <div class="veto-title">生存级重大风险 · 一票否决</div>
        <div class="veto-msg">
          {{ report.major_risks?.message || '命中生存级重大风险事件，财务打分不适用；请优先关注合规与生存风险' }}
        </div>
        <ul v-if="majorRiskEvents.length" class="veto-events">
          <li v-for="(ev, i) in majorRiskEvents" :key="i">
            <span class="ev-label">{{ ev.label }}</span>
            <span v-if="ev.notice_date" class="ev-date">{{ ev.notice_date }}</span>
            <span class="ev-title">{{ ev.title }}</span>
          </li>
        </ul>
      </div>

      <div
        v-else-if="observeRiskEvents.length"
        class="veto-banner observe"
        role="status"
      >
        <div class="veto-title">观察级风险 · 扣分但不否决</div>
        <div class="veto-msg">
          {{ report.major_risks?.observe_message || '警惕情绪杀跌；大股东质押等≠公司生存危机' }}
        </div>
        <ul class="veto-events">
          <li v-for="(ev, i) in observeRiskEvents" :key="i">
            <span class="ev-label">{{ ev.label }}</span>
            <span v-if="ev.notice_date" class="ev-date">{{ ev.notice_date }}</span>
            <span class="ev-title">{{ ev.title }}</span>
          </li>
        </ul>
      </div>

      <div v-if="report.cashflow_veto" class="veto-banner" role="alert">
        现金流不合格，暂不具备价值投资条件
      </div>

      <div v-if="displayWarnings.length" class="warnings">
        <div class="warn-title">风险提示</div>
        <ul>
          <li v-for="(w, i) in displayWarnings" :key="i">{{ w }}</li>
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
            <span
              v-if="report.modules[key].metadata?.insufficient_sample || report.modules[key].level === 'N/A'"
              class="module-score na"
            >
              N/A（样本不足）
            </span>
            <span v-else class="module-score" :class="scoreTone(report.modules[key].score)">
              {{ report.modules[key].score }} · {{ report.modules[key].level }}
            </span>
          </div>
          <ul v-if="report.modules[key].indicators?.length" class="indicators">
            <li v-for="ind in report.modules[key].indicators" :key="ind.name">
              <span class="ind-name">
                {{ ind.name }}
                <span v-if="ind.period" class="ind-period">{{ ind.period }}</span>
              </span>
              <span class="ind-val">{{ ind.value }}</span>
              <span class="ind-score" :class="scoreTone(ind.score)">{{ ind.score.toFixed(0) }}</span>
              <span v-if="ind.comment" class="ind-comment">{{ ind.comment }}</span>
            </li>
          </ul>
        </section>
      </div>

      <section v-if="report.valuation?.relative" class="valuation card-inner">
        <h4>
          相对估值
          <span
            v-if="report.valuation.composite_valuation_score != null"
            class="val-score"
            :class="scoreTone(report.valuation.composite_valuation_score)"
          >
            合理性 {{ report.valuation.composite_valuation_score.toFixed(0) }} 分
          </span>
        </h4>
        <p v-if="report.valuation.valuation_rationale" class="val-rationale">
          {{ report.valuation.valuation_rationale }}
        </p>
        <p v-if="report.valuation.value_trap_veto" class="val-trap" role="alert">
          {{ report.valuation.value_trap_message || '基本面恶化，低估值为陷阱，不适用相对估值' }}
        </p>
        <div class="val-grid">
          <div v-for="(v, k) in report.valuation.relative" :key="k" class="val-item">
            <span class="val-key">{{ k }}</span>
            <span
              class="val-signal"
              :class="
                v.signal === '低估' ? 'good' : v.signal === '高估' || v.signal === '偏贵' ? 'bad' : 'mid'
              "
            >
              {{ v.signal || '—' }}
            </span>
            <span v-if="k === 'PEG' && (v.value != null || v.current != null)" class="val-num">
              {{ v.value ?? v.current }}
            </span>
            <span v-else-if="v.current != null && k !== 'PEG'" class="val-num">{{ v.current }}</span>
            <span v-if="k === 'PEG' && v.growth_label" class="val-sub">
              基于{{ v.growth_label }}
              <template v-if="v.growth_rate != null"> {{ v.growth_rate }}%</template>
            </span>
            <span v-if="k === 'PEG' && v.thresholds" class="val-sub">{{ v.thresholds }}</span>
            <span v-if="v.percentile_5y != null" class="val-sub">分位 {{ v.percentile_5y }}%</span>
            <span v-else-if="v.percentile_na" class="val-sub na">{{ v.percentile_na }}</span>
            <span v-if="v.industry_median != null" class="val-sub">行业 {{ v.industry_median }}</span>
            <span v-if="v.note" class="val-sub">{{ v.note }}</span>
          </div>
        </div>
      </section>

      <section v-if="report.valuation?.comps" class="valuation card-inner">
        <h4>可比公司估值</h4>
        <p v-if="report.valuation.comps.warning" class="muted">{{ report.valuation.comps.warning }}</p>
        <div class="comps-summary">
          <div>
            <span class="k">可比均 PE</span>
            <strong>{{ report.valuation.comps.avg_pe ?? '—' }}</strong>
          </div>
          <div>
            <span class="k">可比均 PB</span>
            <strong>{{ report.valuation.comps.avg_pb ?? '—' }}</strong>
          </div>
          <div v-if="report.valuation.comps.signal">
            <span class="k">相对现价</span>
            <strong
              :class="
                report.valuation.comps.signal === '低估'
                  ? 'good'
                  : report.valuation.comps.signal === '高估'
                    ? 'bad'
                    : 'mid'
              "
            >{{ report.valuation.comps.signal }}</strong>
          </div>
        </div>
        <div v-if="report.valuation.comps.valuation_range?.pe_based" class="comps-range">
          PE 套算：
          {{ report.valuation.comps.valuation_range.pe_based.low }}
          –
          {{ report.valuation.comps.valuation_range.pe_based.high }}
          <span v-if="report.valuation.comps.valuation_range.pe_based.mid != null" class="muted">
            （中枢 {{ report.valuation.comps.valuation_range.pe_based.mid }}）
          </span>
        </div>
        <div v-if="report.valuation.comps.valuation_range?.pb_based" class="comps-range">
          PB 套算：
          {{ report.valuation.comps.valuation_range.pb_based.low }}
          –
          {{ report.valuation.comps.valuation_range.pb_based.high }}
          <span v-if="report.valuation.comps.valuation_range.pb_based.mid != null" class="muted">
            （中枢 {{ report.valuation.comps.valuation_range.pb_based.mid }}）
          </span>
        </div>
        <table v-if="report.valuation.comps.comparables?.length" class="comps-table">
          <thead>
            <tr>
              <th>可比公司</th>
              <th>代码</th>
              <th>PE</th>
              <th>PB</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in report.valuation.comps.comparables" :key="c.symbol">
              <td>{{ c.name || '—' }}</td>
              <td class="code">{{ c.symbol.split('.')[0] }}</td>
              <td>{{ c.pe ?? '—' }}</td>
              <td>{{ c.pb ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section v-if="report.valuation?.dcf" class="valuation card-inner">
        <h4>
          DCF 内在价值
          <span class="muted" style="font-weight:500;font-size:12px">
            {{ report.valuation.dcf.suppressed || report.valuation.dcf.role === 'not_applicable_dividend' ? '红利资产不适用' : '保守参考' }}
          </span>
        </h4>
        <p v-if="report.valuation.dcf.note" class="muted">{{ report.valuation.dcf.note }}</p>
        <template
          v-if="
            report.valuation.dcf.intrinsic_value_per_share != null &&
            !report.valuation.dcf.suppressed
          "
        >
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

/* ── 五维雷达 + 多空 ─────────────────────────────── */
.radar-block {
  display: flex; gap: 20px; align-items: flex-start;
  padding: 14px 12px;
  border: 1px solid var(--border-color); border-radius: 10px;
  margin-bottom: var(--space-md);
  background: var(--bg-secondary, #fafafa);
}
.radar-wrap { flex: 0 0 auto; display: flex; justify-content: center; align-items: center; }
.radar-svg { width: 240px; height: 240px; display: block; }
.bull-bear-wrap { flex: 1; display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.bull-card {
  background: white; border: 1px solid var(--border-color); border-radius: 8px;
  padding: 10px 14px;
}
[data-theme='dark'] .bull-card { background: var(--bg-secondary, #1a1a1a); }
.bull-label { font-size: 12px; color: var(--text-secondary); font-weight: 600; }
.bull-view { font-size: 20px; font-weight: 700; margin: 2px 0; }
.bull-view.good { color: #389e0d; }
.bull-view.mid { color: #d48806; }
.bull-view.bad { color: #cf1322; }
.bull-score { font-size: 11px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.bull-signals { margin: 6px 0 0; padding-left: 16px; font-size: 12px; color: var(--text-secondary); line-height: 1.5; }
.bull-signals li { margin: 2px 0; }

@media (max-width: 720px) {
  .radar-block { flex-direction: column; align-items: center; }
  .bull-bear-wrap { width: 100%; }
}
.veto-banner {
  background: #fff1f0;
  border: 1px solid #ffa39e;
  color: #cf1322;
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: var(--space-md);
  font-size: 14px;
  font-weight: 600;
}
.veto-banner.compliance {
  font-weight: 500;
}
.veto-banner.observe {
  background: #fffbe6;
  border-color: #ffe58f;
  color: #ad6800;
  font-weight: 500;
}
.veto-banner.observe .ev-date,
.veto-banner.observe .ev-title {
  color: #876800;
}
[data-theme='dark'] .veto-banner.observe {
  background: #2b2111;
  border-color: #ad6800;
  color: #ffc069;
}
[data-theme='dark'] .veto-banner.observe .ev-date,
[data-theme='dark'] .veto-banner.observe .ev-title {
  color: #ffe7ba;
}
.veto-banner .veto-title {
  font-weight: 700;
  font-size: 15px;
  margin-bottom: 4px;
}
.veto-banner .veto-msg {
  margin-bottom: 8px;
}
.veto-banner .veto-events {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  font-weight: 400;
  line-height: 1.55;
}
.veto-banner .ev-label {
  font-weight: 600;
  margin-right: 6px;
}
.veto-banner .ev-date {
  color: #a8071a;
  margin-right: 6px;
  white-space: nowrap;
}
.veto-banner .ev-title {
  color: #5c0011;
}
[data-theme='dark'] .veto-banner {
  background: #2a1215;
  border-color: #a8071a;
  color: #ff7875;
}
[data-theme='dark'] .veto-banner .ev-date,
[data-theme='dark'] .veto-banner .ev-title {
  color: #ffccc7;
}
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
.module-score.na, .na { color: #8c8c8c; font-weight: 600; }
.indicators { list-style: none; margin: 0; padding: 0; font-size: 13px; }
.indicators li {
  display: grid; grid-template-columns: 1fr auto auto; gap: 8px;
  padding: 4px 0; border-top: 1px dashed var(--border-color);
}
.ind-name { color: var(--text-secondary); display: flex; flex-direction: column; gap: 2px; }
.ind-period { font-size: 11px; color: var(--text-secondary); opacity: 0.85; font-weight: 400; }
.ind-val { font-variant-numeric: tabular-nums; }
.ind-score { font-weight: 600; min-width: 28px; text-align: right; }
.ind-score.good { color: #389e0d; }
.ind-score.mid { color: #d48806; }
.ind-score.bad { color: #cf1322; }
.ind-comment { grid-column: 1 / -1; font-size: 12px; color: var(--text-secondary); }
.valuation { margin-top: 10px; padding: 10px 0; border-top: 1px solid var(--border-color); }
.valuation h4 { margin: 0 0 8px; font-size: 14px; display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.val-score { font-size: 13px; font-weight: 600; }
.val-score.good { color: #389e0d; }
.val-score.mid { color: #d48806; }
.val-score.bad { color: #cf1322; }
.val-rationale {
  font-size: 12px; color: var(--text-secondary); margin: 0 0 10px; line-height: 1.5;
  padding: 8px 10px; background: var(--bg-secondary, #fafafa); border-radius: 6px;
}
.val-trap {
  font-size: 13px; font-weight: 600; color: #cf1322; margin: 0 0 10px; line-height: 1.5;
  padding: 8px 10px; background: #fff1f0; border: 1px solid #ffa39e; border-radius: 6px;
}
[data-theme='dark'] .val-trap {
  background: #2a1215; border-color: #a8071a; color: #ff7875;
}
.val-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px; }
.val-item { font-size: 13px; padding: 8px; background: var(--bg-secondary, #fafafa); border-radius: 6px; }
.val-key { display: block; font-weight: 600; }
.val-signal.good { color: #389e0d; }
.val-signal.bad { color: #cf1322; }
.val-num { display: block; margin-top: 2px; }
.val-sub { font-size: 12px; color: var(--text-secondary); }
.comps-summary {
  display: flex; flex-wrap: wrap; gap: 12px 20px; margin-bottom: 8px; font-size: 13px;
}
.comps-summary .k { color: var(--text-secondary); margin-right: 6px; }
.comps-summary .good { color: #cf1322; }
.comps-summary .bad { color: #389e0d; }
.comps-summary .mid { color: #d48806; }
.comps-range { font-size: 13px; margin: 4px 0; }
.comps-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }
.comps-table th, .comps-table td {
  text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border-color);
}
.comps-table .code { font-variant-numeric: tabular-nums; color: var(--text-secondary); }
.summary {
  margin-top: var(--space-md); padding: 10px; background: var(--bg-secondary, #fafafa);
  border-radius: 8px; font-size: 12px; white-space: pre-wrap; color: var(--text-secondary);
}
</style>
