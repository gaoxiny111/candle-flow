<script setup lang="ts">
const rules = [
  { title: '先看趋势', text: '先看周线波段定方向，再用日线找入场。周线向下不做多，周线向上不做空；箱体/横盘周线不据此否决。反转形态必须出现在一段明确的上涨或下跌之后。箱体中的十字线不是交易信号。' },
  { title: '等待确认', text: '锤子、上吊、倒锤、流星、孕线、平头、吞没、刺透/乌云需下一根收盘确认才可当作交易信号。最后一根尚未确认的形态只作观察，不生成买点。' },
  { title: '收盘价过滤', text: '支撑/阻力只有被收盘价击穿才算失守，盘中刺破不算突破。窗口也只有收盘填补才失效。' },
  { title: '形态即止损', text: '看涨形态止损放在形态最低价之下；看跌形态止损放在形态最高价之上。确认信号后按此跟踪持仓。' },
  { title: '信号汇聚', text: '第十章：蜡烛形态才是入场触发。均线、布林、MACD、RSI、随机、背离、成交量、高低点、窗口、趋势线、极性、回撤、周线是西方确认工具，须至少两项同向；金叉本身不能当买点。同日多种形态互证加分；互相打架则不做。' },
  { title: '趋势与极性', text: '第十一章：破低反涨/破高反跌可作触发；极性转换（原支撑跌破后变阻力，原阻力突破后变支撑）与上升/下降趋势线贴近可作为汇聚。' },
  { title: '百分比回撤', text: '第十二章：升浪后的 38.2%/50%/61.8% 回撤作支撑；降浪后的同比例反弹作阻力。价格贴近回撤位且蜡烛同向时计入汇聚；主图可开关「回撤位」。' },
  { title: '移动平均线', text: '第十三章：MA5/10/20 支撑阻力与金叉/死叉只作确认，不当单独买点；以收盘价判定站上/跌破。' },
  { title: '摆动指数', text: '第十四章：MACD、RSI、随机指标（Stoch）作超买超卖与动能确认；价格与 RSI/MACD 的背离计入汇聚。' },
  { title: '交易量', text: '第十五章：突破/反转宜放量确认；回调缩量后再放量收阳（或反弹缩量后再放量收阴）视为量价配合。' },
  { title: '测算目标价', text: '第十六章：蜡烛图本身不给目标价。用箱体突破、对等运动、旗形/三法测幅（尼森偏保守）；目标用于减仓平仓。无清晰结构时退回 2R/3R。' },
  { title: '盈亏比先行', text: '没有想清楚风险回报比，不要用蜡烛图开仓。系统默认要求盈亏比至少 1.5。可用回测页检查历史 R。' },
  { title: '窗口即区域', text: '上升窗口整段都是支撑（关键在窗口下沿）；下降窗口整段都是阻力（关键在窗口上沿）。回测有效；仅收盘填补缺口才失效。' },
  { title: '勿追红三兵', text: '三根大阳收在高位往往已超买，应等回调到支撑再考虑买入。前进受阻、停顿形态视为上涨乏力。' },
]
</script>

<template>
  <section class="card nison-rules">
    <h2>尼森交易原则</h2>
    <p class="lead">依据史蒂夫·尼森《日本蜡烛图教程》第十～十六章：蜡烛给方向与风险，西方工具做汇聚与测幅。</p>
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
