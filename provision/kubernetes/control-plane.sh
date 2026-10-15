#!/usr/bin/env bash
set -euo pipefail
readonly version="${KUBERNETES_VERSION:?Kubernetes version is required}"
readonly node_ip="${NODE_IP:?Node IP is required}"
readonly shared_dir="${SHARED_DIR:?Shared directory is required}"
readonly ssh_user="${SSH_USER:?SSH user is required}"
# Images can use another login or primary group. Keep admin access on that account.
ssh_home="$(getent passwd "$ssh_user" | cut -d: -f6)"
ssh_group="$(id -gn "$ssh_user")"
test -n "$ssh_home"

if [[ ! -f /etc/kubernetes/admin.conf ]]; then
  # The Mac reaches the API through a localhost port forward. Keep TLS validation enabled.
  kubeadm init \
    --kubernetes-version="v$version" \
    --apiserver-advertise-address="$node_ip" \
    --apiserver-cert-extra-sans=127.0.0.1,localhost \
    --pod-network-cidr=10.244.0.0/16 \
    --service-cidr=10.96.0.0/12 \
    --skip-token-print
fi
install -d -m 0700 -o "$ssh_user" -g "$ssh_group" "$ssh_home/.kube"
install -m 0600 -o "$ssh_user" -g "$ssh_group" /etc/kubernetes/admin.conf "$ssh_home/.kube/config"
install -d -m 0700 /root/.kube
install -m 0600 /etc/kubernetes/admin.conf /root/.kube/config

# Refresh the short-lived join token on retries. The private shared directory carries
# it to workers without giving them the Mac's SSH key or the admin kubeconfig.
umask 077
kubeadm token create --ttl=30m --print-join-command >"$shared_dir/join-command"
