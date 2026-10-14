#!/usr/bin/env bash
set -euo pipefail
export KUBECONFIG=/etc/kubernetes/admin.conf
readonly node_ip="${NODE_IP:?Control plane IP is required}"
readonly helm_version=3.22.0
readonly helm_sha256=f14e804dfee240f55525b667488fe9adca349e63e00c9af634c0beb1421ac310
readonly cilium_version=1.20.2
readonly cilium_sha256=b2afd87b7f75f875f92a14559f14f59b7babbb479d968e3fd625a20bf30ec20e
temporary_directory="$(mktemp -d)"
readonly temporary_directory
trap 'rm -rf "$temporary_directory"' EXIT

# Use a checked guest tool so provisioning does not depend on the Mac's Helm version.
curl --retry 3 --fail --silent --show-error --location \
  "https://get.helm.sh/helm-v$helm_version-linux-arm64.tar.gz" \
  --output "$temporary_directory/helm.tar.gz"
printf '%s  %s\n' "$helm_sha256" "$temporary_directory/helm.tar.gz" | sha256sum --check -
tar -xzf "$temporary_directory/helm.tar.gz" -C "$temporary_directory" linux-arm64/helm

curl --retry 3 --fail --silent --show-error --location \
  "https://helm.cilium.io/cilium-$cilium_version.tgz" \
  --output "$temporary_directory/cilium.tgz"
printf '%s  %s\n' "$cilium_sha256" "$temporary_directory/cilium.tgz" | sha256sum --check -

# Kubeadm assigns each node's PodCIDR. The default Cilium pool overlaps the NAT
# and service networks. Kubernetes IPAM keeps our separate 10.244.0.0/16 network.
# The private API address also avoids depending on service routing during startup.
"$temporary_directory/linux-arm64/helm" upgrade --install cilium \
  "$temporary_directory/cilium.tgz" --namespace kube-system \
  --set ipam.mode=kubernetes \
  --set routingMode=tunnel \
  --set tunnelProtocol=vxlan \
  --set kubeProxyReplacement=false \
  --set operator.replicas=1 \
  --set "k8sServiceHost=$node_ip" \
  --set k8sServicePort=6443 \
  --wait --timeout 10m
kubectl -n kube-system rollout status daemonset/cilium --timeout=300s
kubectl -n kube-system rollout status deployment/cilium-operator --timeout=300s
