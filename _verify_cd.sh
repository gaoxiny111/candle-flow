#!/bin/bash
# C/D 段线上验证：共振索引重建（180 根窗口）+ 抽样对照
API=http://127.0.0.1:8002/api/v1
cd /opt/candle-flow/backend || exit 1
PY=.venv/bin/python
KWS="603136 002545 600301 600777"

sample() {
  tag="$1"
  echo "===== SAMPLE $tag ====="
  for kw in $KWS; do
    echo "--- $kw"
    curl -s -m 120 "$API/fundamentals/market-scan/resonance?keyword=$kw&top=5" > /tmp/reso_${tag}_$kw.json
    $PY - "/tmp/reso_${tag}_$kw.json" "$tag" <<'PY'
import json, sys
path, tag = sys.argv[1], sys.argv[2]
raw = open(path, encoding="utf-8").read()
try:
    d = json.loads(raw)
except Exception as e:
    print("  JSON parse error:", e, "| raw head:", raw[:200]); raise SystemExit
data = d.get("data") or {}
print("  matched =", data.get("matched"), "| index_age_sec =", data.get("index_age_sec"))
items = data.get("items") or []
if tag == "D0" and items:
    print("  [字段清单]", ", ".join(sorted(items[0].keys())))
for it in items:
    print("  ", it.get("symbol"), it.get("name"),
          "| fund", it.get("composite_score"),
          "| tech", it.get("tech_score"),
          "|", it.get("verdict_label"),
          "| pattern", it.get("pattern_name"), it.get("pattern_score"),
          "| hits", it.get("confluence_hits"),
          "| peg", it.get("peg"))
    print("     reasons:", it.get("verdict_reasons"))
    flags = {k: it.get(k) for k in
             ("weekly_trend", "ma_alignment", "structure_flaw",
              "emotion_extreme", "low_momentum", "stage", "candidate")
             if k in it}
    print("     flags:", flags)
PY
  done
}

echo "===== 索引现状 ====="
curl -s -m 30 "$API/fundamentals/market-scan/resonance/progress"; echo

echo
sample D0

echo
echo "===== C. 重建共振索引（180 根窗口）====="
date -u '+%H:%M:%S UTC'
curl -s -m 120 -X POST "$API/fundamentals/market-scan/resonance/build?force=true"; echo
for i in $(seq 1 120); do
  p=$(curl -s -m 30 "$API/fundamentals/market-scan/resonance/progress")
  echo "[poll $i] $p"
  echo "$p" | grep -Eq '"status" *: *"(done|error)"' && break
  sleep 15
done
date -u '+%H:%M:%S UTC'

echo
sample D1
echo "===== DONE ====="
