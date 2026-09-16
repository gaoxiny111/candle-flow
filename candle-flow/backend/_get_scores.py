import sys
sys.path.insert(0, '.')
from app.analysis.engine import FundamentalEngine

r = FundamentalEngine().run_full_analysis('601088.SH', db=None)
print('composite:', r.get('composite_score'))
mods = r.get('modules', {})
for k, v in mods.items():
    print(f"  {k}: {v.get('score')} ({v.get('level')})")
print()
print('valuation score:', r.get('valuation', {}).get('score') or r.get('valuation', {}).get('composite_valuation_score'))
print('pe_ttm:', r.get('market', {}).get('pe_ttm'))
print('pb:', r.get('market', {}).get('pb'))
print('price:', r.get('market', {}).get('price'))
print('dividend_yield:', r.get('market', {}).get('dividend_yield'))
