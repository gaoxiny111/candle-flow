# -*- coding: utf-8 -*-
"""核实「短期借款」等负债科目的原始取数：格力 vs 美的 vs 海尔。"""
import sys
from io import StringIO
sys.path.insert(0, '.')

import pandas as pd
import requests
from app.analysis.sina_financials import _BASE, _HEADERS, _code6, PAGE_BALANCE

KEY = ('借款', '负债', '货币资金', '流动资产', '流动负债', '预收', '合同', '其他流动', '应付债券', '租赁')

for sym, nm in (('000651.SZ', '格力电器'), ('000333.SZ', '美的集团'), ('600690.SH', '海尔智家')):
    code = _code6(sym)
    print('=' * 78)
    print(nm, sym)
    print('=' * 78)
    try:
        r = requests.get(_BASE.format(page=PAGE_BALANCE, code=code, year=2026),
                         headers=_HEADERS, timeout=20)
        r.encoding = 'gb2312'
        tables = pd.read_html(StringIO(r.text))
        t = max(tables, key=lambda x: x.shape[0] * x.shape[1])
        hdr = [str(x) for x in t.iloc[0].tolist()]
        print('报告期列:', hdr[:5])
        for _, row in t.iterrows():
            lab = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ''
            if lab and any(k in lab for k in KEY):
                vals = []
                for i in range(1, min(4, len(row))):
                    try:
                        v = float(str(row.iloc[i]).replace(',', ''))
                        vals.append('%14.2f' % v)
                    except Exception:
                        vals.append('%14s' % str(row.iloc[i])[:14])
                print('  %-28s %s' % (lab[:28], ' '.join(vals)))
    except Exception as e:
        print('  FAIL', e)
    print()
