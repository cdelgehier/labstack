#!/usr/bin/env bash
set -euo pipefail
readonly shared_dir="${SHARED_DIR:?Shared directory is required}"
readonly primary_ip="${PRIMARY_NODE_IP:?Primary node IP is required}"

if [[ -f /etc/kubernetes/kubelet.conf ]]; then
  exit 0
fi

# Accept only the pinned API endpoint and pass arguments directly to kubeadm.
# Do not evaluate a shell command from the shared file or print its token.
read -r -a join_command <"$shared_dir/join-command"
if [[ "${join_command[0]:-}" != kubeadm || "${join_command[1]:-}" != join \
  || "${join_command[2]:-}" != "$primary_ip:6443" ]]; then
  echo 'The control plane returned an unexpected join command.' >&2
  exit 1
fi
"${join_command[@]}"
