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

# --- cri-o, kubelet, kubeadm ---
apt-get update
apt-get install -y software-properties-common curl gpg apt-transport-https ca-certificates awscli

mkdir -p /etc/apt/keyrings

curl -fsSL "https://pkgs.k8s.io/core:/stable:/v$KUBERNETES_VERSION/deb/Release.key" | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v$KUBERNETES_VERSION/deb/ /" > /etc/apt/sources.list.d/kubernetes.list

curl -fsSL "https://download.opensuse.org/repositories/isv:/cri-o:/stable:/v$KUBERNETES_VERSION/deb/Release.key" | gpg --dearmor -o /etc/apt/keyrings/cri-o-apt-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/cri-o-apt-keyring.gpg] https://download.opensuse.org/repositories/isv:/cri-o:/stable:/v$KUBERNETES_VERSION/deb/ /" > /etc/apt/sources.list.d/cri-o.list

apt-get update
apt-get install -y cri-o kubelet kubeadm
apt-mark hold kubelet kubeadm

systemctl enable --now crio
systemctl enable kubelet

# --- fetch the join command published by the control plane and join ---
# See control-plane-init.sh.tpl for why this is a non-expiring token in SSM
# Parameter Store rather than Secrets Manager/Lambda/a lifecycle hook.
#
# The control plane only publishes this parameter after its own (much
# longer - it includes the full `kubeadm init`) boot sequence completes, and
# this worker boots in parallel with it, not after it. A worker that finishes
# its lighter apt-only setup first would otherwise hit ParameterNotFound on a
# one-shot fetch, so retry here instead of failing immediately.
JOIN_COMMAND=""
for i in $(seq 1 60); do
  JOIN_COMMAND=$(aws ssm get-parameter \
    --region "${region}" \
    --name "${ssm_param_name}" \
    --with-decryption \
    --query 'Parameter.Value' \
    --output text 2>/dev/null) && break
  echo "Join command not published yet, retrying in 10s... ($i/60)"
  sleep 10
done

if [ -z "$JOIN_COMMAND" ]; then
  echo "Timed out waiting for the control plane to publish the join command." >&2
  exit 1
fi

eval "$JOIN_COMMAND --cri-socket=unix:///var/run/crio/crio.sock"
