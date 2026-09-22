#!/bin/bash
API=http://127.0.0.1:8002/api/v1
cd /opt/candle-flow/backend || exit 1
curl -s -m 120 "$API/fundamentals/market-scan/resonance?top=20" > /tmp/top20.json
.venv/bin/python - <<'PY'
import json
d = json.load(open('/tmp/top20.json'))['data']
items = d.get('items', [])
print(f"榜单共 {d.get('matched')} 只，取前 {len(items)}")
print(f"{'#':>3} {'代码':<11}{'名称':<9}{'综合':>6}{'技术':>6} {'档位':<6}{'PE':>9}{'PB':>7}{'股息%':>7}  {'形态':<10}{'形分':>5} {'买点信号':<8}")
for i, it in enumerate(items, 1):
    print(f"{i:>3} {it.get('symbol',''):<11}{it.get('name',''):<9}"
          f"{str(it.get('composite_score')):>6}{str(it.get('tech_score')):>6} "
          f"{str(it.get('verdict_label')):<6}{str(it.get('pe_ttm')):>9}{str(it.get('pb')):>7}"
          f"{str(it.get('dividend_yield')):>7}  {str(it.get('pattern_name')):<10}"
          f"{str(it.get('pattern_score')):>5} {str(it.get('buy_signal')):<8}")
print()
print("—— 三只被质疑的票现居何处 ——")
for kw in ('600722', '603995'):
    r = json.load(open('/tmp/top20.json'))
for it in items:
    if it.get('symbol','').startswith(('600722','603995','002705','605028')):
        print(" ", it['symbol'], it['name'], "| 排名", items.index(it)+1,
              "| 综合", it.get('composite_score'), "| 技术", it.get('tech_score'),
              "|", it.get('verdict_label'), "| pe", it.get('pe_ttm'), "| pb", it.get('pb'))
PY
