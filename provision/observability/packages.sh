#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
: "${PROVISION_ASSETS_DIR:?Labstack must transfer the recipe assets}"

# The common recipe refreshes APT lists before this script. Keep the native dpkg wait.
apt-get -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3 install -y --no-install-recommends \
  docker.io docker-compose-v2 prometheus-node-exporter e2fsprogs python3-venv chrony stress-ng
bash "$PROVISION_ASSETS_DIR/install-demo-tasks.sh"
install -d -m 0755 /etc/chrony/conf.d
# A paused demo VM can resume far behind the host. Keep log and trace clocks aligned.
printf 'makestep 1 -1\n' >/etc/chrony/conf.d/labstack-demo.conf
chronyd -p >/dev/null
systemctl enable chrony
systemctl restart chrony
chronyc waitsync 30 0.1 0 1
systemctl enable --now docker
