"""Check the running environment and retain logs even when a check fails."""

import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from labstack.inventory import nodes, primary_node
from labstack.kubernetes import prepare_client
from labstack.logger import get_logger
from labstack.models.machine import Node
from labstack.models.state import State
from labstack.settings import KUBERNETES_VERSION, get_settings
from labstack.ssh import remote, ssh_command

logger = get_logger(__name__)

GUEST_CHECK = r"""set -euo pipefail
. /etc/os-release
test "$ID" = ubuntu
test "$VERSION_ID" = 26.04
test "$(uname -m)" = aarch64
test "$(id -un)" = ubuntu
sudo -n true
sudo cloud-init status --wait
test "$(lsblk -bndo SIZE /dev/vda)" -eq "$(( $1 * 1024 * 1024 * 1024 ))"
test "$(df --output=size -B1 / | tail -1)" -ge "$(( ($1 - 4) * 1024 * 1024 * 1024 ))"
dpkg-query -W openssh-server sudo python3 rsync cloud-init qemu-guest-agent
test "$(wc -l < ~/.ssh/authorized_keys)" -eq 1
"""

WORKLOAD_CHECK = r"""set -euo pipefail
namespace="$1"
control_plane="$2"
worker="$3"
kubectl create namespace "$namespace"
cat <<YAML | kubectl -n "$namespace" apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: web
  labels: {app: web}
spec:
  nodeSelector: {kubernetes.io/hostname: "$control_plane"}
  tolerations:
    - key: node-role.kubernetes.io/control-plane
      operator: Exists
      effect: NoSchedule
  containers:
    - name: web
      image: nginx:stable-alpine
---
apiVersion: v1
kind: Service
metadata: {name: web}
spec:
  selector: {app: web}
  ports: [{port: 80, targetPort: 80}]
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: data}
spec:
  storageClassName: standard
  accessModes: [ReadWriteOnce]
  resources: {requests: {storage: 64Mi}}
---
apiVersion: v1
kind: Pod
metadata: {name: client}
spec:
  nodeSelector: {kubernetes.io/hostname: "$worker"}
  containers:
    - name: client
      image: busybox:stable
      command: [sleep, '1800']
      volumeMounts: [{name: data, mountPath: /data}]
  volumes:
    - name: data
      persistentVolumeClaim: {claimName: data}
YAML
kubectl -n "$namespace" wait --for=condition=Ready pods --all --timeout=300s
kubectl -n "$namespace" exec client -- sh -ec "
  nslookup web.$namespace.svc.cluster.local
  wget -qO- http://web.$namespace.svc.cluster.local | grep -q 'Welcome to nginx'
  wget -T 20 -qO /dev/null https://example.com
  echo storage-check > /data/check"
kubectl -n "$namespace" delete pod client --wait=true --timeout=120s
cat <<YAML | kubectl -n "$namespace" apply -f -
apiVersion: v1
kind: Pod
metadata: {name: client}
spec:
  nodeSelector: {kubernetes.io/hostname: "$worker"}
  containers:
    - name: client
      image: busybox:stable
      command: [sleep, '1800']
      volumeMounts: [{name: data, mountPath: /data}]
  volumes:
    - name: data
      persistentVolumeClaim: {claimName: data}
YAML
kubectl -n "$namespace" wait --for=condition=Ready pod/client --timeout=300s
kubectl -n "$namespace" exec client -- grep -qx storage-check /data/check
for peer in "${@:4}"; do
  kubectl -n "$namespace" run "peer-$peer" --image=busybox:stable --restart=Never \
    --overrides="{\"spec\":{\"nodeName\":\"$peer\"}}" --command -- sleep 300
  kubectl -n "$namespace" wait --for=condition=Ready "pod/peer-$peer" --timeout=300s
  kubectl -n "$namespace" exec "peer-$peer" -- sh -ec "
    nslookup web.$namespace.svc.cluster.local
    wget -qO- http://web.$namespace.svc.cluster.local | grep -q 'Welcome to nginx'"
done
"""

CILIUM_CHECK = r"""set -euo pipefail
kubectl -n kube-system rollout status daemonset/cilium --timeout=300s
kubectl -n kube-system rollout status deployment/cilium-operator --timeout=300s
kubectl -n kube-system get daemonset cilium -o json | jq -e --argjson count "$1" '
  .status.desiredNumberScheduled == $count and
  .status.updatedNumberScheduled == $count and .status.numberReady == $count'
for pod in $(kubectl -n kube-system get pods -l k8s-app=cilium -o name); do
  kubectl -n kube-system exec "$pod" -c cilium-agent -- cilium-dbg status --brief
done
"""

POLICY_CHECK = r"""set -euo pipefail
namespace="$1"
worker="$2"
web_ip="$(kubectl -n "$namespace" get service/web -o jsonpath='{.spec.clusterIP}')"

probe_http() {
  # The guest command reports the network result with a successful exit. A failed
  # kubectl exec is a transport failure, which must never count as policy denial.
  kubectl -n "$namespace" exec "$1" -- sh -c '
    if wget -T 3 -qO /dev/null "http://$1"; then
      printf ALLOWED
    else
      printf DENIED
    fi' -- "$web_ip"
}

wait_http() {
  local client="$1" expected="$2" observed=""
  # Policy changes reach agents asynchronously. Bound the wait instead of using
  # one fixed sleep, so a slow agent does not make an otherwise healthy test fail.
  for attempt in {1..30}; do
    observed="$(probe_http "$client")" || return
    if [[ "$observed" == "$expected" ]]; then
      printf '%s: %s\n' "$client" "$observed"
      return
    fi
    sleep 2
  done
  printf 'Expected %s from %s, got %s\n' "$expected" "$client" "$observed" >&2
  return 1
}

kubectl -n "$namespace" label pod/client app=policy-client
cat <<YAML | kubectl -n "$namespace" apply -f -
apiVersion: v1
kind: Pod
metadata: {name: excluded}
spec:
  nodeSelector: {kubernetes.io/hostname: "$worker"}
  containers:
    - name: excluded
      image: busybox:stable
      command: [sleep, '1800']
YAML
kubectl -n "$namespace" wait --for=condition=Ready pod/excluded --timeout=300s
# First prove that both clients can reach the same numeric service address. This
# prevents an existing DNS or connectivity failure from passing the denial check.
wait_http client ALLOWED
wait_http excluded ALLOWED
cat <<'YAML' | kubectl -n "$namespace" apply -f -
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: deny-web}
spec:
  podSelector: {matchLabels: {app: web}}
  policyTypes: [Ingress]
  ingress: []
YAML
wait_http client DENIED
wait_http excluded DENIED
cat <<'YAML' | kubectl -n "$namespace" apply -f -
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: allow-client}
spec:
  podSelector: {matchLabels: {app: web}}
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector: {matchLabels: {app: policy-client}}
      ports: [{protocol: TCP, port: 80}]
YAML
wait_http client ALLOWED
wait_http excluded DENIED
echo 'NetworkPolicy checks passed'
"""


def check_environment(state: State) -> None:
    """Verify guest isolation and, when selected, the installed Kubernetes recipe.

    Saved state decides whether Kubernetes checks apply. Infrastructure-only guests
    still need working SSH, cloud-init, distinct identities and private networking.
    Workload checks use one temporary namespace to exercise DNS, cross-node traffic,
    storage and policy enforcement. A network denial is checked separately from API
    exec failure. Diagnostics are retained for every guest, including Cilium status,
    and that namespace is removed after success or failure.
    """
    settings = get_settings()
    directory = settings.logs_dir / time.strftime("test-%Y%m%dT%H%M%SZ", time.gmtime())
    directory.mkdir(mode=0o700)
    namespace = "smoke-" + str(time.time_ns())
    cp = primary_node(state.config)
    identities = set()
    host_keys = set()
    try:
        for node in nodes(state.config):
            logger.info("Checking Ubuntu, SSH, disk and networking.", node=node.name)
            output = remote(node, f"bash -s -- {node.disk_gib}", GUEST_CHECK)
            (directory / f"{node.name}-checks.log").write_text(output)
            identity = remote(node, "cat /etc/machine-id").strip()
            host_key = ssh_key_identity(
                remote(node, "sudo cat /etc/ssh/ssh_host_ed25519_key.pub")
            )
            if not re.fullmatch(r"[a-f0-9]{32}", identity):
                raise ValueError(f"Invalid machine identity on {node.name}.")
            identities.add(identity)
            host_keys.add(host_key)
            for peer in nodes(state.config):
                if node != peer:
                    remote(node, f"ping -c 2 -W 5 {peer.address}")
            if state.kubernetes_enabled:
                remote(
                    node,
                    f"set -e; test $(kubeadm version -o short) = v{KUBERNETES_VERSION}; "
                    f"test \"$(kubelet --version)\" = 'Kubernetes v{KUBERNETES_VERSION}'; "
                    "systemctl is-active containerd kubelet; "
                    "sudo crictl info | jq -e "
                    "'.config.containerd.runtimes.runc.options.SystemdCgroup == true'; "
                    'test -z "$(swapon --noheadings)"',
                )
        count = len(nodes(state.config))
        if len(identities) != count or len(host_keys) != count:
            raise ValueError("Nodes share a machine identity or SSH host key.")
        if state.kubernetes_enabled:
            result = json.loads(remote(cp, "kubectl get nodes -o json"))
            expected = {node.name for node in nodes(state.config)}
            actual = {item["metadata"]["name"] for item in result["items"]}
            if actual != expected:
                raise ValueError(f"Unexpected Kubernetes nodes: {actual}")
            for item in result["items"]:
                if (
                    item["status"]["nodeInfo"]["kubeletVersion"]
                    != f"v{KUBERNETES_VERSION}"
                ):
                    raise ValueError("A node uses a different kubelet version.")
            remote(
                cp,
                "set -e; kubectl wait --for=condition=Ready nodes --all --timeout=300s; "
                "kubectl top nodes",
            )
            cilium = (
                "provision/kubernetes/cilium.sh"
                in state.config.machines[cp.name].scripts
            )
            if cilium:
                output = remote(cp, f"bash -s -- {count}", CILIUM_CHECK)
                (directory / "cilium-checks.log").write_text(output)
            subprocess.run(
                [
                    str(prepare_client()),
                    f"--kubeconfig={settings.root / 'kubeconfig'}",
                    "get",
                    "nodes",
                ],
                check=True,
            )
            logger.info("Checking cross-node DNS, HTTP, Internet access and storage.")
            workers = [node for node in nodes(state.config) if node.name != cp.name]
            if not workers:
                raise ValueError("Workload checks require at least one worker.")
            command = shlex.join(
                [
                    "bash",
                    "-s",
                    "--",
                    namespace,
                    cp.name,
                    *[worker.name for worker in workers],
                ]
            )
            output = remote(cp, command, WORKLOAD_CHECK)
            (directory / "workloads.log").write_text(output)
            if cilium:
                logger.info("Checking NetworkPolicy denial and selected client access.")
                command = shlex.join(["bash", "-s", "--", namespace, workers[0].name])
                output = remote(cp, command, POLICY_CHECK)
                (directory / "network-policy.log").write_text(output)
    finally:
        # Cleanup errors must not replace the original test failure. Diagnostics are
        # collected for all guests even when the first check has already failed.
        failed = sys.exc_info()[0] is not None
        for node in nodes(state.config):
            save_diagnostic(
                node,
                "sudo cloud-init status --long; "
                "sudo journalctl -b -u ssh -u kubelet -u containerd --no-pager -n 200",
                directory / f"{node.name}-diagnostics.log",
            )
        if state.kubernetes_enabled:
            save_diagnostic(
                cp,
                "kubectl get nodes -o wide; kubectl get pods -A -o wide; "
                f"kubectl -n {namespace} get events --sort-by=.lastTimestamp",
                directory / "cluster.log",
            )
            if (
                "provision/kubernetes/cilium.sh"
                in state.config.machines[cp.name].scripts
            ):
                save_diagnostic(
                    cp,
                    "kubectl -n kube-system get daemonset/cilium deployment/cilium-operator; "
                    "for pod in $(kubectl -n kube-system get pods -l k8s-app=cilium -o name); do "
                    'kubectl -n kube-system exec "$pod" -c cilium-agent -- cilium-dbg status; '
                    "done; kubectl -n kube-system logs -l k8s-app=cilium "
                    "--all-containers --tail=100 --prefix --max-log-requests=10",
                    directory / "cilium.log",
                    timeout=60,
                )
            cleaned = save_diagnostic(
                cp,
                f"kubectl delete namespace {namespace} --ignore-not-found "
                "--wait=true --timeout=120s",
                directory / "cleanup.log",
                timeout=150,
            )
            if not cleaned and not failed:
                raise ValueError(
                    f"Test namespace cleanup failed. See {directory / 'cleanup.log'}."
                )
        logger.info("Runtime diagnostics saved.", path=str(directory))
    logger.info("Runtime checks passed.")


def save_diagnostic(node: Node, command: str, path: Path, timeout: int = 30) -> bool:
    """Save guest output and report failure without stopping diagnostic collection.

    A failed SSH call can itself be the evidence needed to explain a runtime failure.
    Other guests must still be inspected. The caller decides whether a false result
    is only missing diagnostic data or a failed cleanup that should fail the test.
    """
    with path.open("w") as stream:
        try:
            result = subprocess.run(
                ssh_command(node, command),
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as error:
            stream.write(f"Diagnostics failed: {error}\n")
            return False
    return result.returncode == 0


def ssh_key_identity(public_key: str) -> str:
    """Compare SSH key type and bytes without the optional hostname comment.

    Two copied keys can have different comments after guest setup. Comparing complete
    lines would then incorrectly treat those guests as having separate host keys.
    """
    fields = public_key.split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise ValueError("Invalid SSH host key.")
    return " ".join(fields[:2])
