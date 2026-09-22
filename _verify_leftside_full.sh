#!/bin/bash
# 全市场影响面：分页读共振索引，统计档位分布 + 被左侧防守否决的票
cd /opt/candle-flow/backend || exit 1
.venv/bin/python -u - <<'PY'
import json, urllib.request
from collections import Counter

BASE = "http://127.0.0.1:8002/api/v1/fundamentals/market-scan/resonance"

def get(off):
    u = f"{BASE}?top=500&offset={off}"
    with urllib.request.urlopen(u, timeout=120) as r:
        return json.load(r)["data"]

first = get(0)
total = first["matched"]
print("覆盖率 coverage:", first.get("coverage"))
print("索引年龄(s):", first.get("index_age_sec"), "陈旧:", first.get("index_stale"))
print("全场档位分布 verdict_counts:", first.get("verdict_counts"))
print("索引统计 index_stats:", json.dumps(first.get("index_stats"), ensure_ascii=False)[:300])
print()

items = []
off = 0
while off < total:
    d = get(off)
    batch = d.get("items") or []
    if not batch:
        break
    items.extend(batch)
    off += 500
print("累计读取:", len(items), "/", total)
print()
print("全市场买点信号分布:", dict(Counter(i.get("buy_signal") for i in items).most_common()))
blk = [i for i in items if i.get("tech_blocker")]
lb = [i for i in blk if "左侧超跌形态" in (i.get("tech_blocker") or "")]
print("技术面被否决总数:", len(blk), "；其中左侧超跌形态:", len(lb))
print("被左侧防守否决者的原档位分布:", dict(Counter(i.get("verdict") for i in lb)))
print()
print("被左侧防守拦下的前 20 只（改前它们在候选池里）:")
for i in lb[:20]:
    print(f"  {i.get('symbol'):<12}{str(i.get('name'))[:6]:<8}fund={i.get('composite_score'):<6}"
          f"verdict={i.get('verdict'):<10}signal={i.get('buy_label')}")
PY
