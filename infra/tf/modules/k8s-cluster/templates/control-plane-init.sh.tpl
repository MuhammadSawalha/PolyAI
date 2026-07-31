#!/bin/bash
set -euo pipefail
exec > >(tee /var/log/polyai-init.log) 2>&1

KUBERNETES_VERSION="${kubernetes_version}"

# --- kubelet requires swap disabled ---
swapoff -a
sed -i '/ swap / s/^/#/' /etc/fstab

# --- kernel modules + sysctl required for pod networking ---
cat <<'EOF' > /etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF
modprobe overlay
modprobe br_netfilter

cat <<'EOF' > /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sysctl --system

# --- cri-o, kubelet, kubeadm, kubectl ---
apt-get update
apt-get install -y software-properties-common curl gpg apt-transport-https ca-certificates awscli

mkdir -p /etc/apt/keyrings

curl -fsSL "https://pkgs.k8s.io/core:/stable:/v$KUBERNETES_VERSION/deb/Release.key" | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v$KUBERNETES_VERSION/deb/ /" > /etc/apt/sources.list.d/kubernetes.list

curl -fsSL "https://download.opensuse.org/repositories/isv:/cri-o:/stable:/v$KUBERNETES_VERSION/deb/Release.key" | gpg --dearmor -o /etc/apt/keyrings/cri-o-apt-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/cri-o-apt-keyring.gpg] https://download.opensuse.org/repositories/isv:/cri-o:/stable:/v$KUBERNETES_VERSION/deb/ /" > /etc/apt/sources.list.d/cri-o.list

apt-get update
apt-get install -y cri-o kubelet kubeadm kubectl
apt-mark hold kubelet kubeadm kubectl

systemctl enable --now crio
systemctl enable kubelet

# --- initialize the control plane ---
kubeadm init --pod-network-cidr=192.168.0.0/16 --cri-socket=unix:///var/run/crio/crio.sock

mkdir -p /home/ubuntu/.kube
cp -i /etc/kubernetes/admin.conf /home/ubuntu/.kube/config
chown ubuntu:ubuntu /home/ubuntu/.kube/config
export KUBECONFIG=/etc/kubernetes/admin.conf

# --- publish a non-expiring join command via SSM Parameter Store ---
# Worker ASG instances can launch at any point in the cluster's life (e.g. when
# scaling back up from desired_capacity=0), long after a default kubeadm
# token's 24h TTL would have lapsed. `--ttl 0` makes the token never expire,
# and publishing the ready-to-run join command to SSM Parameter Store (instead
# of Secrets Manager/Lambda/a lifecycle hook) keeps the join flow to a single
# narrowly-scoped IAM permission on each side (control plane: PutParameter,
# workers: GetParameter) with nothing else to provision or operate.
JOIN_COMMAND=$(kubeadm token create --ttl 0 --print-join-command)
aws ssm put-parameter \
  --region "${region}" \
  --name "${ssm_param_name}" \
  --type SecureString \
  --overwrite \
  --value "$JOIN_COMMAND"
