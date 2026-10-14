#!/usr/bin/env bash
set -euo pipefail
export KUBECONFIG=/etc/kubernetes/admin.conf
readonly node_count="${NODE_COUNT:?Node count is required}"

kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.9.0/components.yaml
if ! kubectl -n kube-system get deployment metrics-server \
  -o jsonpath='{.spec.template.spec.containers[0].args}' | grep -q -- --kubelet-insecure-tls; then
  # These guests use self-signed kubelet certificates.
  kubectl -n kube-system patch deployment metrics-server --type=json \
    -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
fi
kubectl apply -f \
  https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.37/deploy/local-path-storage.yaml
cat <<'EOF' | kubectl apply -f -
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: standard
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: rancher.io/local-path
volumeBindingMode: WaitForFirstConsumer
reclaimPolicy: Delete
EOF
kubectl annotate storageclass local-path storageclass.kubernetes.io/is-default-class- || true

test "$(kubectl get nodes -o name | wc -l)" -eq "$node_count"
kubectl wait --for=condition=Ready nodes --all --timeout=300s
for deployment in coredns metrics-server; do
  kubectl -n kube-system rollout status "deployment/$deployment" --timeout=300s
done
kubectl -n local-path-storage rollout status deployment/local-path-provisioner --timeout=300s
