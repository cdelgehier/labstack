#!/usr/bin/env bash
set -euo pipefail
model_api=$(python3 -c 'import json; print(json.load(open("/etc/labstack-holmes/model_list.yaml"))["demo"]["api_base"])')
curl --fail --silent --show-error --max-time 10 "${model_api%/}/models" >/dev/null

recover() {
  if [[ -f /srv/demo/message.txt.saved ]]; then
    mv /srv/demo/message.txt.saved /srv/demo/message.txt
  fi
  chmod 0644 /srv/demo/message.txt
  systemctl start demo-fastapi
}
trap recover EXIT

wait_idle() {
  local deadline=$((SECONDS + 650))
  while (( SECONDS < deadline )); do
    if curl --fail --silent http://127.0.0.1:18082/healthz \
      | jq -e '.active == false' >/dev/null; then
      return
    fi
    sleep 3
  done
  echo 'Holmes is still investigating.' >&2
  return 1
}

wait_clear() {
  local deadline=$((SECONDS + 60))
  while (( SECONDS < deadline )); do
    if curl --fail --silent --get http://127.0.0.1:9090/api/v1/query \
      --data-urlencode 'query=ALERTS{alertstate="firing",alertname=~"DemoHttpUnavailable|DemoApiUnavailable"}' \
      | jq -e '.data.result | length == 0' >/dev/null; then
      return
    fi
    sleep 2
  done
  echo 'Application alert did not resolve.' >&2
  return 1
}

wait_report() {
  local name=$1 since=$2 expected=$3
  local deadline=$((SECONDS + 650))
  while (( SECONDS < deadline )); do
    if python3 - "$name" "$since" "$expected" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path
db = sqlite3.connect('/var/lib/labstack-holmes/incidents.db')
db.row_factory = sqlite3.Row
rows = db.execute('SELECT * FROM jobs WHERE created >= ? AND state=? AND message IS NOT NULL AND sent=1 ORDER BY created', (float(sys.argv[2]), 'done'))
for row in rows:
    payload = json.loads(row['payload'])
    if payload['labels']['alertname'] == sys.argv[1]:
        print(row['message'], flush=True)
        evidence = Path('/var/lib/labstack-holmes') / (row['id'] + '.json')
        if not evidence.is_file() or (not sys.argv[3] and row['action'] is not None):
            sys.exit(2)
        if sys.argv[3] not in (row['outcome'] or ''):
            sys.exit(2)
        sys.exit(0)
sys.exit(1)
PY
    then
      return
    else
      local result=$?
      if [[ $result == 2 ]]; then
        echo 'The report did not confirm the expected executor outcome.' >&2
        return 1
      fi
    fi
    sleep 3
  done
  journalctl -u labstack-holmes-bridge -n 25 --no-pager
  echo "No completed Holmes notification for $name." >&2
  return 1
}

echo 'Check service recovery with SOP.'
wait_idle
scenario_start=$(date +%s)
systemctl stop demo-fastapi
wait_report DemoHttpUnavailable "$scenario_start" 'Verified action: start_fastapi'
systemctl is-active --quiet demo-fastapi
wait_clear

# The executor deliberately separates repair attempts by at least one minute.
echo 'Wait for the repair cooldown before the next fault.'
sleep 60
echo 'Check permission recovery with logs and trace evidence.'
scenario_start=$(date +%s)
chmod 000 /srv/demo/message.txt
wait_report DemoApiUnavailable "$scenario_start" 'Verified action: restore_permissions'
test "$(stat -c %a /srv/demo/message.txt)" = 644
wait_clear

echo 'Check escalation for a missing file outside the repair policy.'
scenario_start=$(date +%s)
mv /srv/demo/message.txt /srv/demo/message.txt.saved
wait_report DemoApiUnavailable "$scenario_start" ''
test ! -e /srv/demo/message.txt
mv /srv/demo/message.txt.saved /srv/demo/message.txt
wait_clear

curl --fail --silent 'http://127.0.0.1:8081/holmes/json?poll=1' \
  | jq -e 'select(.event == "message" and (.message | contains("Executor:")))' >/dev/null
echo 'Holmes diagnosis, repairs, escalation and ntfy delivery passed.'
