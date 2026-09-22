#!/bin/bash
# 左侧防守「决策日口径」的线上影响面：等索引构建完成 → 统计档位与买点信号分布
JOB="$1"
cd /opt/candle-flow/backend || exit 1
for i in $(seq 1 90); do
  S=$(curl -s -m 15 "http://127.0.0.1:8002/api/v1/fundamentals/market-scan/resonance/progress?job_id=${JOB}")
  ST=$(echo "$S" | .venv/bin/python -c "import sys,json;print(json.load(sys.stdin)['data']['status'])" 2>/dev/null)
  echo "poll$i status=$ST"
  if [ "$ST" = "success" ]; then echo "索引构建完成"; break; fi
  if [ "$ST" = "failed" ] || [ "$ST" = "error" ]; then echo "构建失败：$S"; exit 1; fi
  sleep 20
done

.venv/bin/python -u - <<'PY'
import json, urllib.request
from collections import Counter

def get(url):
    with urllib.request.urlopen(url, timeout=90) as r:
        return json.load(r)["data"]

d = get("http://127.0.0.1:8002/api/v1/fundamentals/market-scan/resonance?top=500")
print("返回字段:", sorted(d.keys()))
print("档位计数 counts:", d.get("counts"))
print("total/matched:", d.get("total"), d.get("matched"))
items = d.get("items") or []
print("返回条数:", len(items))
print("买点信号分布:", dict(Counter(i.get("buy_signal") for i in items)))
blk = [i for i in items if i.get("tech_blocker")]
print("技术面被否决:", len(blk),
      "；其中左侧超跌形态:", sum(1 for i in blk if "左侧超跌形态" in (i.get("tech_blocker") or "")))
print()
print("榜单前 15 条（用户实际看到的）：")
print(f"  {'代码':<12}{'名称':<8}{'基本面':>7}{'技术':>6}{'档位':<12}{'买点信号':<18}{'形态'}")
for i in items[:15]:
    print(f"  {str(i.get('symbol')):<12}{str(i.get('name'))[:6]:<8}"
          f"{str(i.get('composite_score')):>7}{str(i.get('tech_score')):>6}"
          f"{str(i.get('verdict')):<12}{str(i.get('buy_label')):<18}{i.get('pattern_name')}")
PY
