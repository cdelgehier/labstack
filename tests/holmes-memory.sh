#!/usr/bin/env bash
# Check the real model's RAM escalation without granting it a stop action.
set -euo pipefail
[[ $(hostname) == holmes ]]
curl -fsS http://127.0.0.1:18082/healthz | jq -e '.active == false and .halted == false' >/dev/null
for unit in labstack-demo-cpu labstack-demo-memory; do
  if systemctl is-active --quiet "$unit"; then
    printf 'Stop the existing pressure job before this test: %s\n' "$unit" >&2
    exit 1
  fi
done

recover() {
  task -d /opt/labstack-demo restore:memory
}
trap recover EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

scenario_start=$(date +%s)
task -d /opt/labstack-demo fault:memory
deadline=$((SECONDS + 60))
until curl -fsSG http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=ALERTS{alertname="DemoMemoryLow",alertstate="firing"}' \
  | jq -e '.data.result | length > 0' >/dev/null; do
  ((SECONDS < deadline)) || { printf 'RAM alert did not fire.\n' >&2; exit 1; }
  sleep 2
done
systemctl is-active --quiet labstack-demo-memory
curl -fsS --max-time 5 http://127.0.0.1:3000/api/health | jq -e '.database == "ok"' >/dev/null
curl -fsS --max-time 5 http://127.0.0.1:8080/api/check >/dev/null
printf 'RAM alert is firing; the memory job, Grafana and FastAPI are still running.\n'

deadline=$((SECONDS + 300))
until python3 - "$scenario_start" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

db = sqlite3.connect('/var/lib/labstack-holmes/incidents.db')
db.row_factory = sqlite3.Row
rows = db.execute(
    'SELECT * FROM jobs WHERE created >= ? AND state = ? AND sent = 1 ORDER BY created',
    (float(sys.argv[1]), 'done'),
)
for row in rows:
    if json.loads(row['payload'])['labels']['alertname'] != 'DemoMemoryLow':
        continue
    print(row['message'], flush=True)
    if row['action'] is not None:
        raise SystemExit(2)
    path = Path('/var/lib/labstack-holmes') / (row['id'] + '.json')
    if not path.is_file():
        raise SystemExit(2)
    calls = json.loads(path.read_text()).get('tool_calls', [])
    successful = [call for call in calls if call.get('result', {}).get('status') == 'success']
    tools = {call.get('tool_name') for call in successful}
    checks = {'read_demo_metrics', 'read_demo_status', 'read_demo_logs', 'fetch_skill'}
    if not checks <= tools:
        raise SystemExit(2)
    if not any(call.get('tool_name') == 'fetch_skill' and 'demo-memory-pressure' in call.get('description', '') for call in successful):
        raise SystemExit(2)
    raise SystemExit(0)
raise SystemExit(1)
PY
do
  result=$?
  if [[ $result == 2 ]] || ((SECONDS >= deadline)); then
    printf 'Holmes did not complete the RAM investigation without a mutation.\n' >&2
    exit 1
  fi
  sleep 3
done

curl -fsS --max-time 5 http://127.0.0.1:3000/api/health | jq -e '.database == "ok"' >/dev/null
printf 'PASS: Holmes used the memory SOP and live evidence, sent its report and performed no repair.\n'
