set -e
echo '=== market scan 相关日志（最近一次） ==='
journalctl -u candle-flow --since "today" --no-pager | grep -E "market scan|BATCH DEADLINE|prescreen|TIMEOUT|fundamentals" | tail -40
echo
echo '=== 最近请求日志 ==='
journalctl -u candle-flow --since "-2 hours" --no-pager | grep -E "POST /api/v1/signals/scan/market|GET /api/v1/signals/scan/market/progress" | tail -10
echo
echo '=== 服务状态与内存 ==='
systemctl status candle-flow --no-pager | head -8
free -m | head -3
echo
echo '=== 因子回填进程是否在跑 ==='
ps aux | grep -iE "rebuild|factor" | grep -v grep || echo "no factor rebuild process"
echo
echo '=== 扫描 job 当前状态 ==='
curl -s "http://127.0.0.1:8002/api/v1/signals/scan/market/progress" | head -c 600
