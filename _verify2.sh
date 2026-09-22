echo '=== 鍏辨尟璇诲眰锛堝墠 6 鏉★級==='
curl -s -m 30 "http://127.0.0.1:8002/api/v1/fundamentals/market-scan/resonance?top=6" | /usr/bin/python3 -c "
import json,sys
d = json.load(sys.stdin)['data']
print('matched:', d['matched'], '| counts:', d['verdict_counts'])
for it in d['items']:
    print(it['symbol'], it['name'], '| 缁煎悎', it['composite_score'], '| 鎶€鏈?, it.get('tech_score'), '|', it['verdict_label'])
"
echo '=== 浠呮牳蹇冩寔浠?==='
curl -s -m 30 "http://127.0.0.1:8002/api/v1/fundamentals/market-scan/resonance?top=5&verdict_filter=core" | /usr/bin/python3 -c "
import json,sys
d = json.load(sys.stdin)['data']
print('core matched:', d['matched'])
for it in d['items'][:5]:
    print(' ', it['symbol'], it['name'], it['composite_score'], it.get('tech_score'))
"
echo '=== 閲嶆柊瑙﹀彂 profit_yoy 鍥炲～锛堥潪 force锛岀画璺戯級 ==='
nohup curl -s -X POST "http://127.0.0.1:8002/api/v1/fundamentals/factors/rebuild?budget_sec=10800" > /tmp/rebuild.log 2>&1 &
sleep 2
echo "rebuild kicked: $(head -c 150 /tmp/rebuild.log)"
