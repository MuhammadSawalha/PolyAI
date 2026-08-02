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
apt-get install -y software-properties-common curl gpg apt-transport-https ca-certificates unzip

# AWS CLI v2 (single static binary via the official installer) instead of the
# apt-packaged v1, which pulls in ~100 unrelated packages (imagemagick,
# ghostscript, full font-rendering stacks) - real, avoidable boot-time/CPU/
# disk weight on a t3.medium.
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp
/tmp/aws/install
rm -rf /tmp/awscliv2.zip /tmp/aws

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
# The SSM parameter is created by Terraform with a "PENDING" placeholder
# value (see aws_ssm_parameter.join_command) and only overwritten with a real
# join command once the control plane finishes kubeadm init - a plain "did
# the fetch succeed" check isn't enough, since a successful fetch can still
# return that placeholder if this worker is faster than the control plane.
JOIN_COMMAND=""
for i in $(seq 1 60); do
  FETCHED=$(aws ssm get-parameter \
    --region "${region}" \
    --name "${ssm_param_name}" \
    --with-decryption \
    --query 'Parameter.Value' \
    --output text 2>/dev/null || true)

  if [ -n "$FETCHED" ] && [ "$FETCHED" != "PENDING" ]; then
    JOIN_COMMAND="$FETCHED"
    break
  fi

  echo "Join command not published yet, retrying in 10s... ($i/60)"
  sleep 10
done

if [ -z "$JOIN_COMMAND" ]; then
  echo "Timed out waiting for the control plane to publish the join command." >&2
  exit 1
fi

# The control plane's API server can still be warming up (leader election,
# cert setup) right after it publishes the join command, so a join attempt
# can transiently fail (e.g. "client rate limiter Wait returned an error")
# even though the command itself is valid. Retry instead of treating one
# bad-timing attempt as fatal; reset between attempts so a partially-applied
# join doesn't block the next one.
for i in $(seq 1 10); do
  if eval "$JOIN_COMMAND --cri-socket=unix:///var/run/crio/crio.sock"; then
    echo "Successfully joined the cluster."
    exit 0
  fi
  echo "kubeadm join failed (attempt $i/10), resetting and retrying in 15s..."
  kubeadm reset -f --cri-socket=unix:///var/run/crio/crio.sock >/dev/null 2>&1 || true
  sleep 15
done

echo "kubeadm join did not succeed after 10 attempts." >&2
exit 1
