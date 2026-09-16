"""中国神华五维分析雷达图 + 短期/中长期多空判断 (修复中文字体)"""
import sys
sys.path.insert(0, '.')
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

# ── 中文字体修复 ──────────────────────────────────────────────
import platform
system = platform.system()
if system == 'Windows':
    plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'Microsoft JhengHei']
elif system == 'Darwin':
    plt.rcParams['font.sans-serif'] = ['Arial Unicode MS', 'PingFang SC']
else:
    plt.rcParams['font.sans-serif'] = ['Noto Sans CJK SC', 'WenQuanYi Micro Hei', 'SimHei']
plt.rcParams['axes.unicode_minus'] = False

# ── 真实引擎得分 ──────────────────────────────────────────────
from app.analysis.engine import FundamentalEngine

r = FundamentalEngine().run_full_analysis('601088.SH', db=None)
mods = r['modules']

dims = {
    '盈利能力': mods['profitability']['score'],
    '成长性':   mods['growth']['score'],
    '现金流质量': mods['cashflow']['score'],
    '偿债能力': mods['solvency']['score'],
    '估值合理性': r['valuation'].get('composite_valuation_score') or r['valuation'].get('score'),
}

labels = list(dims.keys())
scores = [dims[l] for l in labels]

# ── 多空判断 ──────────────────────────────────────────────────
composite = r['composite_score']
ddm = r['valuation'].get('ddm', {})
mid_scenario = next((s for s in ddm.get('scenarios', []) if s['name'] == '中性'), None)
mos = mid_scenario['margin_of_safety_pct'] if mid_scenario else 0
price = r['market'].get('price', 47.1)
pe = r['market'].get('pe_ttm', 18.9)
dy = r['market'].get('dividend_yield', 4.3)
pb = r['market'].get('pb', 2.27)
growth_score = mods['growth']['score']
profit_score = mods['profitability']['score']
cf_score = mods['cashflow']['score']

# 短期：DDM安全边际+成长边际修复+股息率支撑
short_signals = []
short_score = 50
if mos >= 0:
    short_score += 15; short_signals.append(f"DDM中性安全边际+{mos:.0f}%")
else:
    short_score -= 10; short_signals.append(f"DDM中性安全边际{mos:.0f}%")
if dy >= 4:
    short_score += 10; short_signals.append(f"股息率{dy:.1f}%有支撑")
if growth_score >= 75:
    short_score += 10; short_signals.append("Q2修复超预期")
if cf_score >= 80:
    short_score += 8;  short_signals.append("现金流健康")
if pe >= 18:
    short_score -= 10; short_signals.append(f"PE {pe:.1f}偏高压制")
short_score = max(0, min(100, short_score))

# 中长期：分红承诺+资产注入外延+ROIC/WACC+红利属性
long_score = 50
long_signals = []
if profit_score >= 80: long_score += 15; long_signals.append(f"ROE 12.8%稳健")
if mods['solvency']['score'] >= 85: long_score += 10; long_signals.append("偿债极强有息仅6%")
long_score += 12; long_signals.append("分红承诺≥65%")
long_score += 10; long_signals.append("资产注入规模跃升")
long_score += 8; long_signals.append("红利龙头底仓")
long_score = min(long_score, 100)

def bull_bear(score):
    if score >= 70: return "看多"
    if score >= 45: return "中性"
    if score >= 30: return "偏空"
    return "看空"

short_view = bull_bear(short_score)
long_view = bull_bear(long_score)

print(f"综合评分: {composite} ({r['final_rating_letter']})")
for l, s in zip(labels, scores):
    print(f"  {l}: {s}")
print(f"短期: {short_score} → {short_view}  ({', '.join(short_signals)})")
print(f"中长期: {long_score} → {long_view}  ({', '.join(long_signals)})")

# ── 绘制 ───────────────────────────────────────────────────────
fig = plt.figure(figsize=(16, 11), facecolor='#fafafa')
ax = fig.add_subplot(111, projection='polar')

N = len(labels)
angles = np.linspace(0, 2 * np.pi, N, endpoint=False).tolist()
angles += angles[:1]
scores_plot = scores + scores[:1]

# 填充
ax.fill(angles, scores_plot, color='#ff6b6b', alpha=0.35)
ax.plot(angles, scores_plot, color='#e74c3c', linewidth=2.5, marker='o', markersize=9, markerfacecolor='white', markeredgewidth=2)

ax.set_ylim(0, 100)
ax.set_yticks([20, 40, 60, 80, 100])
ax.set_yticklabels(['20', '40', '60', '80', '100'], fontsize=9, color='#999')
ax.yaxis.grid(True, linestyle='--', color='#e0e0e0', linewidth=0.8)
ax.xaxis.grid(True, linestyle='--', color='#e0e0e0', linewidth=0.8)

ax.set_xticks(angles[:-1])
ax.set_xticklabels(labels, fontsize=15, fontweight='bold', color='#333')

# 顶点分数标注
for angle, label, s in zip(angles[:-1], labels, scores):
    ax.annotate(f'{s}', xy=(angle, s), xytext=(0, 12), textcoords='offset points',
                ha='center', va='bottom', fontsize=12, fontweight='bold', color='#e74c3c',
                bbox=dict(boxstyle='round,pad=0.25', facecolor='white', edgecolor='#e74c3c', alpha=0.9, linewidth=1.5))

# ── 右侧信息面板 ────────────────────────────────────────────────
# 标题
fig.text(0.87, 0.82, '中国神华  601088.SH', fontsize=17, fontweight='bold', color='#222', ha='center')
fig.text(0.87, 0.755, f'综合评分  {composite}', fontsize=26, fontweight='bold', color='#e74c3c', ha='center')
fig.text(0.87, 0.695, f'评级  {r["final_rating_letter"]}', fontsize=14, color='#666', ha='center')

# 短期
short_color = '#27ae60' if short_score >= 60 else ('#e67e22' if short_score >= 40 else '#c0392b')
fig.text(0.87, 0.61, '短 期 观 点', fontsize=13, fontweight='bold', color='#555', ha='center')
fig.text(0.87, 0.54, short_view, fontsize=22, fontweight='bold', color=short_color, ha='center')
fig.text(0.87, 0.485, f'{short_score} / 100', fontsize=11, color='#999', ha='center')
short_text = '\n'.join(f'  {s}' for s in short_signals[:5])
fig.text(0.87, 0.37, short_text, fontsize=10, color='#555', ha='center', linespacing=1.6,
         bbox=dict(boxstyle='round,pad=0.4', facecolor='#f8f8f8', edgecolor='#ddd', alpha=0.9))

# 中长期
long_color = '#27ae60' if long_score >= 60 else ('#e67e22' if long_score >= 40 else '#c0392b')
fig.text(0.87, 0.295, '中 长 期 观 点', fontsize=13, fontweight='bold', color='#555', ha='center')
fig.text(0.87, 0.225, long_view, fontsize=22, fontweight='bold', color=long_color, ha='center')
fig.text(0.87, 0.17, f'{long_score} / 100', fontsize=11, color='#999', ha='center')
long_text = '\n'.join(f'  {s}' for s in long_signals[:5])
fig.text(0.87, 0.055, long_text, fontsize=10, color='#555', ha='center', linespacing=1.6,
         bbox=dict(boxstyle='round,pad=0.4', facecolor='#f8f8f8', edgecolor='#ddd', alpha=0.9))

# 底部行情快照
fig.text(0.5, 0.015,
         f'现价 {price}元   PE {pe:.1f}   PB {pb:.2f}   股息率 {dy:.1f}%   DDM中性 {mid_scenario["intrinsic_value_per_share"]}元',
         fontsize=10, color='#999', ha='center')

plt.tight_layout(rect=[0, 0.04, 0.76, 0.96])

out = r'd:\candle-flow(2)\candle-flow\candle-flow\backend\_shenhua_radar.png'
plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='#fafafa')
print(f'\n已保存: {out}')
