# -*- coding: utf-8 -*-
"""银行/金融股基本面诊断：看现有系统对银行的每个模块怎么打分、卡在哪里。"""
import sys, json
sys.path.insert(0, '.')
from app.analysis.engine import FundamentalEngine

SYM = sys.argv[1] if len(sys.argv) > 1 else '600036.SH'
print('=' * 78)
print('SYMBOL:', SYM)
print('=' * 78)
r = FundamentalEngine().run_full_analysis(SYM, db=None)

print('name        :', r.get('name'))
print('industry    :', r.get('industry'))
print('latest      :', r.get('latest_report'))
print('COMPOSITE   :', r.get('composite_score'), r.get('rating'))
print('dim_scores  :', json.dumps(r.get('dim_scores'), ensure_ascii=False))
print('cashflow_veto:', r.get('cashflow_veto'), 'compliance_veto:', r.get('compliance_veto'))

mods = r.get('modules', {})
print()
for k, v in mods.items():
    if isinstance(v, dict):
        print('  %-12s %-6s %s' % (k, v.get('score'), v.get('level')))
    else:
        print('  %-12s %s' % (k, v))

for k, v in mods.items():
    if not isinstance(v, dict):
        continue
    print()
    print('=' * 78)
    print('MODULE:', k, '->', v.get('score'), v.get('level'))
    print('metadata:', json.dumps(v.get('metadata'), ensure_ascii=False)[:600])
    for ind in v.get('indicators', []) or []:
        print('   [%s] value=%s score=%s w=%s' % (ind.get('name'), ind.get('value'),
                                                  ind.get('score'), ind.get('weight')))
        c = ind.get('comment')
        if c:
            print('        ', c)
    for w in v.get('warnings', []) or []:
        print('   * ', w)

print()
print('=== TOP-LEVEL WARNINGS ===')
for w in r.get('warnings', []) or []:
    print('  -', w)
