#!/usr/bin/env bash
# Check the real model's CPU repair without changing its incident journal.
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
  task -d /opt/labstack-demo restore:cpu
}
trap recover EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Respect the existing global action cooldown instead of clearing its journal.
deadline=$((SECONDS + 70))
until python3 - <<'PY'
import sqlite3
import time
db = sqlite3.connect('/var/lib/labstack-holmes/incidents.db')
last = db.execute('SELECT max(action_at) FROM jobs').fetchone()[0]
raise SystemExit(0 if last is None or time.time() - last >= 60 else 1)
PY
do
  ((SECONDS < deadline)) || { printf 'Repair cooldown did not expire.\n' >&2; exit 1; }
  sleep 2
done

scenario_start=$(date +%s)
task -d /opt/labstack-demo fault:cpu
deadline=$((SECONDS + 240))
until python3 - "$scenario_start" <<'PY'
import json
import sqlite3
import sys
db = sqlite3.connect('/var/lib/labstack-holmes/incidents.db')
db.row_factory = sqlite3.Row
rows = db.execute(
    'SELECT * FROM jobs WHERE created >= ? AND state = ? AND sent = 1 ORDER BY created',
    (float(sys.argv[1]), 'done'),
)
for row in rows:
    if json.loads(row['payload'])['labels']['alertname'] != 'DemoCpuBusy':
        continue
    print(row['message'], flush=True)
    verified = row['action'] == 'stop_cpu_stress' and 'Verified action: stop_cpu_stress' in (row['outcome'] or '')
    raise SystemExit(0 if verified else 2)
raise SystemExit(1)
PY
do
  result=$?
  if [[ $result == 2 ]] || ((SECONDS >= deadline)); then
    printf 'Holmes did not verify CPU recovery through the guarded tool.\n' >&2
    exit 1
  fi
  sleep 3
done

[[ $(systemctl is-active labstack-demo-cpu || true) != active ]]
curl -fsS --max-time 5 http://127.0.0.1:3000/api/health | jq -e '.database == "ok"' >/dev/null
printf 'PASS: Holmes stopped the identified CPU job and verified recovery; Grafana is healthy.\n'
