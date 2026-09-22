curl -s -m 30 "http://127.0.0.1:8002/api/v1/fundamentals/market-scan/resonance?top=3" > /tmp/reso.json
/usr/bin/python3 -c "import json; d=json.load(open('/tmp/reso.json'))['data']; print('matched:', d['matched'], '| counts:', d['verdict_counts']); print('index:', d['index_stats'])"
echo '--- rebuild check ---'
sleep 5; head -c 200 /tmp/rebuild.log; echo
ps aux | grep -c "[f]actors/rebuild"
