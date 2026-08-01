# PolyAI on Kubernetes

Plain-YAML deployment of the PolyAI stack (minus node-exporter) into `dev` and `prod`
namespaces on a self-managed (kubeadm) cluster. No Helm, no Kustomize, no operators, and
(per instructor requirement) **no shared "base" folder** - `infra/k8s/` contains only two
folders, `dev/` and `prod/`, each with its own full, self-contained set of manifests.
Namespace-scoped bare service DNS names (`agent`, `yolo`, `prometheus`, ...) already
resolve correctly per-namespace on their own, so the two folders are identical except for
the frontend Deployment's image tag - even Prometheus's and Grafana's storage is
dynamically provisioned per-namespace via the same shared `ebs-sc` `StorageClass`, so
there's no per-environment EBS volume ID to hand-manage anywhere anymore.

**No NodePort/Ingress anywhere** (per instructor requirement) - every Service is
`ClusterIP`, and every access path (browser to frontend/agent, you to Grafana/Prometheus)
goes through a manually-run `kubectl port-forward`.

The old Docker Compose deployment on the two existing EC2 hosts keeps running unchanged
throughout - nothing here touches it.

> **Note:** As of the ArgoCD integration, `yolo`, `agent`, `frontend`, and `img-proc-mcp`'s
> manifests (`infra/k8s/{dev,prod}/<service>/`) are managed by ArgoCD (see
> `infra/k8s/argo/`), not by manual `kubectl apply`. ArgoCD auto-syncs `dev` on every push
> to the `dev` branch; `prod` requires a manual sync click in the ArgoCD UI. The steps
> below still apply as-is to Grafana/Prometheus (the only services still applied manually).

---

## Step 0 - Access the cluster

All `kubectl`/`aws`/`docker` commands below assume you have `kubectl` configured against
the cluster. Two ways to get that:
- **Directly on the control-plane EC2 node**: SSH in (`ssh ubuntu@<control-plane-ip>`) and
  run `kubectl` there - `kubeadm init` already wrote `/etc/kubernetes/admin.conf` there.
- **From your laptop**: copy that same file down (`scp ubuntu@<control-plane-ip>:/etc/kubernetes/admin.conf ~/.kube/config`) and run `kubectl` locally - only works if the
  control-plane's API server port (6443) is reachable from your laptop (security group
  allows it), or if you tunnel it (`ssh -L 6443:localhost:6443 ubuntu@<control-plane-ip>`).

Since there's no NodePort, whenever a step below says "port-forward and open it in a
browser," if you're running `kubectl` **on the control-plane over SSH**, add an SSH tunnel
on top so your laptop's browser can reach it, e.g.:
```bash
ssh -L 3000:localhost:3000 -L 8000:localhost:8000 ubuntu@<control-plane-ip>
```
then run the `kubectl port-forward` commands in that same SSH session. If you copied the
kubeconfig to your laptop and run `kubectl` locally instead, you can skip the SSH tunnel -
`kubectl port-forward`'s `localhost` is then your own laptop already.

---

## Step 0.5 - Bootstrap Calico + ArgoCD (one-time, after `terraform apply`)

`infra/tf` provisions the control plane already `kubeadm init`-ed. Installing the
CNI plugin, ArgoCD, and the `app-of-apps` `Application` (which in turn makes ArgoCD
pick up every other `Application` in `infra/k8s/argo/` directly from git) is done by
`infra/k8s/bootstrap.sh` - idempotent, safe to re-run:

```bash
git clone https://github.com/MuhammadSawalha/PolyAI.git
cd PolyAI
infra/k8s/bootstrap.sh
```

`.github/workflows/cluster.yaml`'s `bootstrap` job runs this same script (copied
over via `scp` instead of a full clone) - see that workflow for the automated
version of this step.

## Step 1 - Namespaces

Namespaces are defined as plain manifests (`infra/k8s/dev/namespace.yaml`,
`infra/k8s/prod/namespace.yaml`) rather than created imperatively, and every namespaced
object in each folder now declares its own `metadata.namespace` to match - so `dev/` and
`prod/` are each fully self-contained and don't rely on an `-n` flag at apply time.

```bash
kubectl apply -f infra/k8s/dev/namespace.yaml
kubectl apply -f infra/k8s/prod/namespace.yaml
```
(Step 6's bulk `kubectl apply -f infra/k8s/dev/` would create these too automatically -
`kubectl apply` on a directory applies `Namespace` objects before namespaced ones
regardless of file order - but applying it explicitly here first is clearer to follow.)

## Step 2 - metrics-server (required for the HPAs to read CPU%)

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
```
kubeadm clusters commonly need this patch (kubelet serving certs aren't in a chain
metrics-server trusts by default):
```bash
kubectl -n kube-system patch deployment metrics-server --type=json \
  -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
```
Verify: `kubectl top nodes` must show real numbers, not an error, before continuing.

## Step 3 - Worker node IAM role (EBS CSI driver + S3/Bedrock access)

No IRSA since this isn't EKS - every AWS call a pod on the worker node makes (EBS volume
attach/detach for Prometheus, S3 image bucket access for yolo/img-proc-mcp/agent, Bedrock
model invocation for agent) is authorized through the **worker node's own EC2 instance
profile** - the same mechanism already granting S3/Bedrock access on the existing dev/prod
Compose EC2 hosts. There's no k8s Secret involved anywhere for this - `agent/app.py` never
even reads `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/`GOOGLE_API_KEY` (it only uses
`MODEL_PROVIDER=bedrock_converse`, which authenticates via boto3's default credential
chain, i.e. this instance profile).

1. Find the worker node's EC2 instance and its attached IAM role.
2. Attach three policies to that role:
   - `AmazonEBSCSIDriverPolicy` (AWS-managed: `arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy`) - for the EBS CSI driver.
   - `s3-image-policy` (your existing customer-managed policy, already on the dev/prod EC2 hosts) - S3 access for yolo/img-proc-mcp/agent.
   - `sawalha-bedrock-policy` (your existing customer-managed policy, already on the dev/prod EC2 hosts) - Bedrock model invocation for agent.
3. Install the EBS CSI driver (cluster add-on infrastructure, not one of "your services" -
   the no-Helm rule is about the app/Prometheus/Grafana Deployments you hand-write):
   ```bash
   kubectl apply -k "github.com/kubernetes-sigs/aws-ebs-csi-driver/deploy/kubernetes/overlays/stable/?ref=release-1.35"
   ```

That's it - no `aws ec2 create-volume` and no volume ID to paste anywhere. Both
Prometheus's and Grafana's PVCs (`storageClassName: ebs-sc`) are dynamically provisioned:
the CSI driver creates the actual EBS volume automatically, in whichever Availability Zone
the pod that mounts it gets scheduled to, the first time that pod schedules (see Step 7).

## Step 4 - Build the two frontend images

`NEXT_PUBLIC_AGENT_URL` is baked into the browser bundle at Next.js build time, and since
there's no NodePort/Ingress, the frontend will only ever be reached by *you*, via your own
`kubectl port-forward` to `localhost`. So bake each image to point at the local port you'll
forward the agent to for that environment - `localhost:8000` for dev, `localhost:8001` for
prod (chosen so you can run both port-forwards side by side without a clash):

```bash
docker build --build-arg NEXT_PUBLIC_AGENT_URL=http://localhost:8000 \
  -t muhammadsawalha/frontend-service:k8s-dev services/frontend && docker push muhammadsawalha/frontend-service:k8s-dev

docker build --build-arg NEXT_PUBLIC_AGENT_URL=http://localhost:8001 \
  -t muhammadsawalha/frontend-service:k8s-prod services/frontend && docker push muhammadsawalha/frontend-service:k8s-prod
```

(`services/agent/app.py`'s CORS list already allows `http://localhost:3000` and
`http://localhost:3001` - see Step 8 for which port maps to which environment.)

## Step 5 - Grafana dashboards ConfigMap

Created from files rather than hand-authored YAML, since `fastapi-observability.json` is
a large pre-existing export:
```bash
kubectl create configmap grafana-dashboards -n dev \
  --from-file=monitoring/grafana/provisioning/dashboards/dashboards.yml \
  --from-file=monitoring/grafana/provisioning/dashboards/fastapi-observability.json \
  --from-file=infra/grafana/dashboards/agent.json

kubectl create configmap grafana-dashboards -n prod \
  --from-file=monitoring/grafana/provisioning/dashboards/dashboards.yml \
  --from-file=monitoring/grafana/provisioning/dashboards/fastapi-observability.json \
  --from-file=infra/grafana/dashboards/agent.json
```
(`node-exporter-full.json` is deliberately excluded - node-exporter isn't deployed here.)

## Step 6 - Apply everything

Everything in each folder, including the `Namespace` and the `StorageClass`, applies in
one shot - there's no volume ID or other placeholder to fill in anywhere first:

```bash
kubectl apply -f infra/k8s/dev/
kubectl apply -f infra/k8s/prod/
```
No `-n` flag needed - every namespaced object in the folder already declares its own
`metadata.namespace`, and the `Namespace`/`StorageClass` objects are cluster-scoped so a
namespace flag wouldn't apply to them anyway.

(Note: this applies everything except `yolo`, `agent`, `frontend`, and `img-proc-mcp`,
which now live in their own subfolders and are managed by ArgoCD instead - see the note
near the top of this file.)

Check everything came up:
```bash
kubectl get pods -n dev
kubectl get pods -n prod
```

## Step 7 - Observe Prometheus's and Grafana's PVCs dynamically provision (Pending → Bound)

Both Prometheus's and Grafana's storage use **dynamic provisioning**:
`infra/k8s/dev/storageclass.yaml` defines a real `StorageClass` named `ebs-sc`
(`provisioner: ebs.csi.aws.com`), and `prometheus-pvc.yaml`/`grafana-pvc.yaml` just ask
for `storageClassName: ebs-sc` - no EBS volume or PV is created ahead of time, and no
`volumeHandle`/`claimRef` to fill in by hand anywhere. Because `ebs-sc` uses
`volumeBindingMode: WaitForFirstConsumer`, each PVC stays `Pending` until a Pod that
mounts it is actually scheduled onto a node - only then does the EBS CSI driver create the
volume, in that node's exact Availability Zone.

Watch it happen (all four PVCs were already created by Step 6's bulk apply):
```bash
kubectl get pvc -n dev -w
```
You should see `prometheus-pvc` and `grafana-pvc` both go `Pending` → `Bound` within a few
seconds, once their respective pods schedule. Then:
```bash
kubectl get pv                                  # two new PVs were auto-created - no *-pv.yaml exists anywhere in the repo
kubectl describe pvc prometheus-pvc -n dev       # shows the auto-created EBS volume ID
kubectl describe pvc grafana-pvc -n dev          # same, for grafana's own volume
```
Repeat with `-n prod` to see prod's own PVCs go through the same thing independently
(separate EBS volumes - each namespace's PVCs provision their own).

## Step 8 - Access it via port-forward

Pick a set of local ports per environment so dev and prod can run side by side without
clashing. Suggested mapping (matches the URLs baked into the frontend images in Step 4):

| | dev | prod |
|---|---|---|
| frontend | `kubectl port-forward svc/frontend-svc 3000:3000 -n dev` | `kubectl port-forward svc/frontend-svc 3001:3000 -n prod` |
| agent | `kubectl port-forward svc/agent-svc 8000:8000 -n dev` | `kubectl port-forward svc/agent-svc 8001:8000 -n prod` |
| grafana | `kubectl port-forward svc/grafana-svc 3002:3000 -n dev` | `kubectl port-forward svc/grafana-svc 3003:3000 -n prod` |
| prometheus | `kubectl port-forward svc/prometheus-svc 9090:9090 -n dev` | `kubectl port-forward svc/prometheus-svc 9091:9090 -n prod` |

Each `kubectl port-forward` command blocks in its own terminal (or run with `&`/`nohup` to
background it). With frontend+agent forwarded for dev, open `http://localhost:3000` and
send a chat message end-to-end. Same for prod at `http://localhost:3001` (agent forwarded
to 8001).

## Step 9 - Test the HPA (yolo)

`yolo`'s `/predict` needs an image already in S3 (no raw upload endpoint):

```bash
aws s3 cp services/yolo/beatles.jpeg s3://sawalha-polyai-images/loadtest/beatles.jpg

kubectl run loadgen -n dev --rm -it --restart=Never --image=curlimages/curl -- sh -c '
  for i in $(seq 1 8); do
    ( while true; do curl -s -o /dev/null -X POST http://yolo-svc:8080/predict \
        -H "Content-Type: application/json" \
        -d "{\"image_s3_key\":\"loadtest/beatles.jpg\"}"; done ) &
  done; wait'
```

Watch it scale, in another terminal:
```bash
kubectl get hpa -n dev -w
kubectl get pods -n dev -w
```

Stop the load (Ctrl-C, `kubectl delete pod loadgen -n dev` if it lingers) and watch
replicas drop back to 1 after the ~5 minute cooldown. Repeat with `-n prod` if you want to
verify prod's HPA too.

## Step 10 - Verify Prometheus storage actually persists

```bash
kubectl delete pod -n dev -l app=prometheus
kubectl get pods -n dev -w   # wait for the replacement pod to become Ready
kubectl port-forward svc/prometheus-svc 9090:9090 -n dev
```
Open `http://localhost:9090/graph` and confirm historical metrics from before the delete
are still there (proves the PVC/EBS volume, not the pod's local disk, is where the data
actually lives).

## Step 11 - Scaling the worker Auto Scaling Group

Set `asg_desired_capacity` in `infra/tf/tfvars/<region>.tfvars` and re-apply (or re-run
`cluster.yaml`) to change the number of worker nodes - `0` when the cluster isn't in use,
`1`-`3` otherwise (`asg_min_size`/`asg_max_size` bound it to that range).

Scaling down terminates the corresponding EC2 instance(s), but Kubernetes doesn't find out
on its own - the matching `Node` object(s) are left behind showing `NotReady`. This isn't
cleaned up automatically (see the comment on `aws_autoscaling_group.workers` in
`infra/tf/modules/k8s-cluster/main.tf` for why: it needs something reachable from both the
ASG's termination lifecycle hook and the cluster API - a Lambda or SSM Run Command - which
is more moving parts than this cluster's scale warrants). After scaling down, clean up the
stale node(s) by hand:

```bash
kubectl get nodes                      # find the NotReady one(s)
kubectl delete node <node-name>
```
