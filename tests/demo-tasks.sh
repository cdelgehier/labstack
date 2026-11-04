#!/usr/bin/env bash
# Run inside the demo VM as root through SSH.
set -euo pipefail

[[ $(hostname) == holmes ]]
command -v task >/dev/null
cd /opt/labstack-demo
task --list
[[ ! -e /srv/demo/message.txt.saved && ! -e /srv/demo/.labstack-demo-fill ]]
curl -fsS http://127.0.0.1:8080/api/check >/dev/null
curl -fsS http://127.0.0.1:18082/healthz | jq -e '.active == false' >/dev/null
bridge_was_active=false
if systemctl is-active --quiet labstack-holmes-bridge; then
  systemctl stop labstack-holmes-bridge
  bridge_was_active=true
fi

cleanup() {
  task restore
  # Let transient resource and storage alerts resolve before Holmes resumes.
  local deadline=$((SECONDS + 45))
  until curl -fsSG --max-time 3 http://127.0.0.1:9090/api/v1/query \
    --data-urlencode 'query=ALERTS{alertname=~"Demo(CpuBusy|MemoryLow|DiskSpaceLow|ExpectedMountMissing|HttpUnavailable|ApiUnavailable)",alertstate="firing"}' |
    jq -e '.data.result | length == 0' >/dev/null; do
    if ((SECONDS >= deadline)); then
      printf 'Demo alerts have not all resolved; inspect Prometheus.\n' >&2
      break
    fi
    sleep 2
  done
  if [[ $bridge_was_active == true ]]; then
    systemctl start labstack-holmes-bridge
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

wait_for_alert() {
  local alert=$1
  local deadline=$((SECONDS + 75))
  until curl -fsSG --max-time 3 http://127.0.0.1:9090/api/v1/query \
    --data-urlencode "query=ALERTS{alertname=\"$alert\",alertstate=\"firing\"}" |
    jq -e '.data.result | length > 0' >/dev/null; do
    ((SECONDS < deadline)) || { printf 'FAIL: %s never fired\n' "$alert" >&2; return 1; }
    sleep 2
  done
  printf 'PASS: %s fires through the injected fault\n' "$alert"
}

original_checksum=$(sha256sum /srv/demo/message.txt | cut -d ' ' -f 1)
task fault:service
[[ $(systemctl is-active demo-fastapi || true) != active ]]
task restore:service
curl -fsS http://127.0.0.1:8080/healthz >/dev/null

task fault:permissions
[[ $(stat -c %a /srv/demo/message.txt) == 0 ]]
[[ $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/api/check) == 503 ]]
task restore:permissions
[[ $(stat -c %a /srv/demo/message.txt) == 644 ]]

task fault:file
[[ ! -e /srv/demo/message.txt && -f /srv/demo/message.txt.saved ]]
if task fault:file; then
  printf 'FAIL: a second file fault must refuse to overwrite the saved file\n' >&2
  exit 1
fi
task restore:file
[[ $(sha256sum /srv/demo/message.txt | cut -d ' ' -f 1) == "$original_checksum" ]]

task fault:disk
wait_for_alert DemoDiskSpaceLow
task restore:disk
[[ ! -e /srv/demo/.labstack-demo-fill ]]
task fault:mount
if mountpoint -q /srv/demo; then exit 1; fi
if task fault:disk; then
  printf 'FAIL: filling an unmounted volume must be refused\n' >&2
  exit 1
fi
task restore:mount
mountpoint -q /srv/demo

task fault:cpu
wait_for_alert DemoCpuBusy
task restore:cpu
[[ $(systemctl is-active labstack-demo-cpu || true) != active ]]
task fault:memory
wait_for_alert DemoMemoryLow
[[ $(systemctl show labstack-demo-memory -p MemorySwapMax --value) == 0 ]]
# After allocation, the RAM fault must hold its pages without burning a CPU core.
cpu_before=$(systemctl show labstack-demo-memory -p CPUUsageNSec --value)
sleep 5
cpu_after=$(systemctl show labstack-demo-memory -p CPUUsageNSec --value)
if ((cpu_after - cpu_before >= 1000000000)); then
  printf 'FAIL: the memory fault consumed more than 20%% of one CPU core after allocation\n' >&2
  exit 1
fi
curl -fsS --max-time 5 http://127.0.0.1:3000/api/health | jq -e '.database == "ok"' >/dev/null
task restore:memory
[[ $(systemctl is-active labstack-demo-memory || true) != active ]]
curl -fsS --max-time 5 http://127.0.0.1:3000/api/health | jq -e '.database == "ok"' >/dev/null

# Recover simultaneous faults without losing the original data, and allow repetition.
task fault:file
task fault:disk
task fault:service
task restore
task restore
[[ $(sha256sum /srv/demo/message.txt | cut -d ' ' -f 1) == "$original_checksum" ]]
curl -fsS http://127.0.0.1:8080/api/check >/dev/null
task status
printf 'PASS: demo tasks inject and restore every fault\n'
