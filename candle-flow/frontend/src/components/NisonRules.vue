<script setup lang="ts">
const rules = [
  { title: '先看趋势', text: '先看周线波段定方向，再用日线找入场。周线向下不做多，周线向上不做空；箱体/横盘周线不据此否决。反转形态必须出现在一段明确的上涨或下跌之后。箱体中的十字线不是交易信号。' },
  { title: '等待确认', text: '锤子、上吊、倒锤、流星、孕线、平头、吞没、刺透/乌云需下一根收盘确认才可当作交易信号。最后一根尚未确认的形态只作观察，不生成买点。' },
  { title: '收盘价过滤', text: '支撑/阻力只有被收盘价击穿才算失守，盘中刺破不算突破。窗口也只有收盘填补才失效。' },
  { title: '形态即止损', text: '看涨形态止损放在形态最低价之下；看跌形态止损放在形态最高价之上。确认信号后按此跟踪持仓。' },
  { title: '测算目标价', text: '蜡烛图本身不给出目标价（第十六章）。用西方测幅：箱体突破后叠加箱体高度；对等运动/旗形/尖旗形把第一波（旗杆）叠加到调整近端（尼森偏保守，多头从旗底起算）；上升/下降三法按旗形测幅。目标用于减仓平仓，不单独据此开反向仓；达不到目标也可能提前反转。无清晰结构时退回 2R/3R 风控。' },
  { title: '盈亏比先行', text: '没有想清楚风险回报比，不要用蜡烛图开仓。系统默认要求盈亏比至少 1.5。可用回测页检查历史 R。' },
  { title: '信号汇聚', text: '蜡烛形态才是入场触发。均线金叉/死叉、布林、MACD、RSI、成交量、高低点、窗口或周线是西方确认工具，须至少两项同向；金叉本身不能当买点。同一天多种形态互证加分；互相打架则不做。' },
  { title: '窗口即区域', text: '上升窗口整段都是支撑（关键在窗口下沿）；下降窗口整段都是阻力（关键在窗口上沿）。回测有效，可单独给出升窗/降窗回测信号；仅收盘填补缺口才失效。' },
  { title: '勿追红三兵', text: '三根大阳收在高位往往已超买，应等回调到支撑再考虑买入。前进受阻、停顿形态视为上涨乏力。' },
]
</script>

<template>
  <section class="card nison-rules">
    <h2>尼森交易原则</h2>
    <p class="lead">依据史蒂夫·尼森《日本蜡烛图教程》：蜡烛图是工具，不是系统。形态给出方向与风险位置；目标价用第十六章西方测幅。</p>
    <ol>
      <li v-for="r in rules" :key="r.title">
        <strong>{{ r.title }}</strong>
        <span>{{ r.text }}</span>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.nison-rules h2 { margin-bottom: var(--space-sm); }
.lead { color: var(--text-secondary); font-size: 13px; margin-bottom: var(--space-md); line-height: 1.6; }
ol { margin: 0; padding-left: 1.2rem; display: flex; flex-direction: column; gap: var(--space-sm); }
li { font-size: 13px; line-height: 1.55; }
li strong { display: inline-block; min-width: 6.5em; margin-right: 6px; }
li span { color: var(--text-secondary); }
</style>
