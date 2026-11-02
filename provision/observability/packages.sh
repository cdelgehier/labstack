#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# The common recipe refreshes APT lists before this script. Keep the native dpkg wait.
apt-get -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3 install -y --no-install-recommends \
  apache2 docker.io docker-compose-v2 prometheus-node-exporter e2fsprogs
systemctl enable --now docker
# The demo runs a separate Apache unit and configuration.
systemctl disable --now apache2
