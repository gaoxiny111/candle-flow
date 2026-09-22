echo '=== health ==='
curl -s -m 20 "http://127.0.0.1:8002/api/v1/health" | head -c 200; echo
echo '=== 鏈嶅姟鍚姩鏃堕棿 ==='
systemctl show candle-flow -p ExecMainStartTimestamp --value
echo '=== resonance 璇诲眰锛堝簲涓?empty锛?=='
curl -s -m 30 "http://127.0.0.1:8002/api/v1/fundamentals/market-scan/resonance?top=3" | head -c 260; echo
echo '=== 瑙﹀彂绱㈠紩鏋勫缓 ==='
curl -s -m 30 -X POST "http://127.0.0.1:8002/api/v1/fundamentals/market-scan/resonance/build" | head -c 300; echo
echo '=== 瑕嗙洊鐜?==='
curl -s -m 30 "http://127.0.0.1:8002/api/v1/fundamentals/market-coverage" | head -c 300; echo
echo '=== 鏃ф鍗曟帴鍙ｄ粛姝ｅ父锛?==='
curl -s -m 30 "http://127.0.0.1:8002/api/v1/fundamentals/market-scan?top=2&exclude_st=true" | head -c 200; echo
echo '=== 鍥炲～杩涚▼ ==='
ps aux | grep -E "factors/rebuild" | grep -v grep || echo "no backfill running"
