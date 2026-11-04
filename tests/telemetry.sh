#!/usr/bin/env bash
# Run inside the demo VM as root through SSH.
set -euo pipefail

if [[ $(hostname) != holmes ]] || ! mountpoint -q /srv/demo; then
  printf 'This test requires the Labstack Holmes demo VM and its data volume.\n' >&2
  exit 1
fi

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

trace_received() {
  curl -fsS --max-time 3 "http://127.0.0.1:16686/api/traces/$trace_id" |
    jq -e --arg trace "$trace_id" --arg error "$1" '
      .data[0] | .traceID == $trace and
      any(.spans[]; .operationName == "GET /api/check") and
      any(.spans[]; .operationName == "read demo message" and
        (any(.tags[]; .key == "error" and .value == true) == ($error == "true")))' >/dev/null
}

log_received() {
  curl -fsSG --max-time 3 http://127.0.0.1:3100/loki/api/v1/query_range \
    --data-urlencode "query={service_name=\"demo-fastapi\"} | json | trace_id=\"$trace_id\"" |
    jq -e --argjson status "$1" --arg error "${2:-}" '.status == "success" and
      any(.data.result[].values[]; (.[1] | fromjson) |
        .status == $status and ($error == "" or .error_type == $error))' >/dev/null
}

source_logs_received() {
  curl -fsSG --max-time 3 http://127.0.0.1:3100/loki/api/v1/query_range \
    --data-urlencode "query={service_name=\"$1\"}" |
    jq -e '.status == "success" and (.data.result | length > 0)' >/dev/null
}

alert_is() {
  curl -fsSG --max-time 3 http://127.0.0.1:9090/api/v1/query \
    --data-urlencode 'query=count(ALERTS{alertname="DemoApiUnavailable",alertstate="firing"}) or vector(0)' |
    jq -e --arg expected "$1" '.status == "success" and .data.result[0].value[1] == $expected' >/dev/null
}

notification_received() {
  curl -fsSG --max-time 3 http://127.0.0.1:8081/alerts/json \
    --data-urlencode poll=1 --data-urlencode "since=$notification_start" |
    jq -se --arg status "$1" 'any(.[]; .event == "message" and
      ((.message // "") | contains("Status: " + $status)) and
      ((.title // "") | contains("DemoApiUnavailable")))' >/dev/null
}

# First prove that the public route returns a real trace, not just a healthy proxy.
response=$(curl -fsS --max-time 5 http://127.0.0.1:8080/api/check)
trace_id=$(jq -er '.trace_id | select(test("^[a-f0-9]{32}$") and . != "00000000000000000000000000000000")' <<<"$response")
jq -e '.status == "ok" and .message == "Labstack demo is healthy"' <<<"$response" >/dev/null
wait_for 'healthy request reaches Jaeger' trace_received false
wait_for 'healthy request reaches Loki with the same trace ID' log_received 200
request_trace=$(tr -d '-' </proc/sys/kernel/random/uuid)
response=$(curl -fsS --max-time 5 http://127.0.0.1:8080/api/check \
  -H "Traceparent: 00-$request_trace-0123456789abcdef-01")
jq -e --arg trace "$request_trace" '.trace_id == $trace' <<<"$response" >/dev/null
printf 'PASS: W3C trace context is kept in FastAPI\n'
wait_for 'FastAPI logs reach Loki' source_logs_received demo-fastapi
wait_for 'Linux service logs reach Loki' source_logs_received linux

# Restore the original mode even if an assertion fails during the incident.
original_mode=$(stat -c '%a' /srv/demo/message.txt)
message_backup=/srv/demo/.labstack-message-test
if [[ -e $message_backup ]]; then
  printf 'A previous message backup exists. Inspect it first.\n' >&2
  exit 1
fi
restore_permissions() {
  if [[ -f $message_backup ]]; then
    mv "$message_backup" /srv/demo/message.txt
  fi
  chmod "$original_mode" /srv/demo/message.txt
  restore_bridge
}
trap restore_permissions EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
notification_start=$(date +%s)
chmod 000 /srv/demo/message.txt
[[ $(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/api/check) == 503 ]]
response=$(curl -sS --max-time 5 http://127.0.0.1:8080/api/check)
jq -e '.status == "error" and .error == "dependency_unavailable"' <<<"$response" >/dev/null
trace_id=$(jq -er '.trace_id' <<<"$response")
wait_for 'permission failure is recorded in the trace' trace_received true
wait_for 'error log has the same trace ID, status and permission cause' log_received 503 PermissionError
wait_for 'API probe raises an alert' alert_is 1
wait_for 'API failure reaches the notification inbox' notification_received Firing
chmod "$original_mode" /srv/demo/message.txt
response=$(curl -fsS --max-time 5 http://127.0.0.1:8080/api/check)
jq -e '.status == "ok"' <<<"$response" >/dev/null
wait_for 'restoring permissions clears the API alert' alert_is 0
wait_for 'API recovery reaches the notification inbox' notification_received Resolved

# A missing file has the same HTTP symptom but a different, non-repairable cause.
notification_start=$(date +%s)
mv /srv/demo/message.txt "$message_backup"
response=$(curl -sS --max-time 5 http://127.0.0.1:8080/api/check)
trace_id=$(jq -er '.trace_id' <<<"$response")
[[ $(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/api/check) == 503 ]]
curl -fsS --max-time 5 http://127.0.0.1:8080/healthz >/dev/null
wait_for 'missing file is recorded in its failed trace' trace_received true
wait_for 'missing file is distinguished from a permission error' log_received 503 FileNotFoundError
wait_for 'missing file raises the API alert' alert_is 1
wait_for 'missing file reaches the notification inbox' notification_received Firing
test ! -e /srv/demo/message.txt
mv "$message_backup" /srv/demo/message.txt
wait_for 'replacing the file clears the API alert' alert_is 0
wait_for 'file recovery reaches the notification inbox' notification_received Resolved
cd /opt/labstack-observability
docker compose restart loki jaeger
systemctl restart labstack-telemetry
wait_for 'trace survives a Jaeger restart' trace_received true
wait_for 'log survives a Loki restart' log_received 503
printf 'PASS: request, log, trace, alert and recovery are connected\n'
