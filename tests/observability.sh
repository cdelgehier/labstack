#!/usr/bin/env bash
# Run inside the demo VM as root through SSH.
set -euo pipefail

if [[ $(hostname) != holmes ]] || [[ ! -f /opt/labstack-fastapi/fastapi_app.py ]]; then
  printf 'This test requires the Labstack Holmes demo VM.\n' >&2
  exit 1
fi
systemctl is-active --quiet demo-fastapi
systemctl is-active --quiet labstack-observability

bridge_was_active=false
if systemctl is-active --quiet labstack-holmes-bridge; then
  curl -fsS http://127.0.0.1:18082/healthz | jq -e ".active == false" >/dev/null
  systemctl stop labstack-holmes-bridge
  bridge_was_active=true
fi
restore_bridge() {
  if [[ $bridge_was_active == true ]]; then
    systemctl start labstack-holmes-bridge
  fi
}
trap restore_bridge EXIT

wait_for() {
  local description=$1
  shift
  local deadline=$((SECONDS + 90))
  until "$@"; do
    if ((SECONDS >= deadline)); then
      printf 'FAIL: %s\n' "$description" >&2
      return 1
    fi
    sleep 1
  done
  printf 'PASS: %s\n' "$description"
}

query_is() {
  curl -fsSG --max-time 3 http://127.0.0.1:9090/api/v1/query \
    --data-urlencode "query=$1" |
    jq -e --arg expected "$2" '.status == "success" and
      (.data.result | length == 1 and .[0].value[1] == $expected)' >/dev/null
}

notification_received() {
  curl -fsSG --max-time 3 http://127.0.0.1:8081/alerts/json \
    --data-urlencode poll=1 --data-urlencode "since=$notification_start" |
    jq -se --arg status "$1" --arg alert "$2" 'any(.[]; .event == "message" and
      ((.message // "") | contains("Status: " + $status)) and
      ((.title // "") | contains($alert)))' >/dev/null
}

http_healthy() {
  curl -fsS --max-time 3 http://127.0.0.1:8080/ |
    grep -q 'Labstack demo is healthy'
}

wait_for 'all scrape targets are up' query_is 'min(up)' 1
wait_for 'systemd state is collected' query_is \
  'node_systemd_unit_state{name="demo-fastapi.service",state="active"}' 1
wait_for 'HTTP response is healthy' http_healthy
wait_for 'HTTP probe succeeds' query_is 'probe_success{job="demo-fastapi"}' 1
wait_for 'no demo alert is firing' query_is 'count(ALERTS{alertstate="firing"}) or vector(0)' 0
curl -fsS --max-time 5 http://127.0.0.1:3000/api/dashboards/uid/labstack-fastapi |
  jq -e '.dashboard.panels | length > 0' >/dev/null
printf 'PASS: Grafana dashboard is available without a login\n'

# Restore the exact page and start the service even if an assertion fails.
page_backup=$(mktemp)
cp -p /opt/labstack-fastapi/index.html "$page_backup"
restore_demo() {
  cp -p "$page_backup" /opt/labstack-fastapi/index.html
  rm -f "$page_backup"
  systemctl start demo-fastapi
  restore_bridge
}
trap restore_demo EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# A successful HTTP status must not hide a broken response body.
printf 'The demo page is broken.\n' >/opt/labstack-fastapi/index.html
curl -fsS --max-time 3 -o /dev/null http://127.0.0.1:8080/
curl -fsSG --max-time 5 http://127.0.0.1:9115/probe \
  --data-urlencode module=http_demo --data-urlencode target=http://127.0.0.1:8080/ |
  grep -q '^probe_success 0$'
cp -p "$page_backup" /opt/labstack-fastapi/index.html
printf 'PASS: wrong content is detected even with HTTP 200\n'

notification_start=$(date +%s)
systemctl stop demo-fastapi
wait_for 'stopped service fails its HTTP probe' query_is 'probe_success{job="demo-fastapi"}' 0
wait_for 'systemd reports the stopped service' query_is \
  'node_systemd_unit_state{name="demo-fastapi.service",state="active"}' 0
wait_for 'HTTP alert is firing' query_is 'ALERTS{alertname="DemoHttpUnavailable",alertstate="firing"}' 1
wait_for 'firing notification is received by ntfy' notification_received Firing DemoHttpUnavailable

systemctl start demo-fastapi
wait_for 'HTTP response is restored' http_healthy
wait_for 'HTTP probe succeeds again' query_is 'probe_success{job="demo-fastapi"}' 1
wait_for 'HTTP alert is cleared' query_is \
  'count(ALERTS{alertname="DemoHttpUnavailable",alertstate="firing"}) or vector(0)' 0
wait_for 'resolved notification is received by ntfy' notification_received Resolved DemoHttpUnavailable
