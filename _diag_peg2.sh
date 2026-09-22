#!/bin/bash
cd /opt/candle-flow/backend || exit 1
.venv/bin/python - <<'PY'
import json
from collections import Counter, defaultdict
from statistics import median
from sqlalchemy import text
from app.database import SessionLocal

db = SessionLocal()

print("########## A. PEG growth_label 分布（全量快照） ##########")
rows = db.execute(text(
    "SELECT symbol, json_extract(payload,'$.market.pe_ttm'), "
    "json_extract(payload,'$.valuation.relative.PEG.value'), "
    "json_extract(payload,'$.valuation.relative.PEG.growth_label'), "
    "json_extract(payload,'$.valuation.relative.PEG.growth_rate'), "
    "json_extract(payload,'$.valuation.valuation_score_breakdown'), "
    "json_extract(payload,'$.valuation.valuation_score_base'), "
    "json_extract(payload,'$.valuation.is_growth_stock'), "
    "json_extract(payload,'$.valuation.is_dividend_asset'), "
    "json_extract(payload,'$.composite_score'), "
    "json_extract(payload,'$.name'), "
    "json_extract(payload,'$.industry') "
    "FROM factor_snapshots"
)).fetchall()
print("快照总数 =", len(rows))

lab = Counter()
yoy_rows = []
peg_lt1 = []
for (sym, pe, peg, glab, grat, bd, vbase, isg, isd, cs, nm, ind) in rows:
    k = glab or "(None)"
    lab[k] += 1
    if glab and str(glab).startswith("最新净利同比"):
        yoy_rows.append((sym, pe, peg, glab, grat, bd, vbase, isg, isd, cs, nm, ind))
    if peg is not None and float(peg) < 1:
        has_anchor = "PEG核心锚" in (bd or "")
        peg_lt1.append((sym, pe, peg, glab, has_anchor, vbase, cs, nm, ind))

for k, v in lab.most_common():
    print(f"  {k:<28}{v:>6}  ({v/len(rows)*100:.1f}%)")

print()
print("########## B. 回退「最新净利同比」的样本（口径泄漏面） ##########")
print("  只数 =", len(yoy_rows), f"({len(yoy_rows)/len(rows)*100:.1f}%)")
print("  其中 PEG<1（会拿 80/90 分核心锚）= ",
      sum(1 for r in yoy_rows if r[2] is not None and float(r[2]) < 1))
print()
print("  ⚠ PEG<1 且拿核心锚的回退票（PE 降序前 25）：")
bad = [r for r in yoy_rows if r[2] is not None and float(r[2]) < 1]
print(f"  {'代码':<12}{'名称':<10}{'行业':<14}{'PE':>9}{'PEG':>7}{'增速':>9}{'估值基':>8}{'综合':>7}  锚")
for (sym, pe, peg, glab, grat, bd, vbase, isg, isd, cs, nm, ind) in sorted(bad, key=lambda x: -(x[1] or 0))[:25]:
    has = "PEG核心锚" in (bd or "")
    print(f"  {sym:<12}{str(nm)[:8]:<10}{str(ind)[:12]:<14}{(pe or 0):>9.1f}{(peg or 0):>7.2f}"
          f"{(grat or 0):>9.1f}{(vbase or 0):>8.1f}{(cs or 0):>7.1f}  {'有' if has else '-'}")

print()
print("########## C. PEG<1 总数 vs 有核心锚（一致性） ##########")
n_lt1 = len(peg_lt1)
n_anchor = sum(1 for r in peg_lt1 if r[4])
print(f"  PEG<1 的票 = {n_lt1}，其中 breakdown 含「PEG核心锚」= {n_anchor}")
la = Counter(r[3] or "(None)" for r in peg_lt1 if r[4])
print("  有核心锚的票按增速口径：")
for k, v in la.most_common():
    print(f"    {k:<28}{v:>6}")

print()
print("########## D. 目标票现状（.2 重建后） ##########")
SAMPLE = {
    "600722.SH": "金牛化工", "603407.SH": "长裕集团", "603995.SH": "甬金股份",
    "600309.SH": "万华化学", "603659.SH": "璞泰来", "001287.SZ": "中电港",
    "002371.SZ": "北方华创", "002705.SZ": "新宝股份", "600519.SH": "贵州茅台",
}
data = {r[0]: r for r in rows}
for sym, label in SAMPLE.items():
    r = data.get(sym)
    if not r:
        print(f"  {label:<10}{sym} 不在快照"); continue
    (sym2, pe, peg, glab, grat, bd, vbase, isg, isd, cs, nm, ind) = r
    try:
        items = [(b.get("factor"), float(b.get("points") or 0)) for b in json.loads(bd or "[]")]
    except Exception:
        items = []
    comp = "、".join(f"{f}{p:.0f}" for f, p in items) or "(空)"
    print(f"  {label:<10}{sym} PE={pe if pe else 0:>8.1f} 估值基={vbase} 综合={cs} "
          f"is_growth={isg} is_div={isd} PEG={peg}({glab})")
    print(f"  {'':<10}  构成：{comp}")

print()
print("########## E. 回退票里 3 年年报窗口是否可用（判定泄漏真伪） ##########")
from app.analysis.financials import build_financial_dataframe
CHECK = [r[0] for r in sorted(bad, key=lambda x: -(x[1] or 0))[:12]]
print(f"  {'代码':<12}{'名称':<10}{'年报点数':>8}{'3年CAGR':>10}{'全窗CAGR':>10}{'同比':>9}  判读")
for sym in CHECK:
    r = data.get(sym)
    nm = r[10]
    try:
        fin_df, meta = build_financial_dataframe(sym)
    except Exception as e:
        print(f"  {sym:<12}{str(nm)[:8]:<10}  取数失败: {e}")
        continue
    n = 0
    cagr3 = None
    cagr_all = None
    if fin_df is not None and not fin_df.empty and "net_profit" in fin_df.columns:
        clean = fin_df["net_profit"].dropna()
        n = len(clean)
        if n >= 4:
            s, e = float(clean.iloc[-4]), float(clean.iloc[-1])
            if s > 0 and e > 0:
                cagr3 = round(((e / s) ** (1 / 3) - 1) * 100, 1)
        if n >= 2:
            s, e = float(clean.iloc[0]), float(clean.iloc[-1])
            if s > 0 and e > 0:
                cagr_all = round(((e / s) ** (1 / max(n - 1, 1)) - 1) * 100, 1)
    yoy = meta.get("profit_yoy_forward")
    if yoy is None:
        yoy = meta.get("profit_yoy")
    verdict = "窗口可用(有正CAGR)" if (cagr3 or 0) > 0 else "窗口不可用→禁止回退同比"
    print(f"  {sym:<12}{str(nm)[:8]:<10}{n:>8}{str(cagr3):>10}{str(cagr_all):>10}"
          f"{str(round(float(yoy),1)) if yoy is not None else '-':>9}  {verdict}")
PY
