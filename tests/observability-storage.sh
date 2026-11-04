#!/usr/bin/env bash
# Run inside the demo VM as root through SSH.
set -euo pipefail

if [[ $(hostname) != holmes ]] || [[ ! -f /etc/systemd/system/srv-demo.mount ]]; then
  printf 'This test requires the Labstack demo data volume.\n' >&2
  exit 1
fi
mountpoint -q /srv/demo
findmnt -rn -M /srv/demo -t ext4 -o SOURCE | grep -q '^/dev/loop'
if [[ -e /srv/demo/.labstack-space-test ]]; then
  printf 'A previous disk test file already exists. Inspect it first.\n' >&2
  exit 1
fi

bridge_was_active=false
if systemctl is-active --quiet labstack-holmes-bridge; then
  curl -fsS http://127.0.0.1:18082/healthz | jq -e ".active == false" >/dev/null
  systemctl stop labstack-holmes-bridge
  bridge_was_active=true
fi

restore_volume() {
  systemctl start srv-demo.mount
  if mountpoint -q /srv/demo; then
    rm -f /srv/demo/.labstack-space-test
  fi
  if [[ $bridge_was_active == true ]]; then
    systemctl start labstack-holmes-bridge
  fi
}
trap restore_volume EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

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

alert_is() {
  curl -fsSG --max-time 3 http://127.0.0.1:9090/api/v1/query \
    --data-urlencode "query=count(ALERTS{alertname=\"$1\",mountpoint=\"/srv/demo\",alertstate=\"firing\"}) or vector(0)" |
    jq -e --arg expected "$2" '.data.result[0].value[1] == $expected' >/dev/null
}

notification_received() {
  curl -fsSG --max-time 3 http://127.0.0.1:8081/alerts/json \
    --data-urlencode poll=1 --data-urlencode "since=$notification_start" |
    jq -se --arg status "$1" --arg alert "$2" 'any(.[]; .event == "message" and
      ((.message // "") | contains("Status: " + $status)) and
      ((.title // "") | contains($alert)))' >/dev/null
}

notification_start=$(date +%s)
# This write targets only the mounted loop filesystem, never the VM root disk.
read -r disk_size disk_available < <(df -B1 --output=size,avail /srv/demo | tail -n 1)
fill_bytes=$((disk_available - disk_size / 20))
((fill_bytes > 0))
fallocate -l "$fill_bytes" /srv/demo/.labstack-space-test
sync /srv/demo/.labstack-space-test
wait_for 'low space on the data volume fires an alert' alert_is DemoDiskSpaceLow 1
wait_for 'low-space notification is received' notification_received Firing DemoDiskSpaceLow
rm /srv/demo/.labstack-space-test
wait_for 'freeing the data volume clears the alert' alert_is DemoDiskSpaceLow 0
wait_for 'disk-space recovery notification is received' notification_received Resolved DemoDiskSpaceLow

notification_start=$(date +%s)
systemctl stop srv-demo.mount
wait_for 'missing expected mount fires an alert' alert_is DemoExpectedMountMissing 1
wait_for 'missing-mount notification is received' notification_received Firing DemoExpectedMountMissing
systemctl start srv-demo.mount
wait_for 'remounting the data volume clears the alert' alert_is DemoExpectedMountMissing 0
wait_for 'mount recovery notification is received' notification_received Resolved DemoExpectedMountMissing
