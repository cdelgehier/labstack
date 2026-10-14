#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# Keep common guest tools separate from container runtimes and cluster packages.
apt-get -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3 update -qq
apt-get -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3 install -y -qq \
  bash-completion ca-certificates curl dnsutils gpg jq rsync vim
