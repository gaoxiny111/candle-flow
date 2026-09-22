"""验证 ops_efficiency 同期口径。"""
import json
import sys

sys.path.insert(0, ".")

from app.analysis.financials import build_financial_dataframe  # noqa

for sym in ("603519.SH", "600585.SH", "000651.SZ"):
    try:
        fd, meta = build_financial_dataframe(sym)
        print(f"\n=== {sym} latest_report={meta.get('latest_report')} ===")
        print(json.dumps(meta.get("ops_efficiency"), ensure_ascii=False, indent=1))
    except Exception as e:
        print(f"{sym} FAIL:", type(e).__name__, e)
