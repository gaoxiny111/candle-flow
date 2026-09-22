"""立霸 603519 当前系统全链路评分快照。"""
import json
import sys

sys.path.insert(0, ".")

from app.analysis.engine import FundamentalEngine  # noqa

SYM = "603519.SH"
eng = FundamentalEngine()
rep = FundamentalEngine().run_full_analysis(SYM)

out = {
    "symbol": SYM,
    "composite_score": rep.get("composite_score"),
    "rating": rep.get("rating"),
    "modules": {},
}
for name, m in (rep.get("modules") or {}).items():
    out["modules"][name] = {
        "score": m.get("score"),
        "level": m.get("level"),
        "indicator_count": len(m.get("indicators") or []),
        "indicators": [
            {"name": i.get("name"), "value": i.get("value"), "score": i.get("score"), "weight": i.get("weight")}
            for i in (m.get("indicators") or [])
        ],
        "warnings": (m.get("warnings") or [])[:8],
        "metadata": m.get("metadata"),
    }

print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
