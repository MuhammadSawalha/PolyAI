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

# --- wait for at least one worker node to exist before installing anything ---
# The control plane is tainted NoSchedule, so nothing but DaemonSets can run
# there - both Calico's kube-controllers Deployment and the whole ArgoCD
# stack need an actual worker. The worker boots in parallel with (not after)
# the control plane, so wait for its Node object to register instead of
# racing it.
#
# Deliberately checking for the Node object's existence, NOT status=Ready:
# no node (control plane included) can report Ready until Calico's CNI is
# installed, which is the very next step below - requiring Ready here would
# wait on a condition this script hasn't made possible yet.
echo "Waiting for at least one worker node to register..."
for i in $(seq 1 60); do
  if kubectl get nodes -l '!node-role.kubernetes.io/control-plane' --no-headers 2>/dev/null | grep -q .; then
    echo "Worker node has registered."
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "Timed out waiting for a worker node to join." >&2
    exit 1
  fi
  echo "No worker node yet, retrying in 10s... ($i/60)"
  sleep 10
done

# --- Calico CNI ---
kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.28.0/manifests/calico.yaml

# --- metrics-server ---
# Required for the agent/frontend/yolo HPAs to read CPU% - without it every
# HPA sits at FailedGetResourceMetric forever, which ArgoCD reports as the
# whole Application being Degraded even though the underlying Deployment is
# healthy. kubeadm clusters need the --kubelet-insecure-tls patch since
# kubelet's self-signed serving certs aren't in a chain metrics-server trusts
# by default. The patch itself isn't idempotent (kubectl patch --type=json
# "add" would append a duplicate arg on every re-run), so it's guarded by a
# check for whether the flag is already there.
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml

if ! kubectl get deployment metrics-server -n kube-system -o jsonpath='{.spec.template.spec.containers[0].args}' \
    | grep -q "kubelet-insecure-tls"; then
  kubectl -n kube-system patch deployment metrics-server --type=json \
    -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
fi

# --- Nginx Ingress Controller ---
# baremetal provider manifest -> the Service comes up as NodePort (no cloud
# LB controller here to provision one for us). The nodePorts it's assigned
# start out random; pin them to the fixed values infra/tf's ALB target group
# is built to point at (var.ingress_http_node_port, currently 30080) so the
# target group doesn't need to be re-pointed by hand after every install.
# `kubectl patch` without --type defaults to a strategic merge, which for
# Service.spec.ports merges list entries by their "port" key - safe to re-run
# regardless of the two ports' order in the upstream manifest.
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.11.3/deploy/static/provider/baremetal/deploy.yaml
kubectl -n ingress-nginx patch svc ingress-nginx-controller -p \
  '{"spec":{"ports":[{"port":80,"nodePort":30080},{"port":443,"nodePort":30443}]}}'
kubectl -n ingress-nginx rollout status deployment/ingress-nginx-controller --timeout=300s

# --- Namespaces + StorageClass (dev/prod) ---
# infra/k8s/README.md Step 6 - non-recursive on purpose: dev/ and prod/ each
# hold only namespace.yaml and storageclass.yaml directly (agent/yolo/frontend
# now live in their own subfolders and are managed by ArgoCD instead, applied
# via app-of-apps below). Must happen before kube-prometheus-stack's PVCs
# (storageClassName: ebs-sc) are created further down.
kubectl apply -f "$SCRIPT_DIR/dev/"
kubectl apply -f "$SCRIPT_DIR/prod/"

# --- EBS CSI driver ---
# infra/k8s/README.md Step 3 - cluster add-on infrastructure, not one of "your
# services". The worker/control-plane IAM roles already carry
# AmazonEBSCSIDriverPolicy (infra/tf/modules/k8s-cluster); this installs the
# actual driver software that uses it. Without it, ebs-sc PVCs (Prometheus,
# Grafana) stay Pending forever and the kube-prometheus-stack Helm install
# below times out waiting on them.
kubectl apply -k "github.com/kubernetes-sigs/aws-ebs-csi-driver/deploy/kubernetes/overlays/stable/?ref=release-1.35"
kubectl -n kube-system rollout status deployment/ebs-csi-controller --timeout=300s

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

# --- kube-prometheus-stack (Prometheus + Grafana + Alertmanager) ---
# task7.md Part II. Installed once, cluster-wide, into the `monitoring`
# namespace via Helm directly (not an ArgoCD Application, same as ArgoCD and
# ingress-nginx above) - see infra/k8s/monitoring/values.yaml for why this one
# isn't duplicated per dev/prod like everything else.
#
# SNS_TOPIC_ARN is a Terraform output (module.monitoring, infra/tf output
# sns_topic_arn) - unknowable at commit time, so it's threaded into the
# Alertmanager SNS receiver via --set instead of being baked into the
# committed values.yaml. .github/workflows/cluster.yaml exports it
# automatically before invoking this script; running by hand, export it
# yourself first: `export SNS_TOPIC_ARN=$(terraform -chdir=infra/tf output -raw sns_topic_arn)`.
: "${SNS_TOPIC_ARN:?SNS_TOPIC_ARN must be set before running bootstrap.sh - see the comment above this line}"
SNS_TOPIC_REGION="$(echo "$SNS_TOPIC_ARN" | cut -d: -f4)"

if ! command -v helm >/dev/null 2>&1; then
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update prometheus-community

helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  -f "$SCRIPT_DIR/monitoring/values.yaml" \
  --set alertmanager.config.receivers[0].sns_configs[0].topic_arn="$SNS_TOPIC_ARN" \
  --set alertmanager.config.receivers[0].sns_configs[0].sigv4.region="$SNS_TOPIC_REGION" \
  --wait --timeout 10m

# The ServiceMonitor/PrometheusRule CRDs these depend on only exist once the
# chart above has installed them, so these must come after, not before.
kubectl apply -f "$SCRIPT_DIR/monitoring/grafana-dashboard-agent.yaml"
kubectl apply -f "$SCRIPT_DIR/monitoring/ingress-nginx-metrics.yaml"
kubectl apply -f "$SCRIPT_DIR/monitoring/prometheus-rules.yaml"
kubectl apply -f "$SCRIPT_DIR/monitoring/grafana-ingress.yaml"
kubectl apply -f "$SCRIPT_DIR/monitoring/prometheus-ingress.yaml"

# --- Cluster Autoscaler (task7.md Part III bonus) ---
# Installed the same way as kube-prometheus-stack: Helm directly, into
# kube-system, not an ArgoCD Application. No IRSA (not EKS) - it
# authenticates through the worker node's own EC2 instance profile
# (infra/tf/modules/autoscaler), same pattern as Alertmanager's SNS publish.
#
# CLUSTER_NAME/AWS_REGION are Terraform outputs (module.autoscaler's
# discovery tags live on the worker ASG; infra/tf output cluster_name) -
# unknowable at commit time, so they're threaded into the Helm install via
# --set instead of being baked into the committed values.yaml, same as
# SNS_TOPIC_ARN above. .github/workflows/cluster.yaml exports both
# automatically before invoking this script; running by hand, export them
# yourself first:
#   export CLUSTER_NAME=$(terraform -chdir=infra/tf output -raw cluster_name)
#   export AWS_REGION=<the region you provisioned the cluster into>
: "${CLUSTER_NAME:?CLUSTER_NAME must be set before running bootstrap.sh - see the comment above this line}"
: "${AWS_REGION:?AWS_REGION must be set before running bootstrap.sh - see the comment above this line}"

helm repo add autoscaler https://kubernetes.github.io/autoscaler
helm repo update autoscaler

helm upgrade --install cluster-autoscaler autoscaler/cluster-autoscaler \
  -n kube-system \
  -f "$SCRIPT_DIR/autoscaler/values.yaml" \
  --set autoDiscovery.clusterName="$CLUSTER_NAME" \
  --set awsRegion="$AWS_REGION" \
  --wait --timeout 5m

echo "ArgoCD initial admin password:"
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 --decode
echo
