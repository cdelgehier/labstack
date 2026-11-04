#!/usr/bin/env bash
# Fixed demo targets only; this helper is run by the guest Taskfile with sudo.
set -euo pipefail

fail() {
  printf '%s\n' "$*" >&2
  exit 1
}

[[ $EUID == 0 && $(hostname) == holmes ]] || fail 'Run this task inside the Holmes demo VM.'
[[ -f /etc/systemd/system/demo-fastapi.service && -f /etc/systemd/system/srv-demo.mount ]] ||
  fail 'The demo service and data volume must be installed first.'
[[ $# == 1 ]] || fail 'Choose one demo task.'
exec 9>/run/lock/labstack-demo.lock
flock -n 9 || fail 'Another demo task is running.'

require_volume() {
  local source
  mountpoint -q /srv/demo || fail 'The demo volume is not mounted. Run task restore:mount.'
  source=$(findmnt -rn -M /srv/demo -t ext4 -o SOURCE)
  [[ $source == /dev/loop* ]] || fail 'Refusing to modify a volume other than the demo loop filesystem.'
  [[ $(losetup -n -O BACK-FILE "$source") == /var/lib/labstack-storage/data.ext4 ]] ||
    fail 'The mounted loop filesystem is not the demo image.'
}

require_message() {
  require_volume
  [[ -f /srv/demo/message.txt && ! -L /srv/demo/message.txt ]] ||
    fail 'The demo message is missing or is not a regular file.'
}

stop_pressure() {
  local unit=$1
  if systemctl cat "$unit" >/dev/null 2>&1; then
    systemctl stop "$unit"
  fi
}

restore_file() {
  require_volume
  if [[ -e /srv/demo/message.txt.saved || -L /srv/demo/message.txt.saved ]]; then
    [[ -f /srv/demo/message.txt.saved && ! -L /srv/demo/message.txt.saved ]] ||
      fail 'The saved message is not a regular file. Inspect it before continuing.'
    [[ ! -e /srv/demo/message.txt && ! -L /srv/demo/message.txt ]] ||
      fail 'Both message and saved message exist. Refusing to overwrite either.'
    mv -T /srv/demo/message.txt.saved /srv/demo/message.txt
  fi
}

restore_disk() {
  require_volume
  rm -f /srv/demo/.labstack-demo-fill
}

start_service() {
  systemctl start demo-fastapi
  curl -fs --retry 5 --retry-connrefused --retry-delay 1 http://127.0.0.1:8080/healthz >/dev/null ||
    fail 'FastAPI did not become healthy. Inspect journalctl -u demo-fastapi.'
}

case "$1" in
  status)
    for unit in demo-fastapi srv-demo.mount labstack-demo-cpu labstack-demo-memory labstack-holmes-bridge; do
      printf '%-24s %s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || true)"
    done
    if mountpoint -q /srv/demo; then
      df -h /srv/demo
    else
      printf 'Demo volume: unmounted\n'
    fi
    free -m
    curl -sS --max-time 3 -o /dev/null -w 'API HTTP status: %{http_code}\n' \
      http://127.0.0.1:8080/api/check || true
    ;;
  fault:service) systemctl stop demo-fastapi ;;
  restore:service) start_service ;;
  fault:permissions)
    require_message
    chmod 000 /srv/demo/message.txt
    ;;
  restore:permissions)
    require_message
    chmod 0644 /srv/demo/message.txt
    ;;
  fault:file)
    require_message
    [[ ! -e /srv/demo/message.txt.saved && ! -L /srv/demo/message.txt.saved ]] ||
      fail 'A saved message already exists. Restore or inspect it first.'
    mv -T /srv/demo/message.txt /srv/demo/message.txt.saved
    ;;
  restore:file) restore_file ;;
  fault:disk)
    require_volume
    [[ ! -e /srv/demo/.labstack-demo-fill && ! -L /srv/demo/.labstack-demo-fill ]] ||
      fail 'The disk fault is already present. Run task restore:disk first.'
    read -r size available < <(df -B1 --output=size,avail /srv/demo | tail -n 1)
    bytes=$((available - size / 20))
    ((bytes > 0)) || fail 'The demo volume already has less than 5% available space.'
    fallocate -l "$bytes" /srv/demo/.labstack-demo-fill
    sync /srv/demo/.labstack-demo-fill
    ;;
  restore:disk) restore_disk ;;
  fault:mount) systemctl stop srv-demo.mount ;;
  restore:mount) systemctl start srv-demo.mount; require_volume ;;
  fault:cpu)
    systemd-run --collect --unit=labstack-demo-cpu --property=RuntimeMaxSec=120s \
      --property=Nice=10 --property=OOMScoreAdjust=1000 \
      /usr/bin/stress-ng --cpu "$(nproc)" --timeout 120s --quiet
    ;;
  restore:cpu) stop_pressure labstack-demo-cpu ;;
  fault:memory)
    total=$(awk '/^MemTotal:/ {printf "%.0f\n", $2 * 1024}' /proc/meminfo)
    available=$(awk '/^MemAvailable:/ {printf "%.0f\n", $2 * 1024}' /proc/meminfo)
    reserve=$((total * 8 / 100))
    ((reserve >= 268435456)) || reserve=268435456
    bytes=$((available - reserve))
    ((bytes > 134217728)) || fail 'Not enough available memory for a controlled pressure test.'
    # Reserve headroom and make this bounded job the first OOM victim if demand changes.
    systemd-run --collect --unit=labstack-demo-memory --property=RuntimeMaxSec=120s \
      --property="MemoryMax=$((bytes + 134217728))" --property=MemorySwapMax=0 \
      --property=OOMScoreAdjust=1000 --property=Nice=10 \
      /usr/bin/stress-ng --vm 1 --vm-bytes "$bytes" --vm-keep --vm-populate \
      --vm-method write64 --vm-hang 0 --timeout 120s --quiet
    ;;
  restore:memory) stop_pressure labstack-demo-memory ;;
  restore)
    stop_pressure labstack-demo-cpu
    stop_pressure labstack-demo-memory
    systemctl start srv-demo.mount
    restore_disk
    restore_file
    require_message
    chmod 0644 /srv/demo/message.txt
    start_service
    curl -fsS http://127.0.0.1:8080/api/check >/dev/null
    ;;
  *) fail "Unknown demo task: $1" ;;
esac
