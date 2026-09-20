cd /opt/candle-flow/backend
PY=$(ls /opt/candle-flow/backend/venv/bin/python 2>/dev/null || which python3)
echo "using $PY"
"$PY" - <<'EOF'
import sqlite3
db = sqlite3.connect('data/candle_flow.db')
c = db.cursor()
row = c.execute("""
SELECT
  SUM(CASE WHEN composite_score>=85 THEN 1 ELSE 0 END),
  SUM(CASE WHEN composite_score>=70 AND composite_score<85 THEN 1 ELSE 0 END),
  SUM(CASE WHEN composite_score>=60 AND composite_score<70 THEN 1 ELSE 0 END),
  SUM(CASE WHEN composite_score<60 THEN 1 ELSE 0 END),
  COUNT(*)
FROM factor_snapshots WHERE composite_score IS NOT NULL
""").fetchone()
print(">=85:", row[0], "| 70-85:", row[1], "| 60-70:", row[2], "| <60:", row[3], "| total:", row[4])
# 主板近似（剔除 300/301/302/688/689 前缀）
row2 = c.execute("""
SELECT SUM(CASE WHEN composite_score>=60 THEN 1 ELSE 0 END), COUNT(*)
FROM factor_snapshots
WHERE composite_score IS NOT NULL
  AND substr(symbol,1,3) NOT IN ('300','301','302','688','689')
""").fetchone()
print("主板 ge60:", row2[0], "| 主板 total:", row2[1])
EOF
