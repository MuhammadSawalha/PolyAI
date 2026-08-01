#!/bin/bash
# One-time cluster bootstrap (task6.md Part II), run on the control-plane node
# after `terraform apply` has already initialized it via kubeadm. Safe to
# re-run - every step here is idempotent (`kubectl apply` always is; the
# namespace check just skips creation if it's already there).
#
# Usage:
#   - Manually: SSH into the control plane, clone/copy this repo, run
#     `infra/k8s/bootstrap.sh` from the repo root (or anywhere - it locates
#     its sibling infra/k8s/argo/app-of-apps.yaml relative to its own path).
#   - From CI: .github/workflows/cluster.yaml scp's this script and
#     infra/k8s/argo/app-of-apps.yaml over (preserving that same relative
#     layout) and runs it via SSH.
set -euo pipefail

export KUBECONFIG=/home/ubuntu/.kube/config
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Calico CNI ---
kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.28.0/manifests/calico.yaml

# --- ArgoCD ---
kubectl get namespace argocd || kubectl create namespace argocd
kubectl apply -n argocd --server-side --force-conflicts \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl -n argocd rollout status deployment/argocd-server --timeout=300s

# --- App of apps ---
# This is the only Application object we create by hand. It points ArgoCD at
# infra/k8s/argo/ in the repo, so ArgoCD's own reconciliation loop picks up
# every other Application (yolo/agent/frontend/img-proc-mcp, dev+prod)
# directly from git - nothing else ever needs to be applied here, now or for
# any future microservice.

kubectl apply -f "$SCRIPT_DIR/argo/app-of-apps.yaml"

echo "ArgoCD initial admin password:"
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 --decode
echo
