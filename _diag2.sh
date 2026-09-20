echo '=== market-coverage 接口 ==='
curl -s -m 30 "http://127.0.0.1:8002/api/v1/fundamentals/market-coverage" | head -c 800
echo
echo '=== 最近的 coverage 报错日志 ==='
journalctl -u candle-flow --since "-40 min" --no-pager | grep -iE "coverage|ERROR|Traceback" | tail -20
echo '=== 回填进度（factor snapshot 总数） ==='
cd /opt/candle-flow/backend && ./venv/bin/python -c "
from app.services.factor_db import coverage_stats
import json
print(json.dumps(coverage_stats(), ensure_ascii=False)[:400])
" 2>&1 | head -5
