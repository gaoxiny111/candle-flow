import sys, json
sys.path.insert(0, '.')
from app.analysis.engine import FundamentalEngine

SYM = sys.argv[1] if len(sys.argv) > 1 else '000062.SZ'
r = FundamentalEngine().run_full_analysis(SYM, db=None)

print('=== COMPOSITE ===', r.get('composite_score'), r.get('rating'))
mods = r.get('modules', {})
for k, v in mods.items():
    print(f"  {k}: {v.get('score')} ({v.get('level')})")

cf = mods.get('现金流质量') or mods.get('cashflow') or {}
print()
print('=== CASHFLOW module ===')
print('score:', cf.get('score'), 'level:', cf.get('level'))
print('metadata:', json.dumps(cf.get('metadata'), ensure_ascii=False))
print('--- indicators ---')
for ind in cf.get('indicators', []):
    print(f"  [{ind.get('name')}] value={ind.get('value')} score={ind.get('score')} "
          f"level={ind.get('level')} weight={ind.get('weight')} trend={ind.get('trend')}")
    print(f"      comment: {ind.get('comment')}")
print('--- warnings ---')
for w in cf.get('warnings', []):
    print('  *', w)

print()
print('=== MARKET ===')
m = r.get('market', {})
for k in ('price', 'pe_ttm', 'pb', 'market_cap', 'dividend_yield'):
    print(f'  {k}:', m.get(k))
