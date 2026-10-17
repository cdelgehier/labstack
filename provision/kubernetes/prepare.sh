#!/usr/bin/env bash
set -euo pipefail

readonly version="${KUBERNETES_VERSION:?Kubernetes version is required}"
readonly node_ip="${NODE_IP:?Node IP is required}"
readonly marker="/var/lib/labstack/prepared"
if [[ -f "$marker" ]] && [[ "$(cat "$marker")" == "$version" ]] && command -v crictl >/dev/null; then
  exit 0
fi
export DEBIAN_FRONTEND=noninteractive

# Keep this helper in each standalone recipe: guests receive one script at a time.
apt_run() (
  # A subshell keeps temporary-file and signal traps local to this command.
  local command="$1"
  shift
  local output deadline remaining=300 delay status
  local -a pipeline_status
  output=$(mktemp)
  trap 'rm -f "$output"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  deadline=$((SECONDS + remaining))

  while true; do
    # Native dpkg waits and our retries use the same budget. Lists and archives
    # do not use dpkg's timeout, and apt-mark can fail directly inside dpkg.
    if LC_ALL=C "$command" -o DPkg::Lock::Timeout="$remaining" -o Acquire::Retries=3 "$@" \
      2>&1 | tee "$output" >&2; then
      return 0
    else
      pipeline_status=("${PIPESTATUS[@]}")
      status=${pipeline_status[0]}
      # A log capture failure must also stop provisioning.
      if ((status == 0)); then
        return "${pipeline_status[1]}"
      fi
    fi
    # Permission errors and broken packages must fail, even with exit code 100.
    if ! grep -Eq \
      'Could not get lock .*(It is held by process [0-9]+|Resource temporarily unavailable)|dpkg: error: dpkg (frontend|database) lock was locked by (another process|.+ process with pid [0-9]+)' \
      "$output"; then
      return "$status"
    fi
    remaining=$((deadline - SECONDS))
    if ((remaining <= 0)); then
      break
    fi
    delay=5
    if ((remaining < delay)); then
      delay=$remaining
    fi
    printf 'Waiting for APT lock (%s): retrying in %ss (%ss remaining).\n' \
      "$command" "$delay" "$remaining" >&2
    sleep "$delay"
    remaining=$((deadline - SECONDS))
    if ((remaining <= 0)); then
      break
    fi
  done
  printf 'Timed out waiting for APT lock (%s) after 300s.\n' "$command" >&2
  return "$status"
)

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

# Keep native dpkg lock messages visible; -qq would hide them while APT waits.
apt_run apt-get update -q
apt_run apt-get install -y -q conntrack containerd iptables socat

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
apt_run apt-get update -q
apt_run apt-get install -y -q "kubeadm=$version-1.1" "kubelet=$version-1.1" "kubectl=$version-1.1" cri-tools
apt_run apt-mark hold kubeadm kubelet kubectl cri-tools
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
