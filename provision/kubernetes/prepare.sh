#!/usr/bin/env bash
set -euo pipefail

readonly version="${KUBERNETES_VERSION:?Kubernetes version is required}"
readonly node_ip="${NODE_IP:?Node IP is required}"
readonly marker="/var/lib/labstack/prepared"
if [[ -f "$marker" ]] && [[ "$(cat "$marker")" == "$version" ]] && command -v crictl >/dev/null; then
  exit 0
fi
export DEBIAN_FRONTEND=noninteractive

apt_get() {
  apt-get -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3 "$@"
}

swapoff -a
sed -ri '/\sswap\s/s/^#?/#/' /etc/fstab
cat >/etc/modules-load.d/kubernetes.conf <<'EOF'
overlay
br_netfilter
EOF
modprobe overlay
modprobe br_netfilter
cat >/etc/sysctl.d/99-kubernetes.conf <<'EOF'
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1
fs.inotify.max_user_instances = 512
EOF
sysctl --system >/dev/null

apt_get update -qq
apt_get install -y -qq conntrack containerd iptables socat

install -d -m 0755 /etc/containerd
containerd config default >/etc/containerd/config.toml
# Kubelet and containerd must use the same cgroup manager.
sed -ri 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
# Debian's runtime defaults to /usr/lib/cni, but Kubernetes and Cilium install here.
sed -ri 's@bin_dir = "/usr/lib/cni"@bin_dir = "/opt/cni/bin"@' /etc/containerd/config.toml
systemctl enable --now containerd
systemctl restart containerd

install -d -m 0755 /etc/apt/keyrings
curl --retry 3 -fsSL https://pkgs.k8s.io/core:/stable:/v1.37/deb/Release.key \
  | gpg --dearmor --yes -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
cat >/etc/apt/sources.list.d/kubernetes.list <<'EOF'
deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.37/deb/ /
EOF
apt_get update -qq
apt_get install -y -qq "kubeadm=$version-1.1" "kubelet=$version-1.1" "kubectl=$version-1.1" cri-tools
apt-mark hold kubeadm kubelet kubectl cri-tools
systemctl enable kubelet

# Both NICs have addresses. The kubelet must announce the private one.
printf 'KUBELET_EXTRA_ARGS=--node-ip=%s\n' "$node_ip" >/etc/default/kubelet
cat >/etc/crictl.yaml <<'EOF'
runtime-endpoint: unix:///run/containerd/containerd.sock
image-endpoint: unix:///run/containerd/containerd.sock
timeout: 10
debug: false
EOF
cat >/etc/profile.d/kubernetes.sh <<'EOF'
if [[ -n "${BASH_VERSION:-}" && $- == *i* ]]; then
  source /usr/share/bash-completion/bash_completion
  source <(kubectl completion bash)
  alias k=kubectl
  complete -o default -F __start_kubectl k
fi
EOF
install -d -m 0755 "$(dirname "$marker")"
printf '%s\n' "$version" >"$marker"
