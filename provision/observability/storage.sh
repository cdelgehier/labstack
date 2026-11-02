#!/usr/bin/env bash
set -euo pipefail

# Use a separate, small filesystem for disk faults instead of filling the root disk.
install -d -m 0755 /var/lib/labstack-storage /srv/demo /var/lib/labstack-metrics
if [[ ! -e /var/lib/labstack-storage/data.ext4 ]]; then
  truncate -s 256M /var/lib/labstack-storage/data.ext4
  mkfs.ext4 -q /var/lib/labstack-storage/data.ext4
fi
cat >/etc/systemd/system/srv-demo.mount <<'EOF'
[Unit]
Description=Labstack demo data volume
[Mount]
What=/var/lib/labstack-storage/data.ext4
Where=/srv/demo
Type=ext4
Options=loop,nodev,nosuid,noexec
[Install]
WantedBy=multi-user.target
EOF
cat >/var/lib/labstack-metrics/mounts.prom <<'EOF'
# HELP labstack_expected_mount Filesystems that must stay mounted in this lab.
# TYPE labstack_expected_mount gauge
labstack_expected_mount{mountpoint="/srv/demo"} 1
EOF
chmod 0644 /var/lib/labstack-metrics/mounts.prom
systemctl daemon-reload
systemctl enable --now srv-demo.mount
