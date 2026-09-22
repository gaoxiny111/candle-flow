<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { fetchTechNarrative, type TechNarrative } from '@/api'

const props = defineProps<{ symbol: string }>()

const report = ref<TechNarrative | null>(null)
const loading = ref(false)
const error = ref('')
const open = ref(true)

/** 近 5 个交易日主力资金明细（最新在前）。 */
const ffDays = computed(() => {
  const series = report.value?.fund_flow?.main?.series ?? []
  return series.slice(-5).reverse()
})

async function load(sym: string) {
  if (!sym) return
  loading.value = true
  error.value = ''
  try {
    const { data } = await fetchTechNarrative(sym)
    report.value = data.data ?? null
    if (data.code !== 200) {
      error.value = data.message || '技术面分析生成失败'
      report.value = null
    }
  } catch (e) {
    report.value = null
    error.value = e instanceof Error ? e.message : '技术面分析生成失败'
  } finally {
    loading.value = false
  }
}

function pct(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return '—'
  const n = Number(v)
  return `${n > 0 ? '+' : ''}${(n * 100).toFixed(2)}%`
}

function px(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return '—'
  return Number(v).toFixed(2)
}

/** 元 → 万元/亿元（带符号），资金面专用。 */
function money(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return '—'
  const n = Number(v)
  const sign = n < 0 ? '-' : '+'
  const a = Math.abs(n)
  if (a >= 1e8) return `${sign}${(a / 1e8).toFixed(2)}亿`
  if (a >= 1e4) return `${sign}${(a / 1e4).toFixed(0)}万`
  return `${sign}${a.toFixed(0)}`
}

function dateShort(d: string): string {
  return d ? d.slice(5).replace('-', '/') : ''
}

watch(() => props.symbol, (s) => load(s), { immediate: true })
</script>

<template>
  <div class="tech-narrative card">
    <div class="tn-header" @click="open = !open">
      <h3>技术面分析（系统口径）</h3>
      <span v-if="report" class="tn-asof">截至 {{ report.as_of }} · 收盘 {{ px(report.close) }}</span>
      <span v-if="report?.name" class="tn-name">{{ report.name }}</span>
      <span class="tn-toggle">{{ open ? '收起 ▲' : '展开 ▼' }}</span>
    </div>

    <div v-if="loading" class="tn-loading">技术面分析生成中...</div>
    <div v-else-if="error" class="tn-error">{{ error }}</div>

    <div v-else-if="open && report" class="tn-body">
      <!-- 一、趋势与形态 -->
      <section>
        <h4>一、趋势与形态</h4>
        <ul class="tn-list">
          <li>
            <b>{{ report.trend.align }}</b>（MA5 {{ px(report.trend.ma5) }} / MA10
            {{ px(report.trend.ma10) }} / MA20 {{ px(report.trend.ma20) }}<template v-if="report.trend.ma60">
              / MA60 {{ px(report.trend.ma60) }}</template
            >），收盘 {{ px(report.trend.close) }}
            <template v-if="report.trend.chg5 !== null">；近 5 日 {{ pct(report.trend.chg5) }}</template>
            <template v-if="report.trend.chg20 !== null">，近 20 日 {{ pct(report.trend.chg20) }}</template
            >。{{ report.trend.summary }}
          </li>
          <li v-if="report.patterns.length">
            近期 K 线形态：
            <span
              v-for="(p, i) in report.patterns.slice(0, 6)"
              :key="p.date + p.name"
              :class="['pattern-chip', p.direction]"
            >
              {{ dateShort(p.date) }} {{ p.direction === 'bullish' ? '看涨' : '看跌' }}·{{ p.name }}({{ p.score }}分)<template v-if="i < Math.min(report.patterns.length, 6) - 1">、</template>
            </span>
          </li>
          <li v-else>近 10 日无 ≥60 分的 K 线形态信号。</li>
          <li v-if="report.guard_note" class="warn">形态提示：{{ report.guard_note }}</li>
        </ul>
      </section>

      <!-- 二、技术指标 -->
      <section>
        <h4>二、技术指标</h4>
        <ul class="tn-list">
          <li v-if="report.indicators.macd?.available">
            <b>MACD</b>：{{ report.indicators.macd.state
            }}<template v-if="report.indicators.macd.cross">，{{ report.indicators.macd.cross }}</template
            >；DIF {{ report.indicators.macd.dif.toFixed(3) }} / DEA
            {{ report.indicators.macd.dea.toFixed(3) }} / 柱
            {{ report.indicators.macd.hist.toFixed(3) }}，{{ report.indicators.macd.bar_note }}
          </li>
          <li v-if="report.indicators.rsi_text"><b>RSI</b>：{{ report.indicators.rsi_text }}</li>
          <li v-if="report.indicators.stoch">
            <b>KDJ</b>：%K={{ report.indicators.stoch.k.toFixed(1) }}，%D={{
              report.indicators.stoch.d.toFixed(1)
            }}，{{ report.indicators.stoch.k > report.indicators.stoch.d ? 'K 在 D 上方（偏多）' : 'K 在 D 下方（偏空）' }}
          </li>
          <li v-if="report.indicators.boll">
            <b>布林</b>：价格{{ report.indicators.boll.position }}（下轨 {{ px(report.indicators.boll.lower) }} / 中轨
            {{ px(report.indicators.boll.mid) }} / 上轨 {{ px(report.indicators.boll.upper) }}）
          </li>
          <li v-if="report.indicators.volume?.available">
            <b>量能</b>：近 5 日均量为前 20 日的
            {{ report.indicators.volume.ratio.toFixed(2) }} 倍（{{ report.indicators.volume.label }}）
          </li>
        </ul>
      </section>

      <!-- 三、支撑与压力 -->
      <section>
        <h4>三、关键支撑与压力</h4>
        <table class="tn-table">
          <thead>
            <tr><th>类型</th><th>价位</th><th>来源</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in report.levels.resistances" :key="'r' + r.price">
              <td class="resist">压力</td>
              <td class="num">{{ px(r.price) }}</td>
              <td>{{ r.source }}</td>
              <td class="muted">{{ r.note }}</td>
            </tr>
            <tr v-for="s in report.levels.supports" :key="'s' + s.price">
              <td class="support">支撑</td>
              <td class="num">{{ px(s.price) }}</td>
              <td>{{ s.source }}</td>
              <td class="muted">{{ s.note }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <!-- 四、资金面 -->
      <section v-if="report.fund_flow">
        <h4>四、资金面</h4>
        <ul class="tn-list">
          <li v-if="report.fund_flow.texts.main">{{ report.fund_flow.texts.main }}</li>
          <li v-else class="muted-line">主力资金数据源暂不可用。</li>
          <li v-if="report.fund_flow.texts.margin">{{ report.fund_flow.texts.margin }}</li>
          <li v-if="report.fund_flow.texts.lhb">{{ report.fund_flow.texts.lhb }}</li>
          <li v-if="report.fund_flow.texts.stance" class="ff-stance">
            资金面倾向：{{ report.fund_flow.texts.stance }}
          </li>
        </ul>
        <table v-if="ffDays.length" class="tn-table ff-table">
          <thead>
            <tr>
              <th>日期</th>
              <th>主力净额</th>
              <th>净占比</th>
              <th>超大单</th>
              <th>大单</th>
              <th>收盘</th>
              <th>涨跌</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="d in ffDays" :key="d.date">
              <td>{{ dateShort(d.date) }}</td>
              <td class="num" :class="(d.main ?? 0) < 0 ? 'out' : 'in'">{{ money(d.main) }}</td>
              <td class="num" :class="(d.main_ratio ?? 0) < 0 ? 'out' : 'in'">
                {{ d.main_ratio === null ? '—' : d.main_ratio.toFixed(2) + '%' }}
              </td>
              <td class="num" :class="(d.xlarge ?? 0) < 0 ? 'out' : 'in'">{{ money(d.xlarge) }}</td>
              <td class="num" :class="(d.large ?? 0) < 0 ? 'out' : 'in'">{{ money(d.large) }}</td>
              <td class="num">{{ px(d.close) }}</td>
              <td class="num" :class="(d.chg ?? 0) < 0 ? 'out' : 'in'">
                {{ d.chg === null ? '—' : (d.chg > 0 ? '+' : '') + d.chg.toFixed(2) + '%' }}
              </td>
            </tr>
          </tbody>
        </table>
        <p class="ff-legend">
          主力=超大单+大单（东方财富成交结构口径，非同花顺 DDX，属成交统计而非持仓变动）；融资融券为交易所披露（T+1）；
          北向资金个股日频持股自 2024-08 起停止披露，故不含北向。资金面数据仅作展示，不参与打分与筛选。
        </p>
      </section>

      <!-- 五、综合判断 -->
      <section>
        <h4>五、综合判断与操作建议</h4>
        <p><b>短期：</b>{{ report.verdict.short_term }}</p>
        <p><b>中期：</b>{{ report.verdict.mid_term }}</p>
        <ul class="tn-list">
          <li v-for="(a, i) in report.verdict.actions" :key="i">{{ a }}</li>
        </ul>
      </section>

      <p class="tn-note">{{ report.note }}</p>
    </div>
  </div>
</template>

<style scoped>
.tech-narrative { padding: var(--space-md); }
.tn-header { display: flex; align-items: baseline; gap: var(--space-sm); cursor: pointer; flex-wrap: wrap; }
.tn-header h3 { margin: 0; font-size: 15px; }
.tn-asof { font-size: 12px; color: var(--text-secondary); }
.tn-name { font-size: 13px; font-weight: 600; color: var(--color-primary); }
.tn-toggle { margin-left: auto; font-size: 12px; color: var(--text-secondary); cursor: pointer; }
.tn-loading, .tn-error { padding: var(--space-md) 0; font-size: 13px; color: var(--text-secondary); }
.tn-error { color: #f5222d; }
.tn-body { display: flex; flex-direction: column; gap: var(--space-md); margin-top: var(--space-sm); }
section h4 { margin: 0 0 6px; font-size: 14px; color: var(--text-primary); }
.tn-list { margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 4px; }
.tn-list li { font-size: 13px; line-height: 1.7; color: var(--text-primary); }
.tn-list li.warn { color: #d48806; }
.pattern-chip.bullish { color: #f5222d; }
.pattern-chip.bearish { color: #52c41a; }
.tn-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.tn-table th, .tn-table td { border-bottom: 1px solid var(--border-color); padding: 5px 8px; text-align: left; }
.tn-table th { color: var(--text-secondary); font-weight: 500; font-size: 12px; }
.tn-table td.num { font-variant-numeric: tabular-nums; font-weight: 600; }
.tn-table td.resist { color: #f5222d; }
.tn-table td.support { color: #52c41a; }
.tn-table td.muted { color: var(--text-secondary); }
.tn-body p { margin: 0; font-size: 13px; line-height: 1.7; }
.tn-list li.muted-line { color: var(--text-secondary); }
.tn-list li.ff-stance { color: var(--color-primary); }
.ff-table { margin-top: 6px; }
.ff-table td.in { color: #f5222d; }
.ff-table td.out { color: #52c41a; }
.ff-legend { margin-top: 6px; font-size: 12px; line-height: 1.6; color: var(--text-secondary); }
.tn-note { font-size: 12px; color: var(--text-secondary); border-top: 1px dashed var(--border-color); padding-top: 8px; }
</style>
