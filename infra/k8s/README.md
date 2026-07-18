# PolyAI on Kubernetes

Plain-YAML deployment of the PolyAI stack (minus node-exporter) into `dev` and `prod`
namespaces on a self-managed (kubeadm) cluster. No Helm, no Kustomize, no operators, and
(per instructor requirement) **no shared "base" folder** - `infra/k8s/` contains only two
folders, `dev/` and `prod/`, each with its own full, self-contained set of manifests.
Namespace-scoped bare service DNS names (`agent`, `yolo`, `prometheus`, ...) already
resolve correctly per-namespace on their own, so the two folders are identical except for:
the frontend Deployment's image tag, and Prometheus's PV/PVC (different EBS volume per
environment).

**No NodePort/Ingress anywhere** (per instructor requirement) - every Service is
`ClusterIP`, and every access path (browser to frontend/agent, you to Grafana/Prometheus)
goes through a manually-run `kubectl port-forward`.

The old Docker Compose deployment on the two existing EC2 hosts keeps running unchanged
throughout - nothing here touches it.

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

## Step 1 - Namespaces

```bash
kubectl create ns dev
kubectl create ns prod
```

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
3. Note the worker's exact Availability Zone (EBS volumes are AZ-locked - the volume must
   be created in the same AZ as the worker node).
4. Install the EBS CSI driver (cluster add-on infrastructure, not one of "your services" -
   the no-Helm rule is about the app/Prometheus/Grafana Deployments you hand-write):
   ```bash
   kubectl apply -k "github.com/kubernetes-sigs/aws-ebs-csi-driver/deploy/kubernetes/overlays/stable/?ref=release-1.35"
   ```
5. Create two EBS volumes, one per environment:
   ```bash
   aws ec2 create-volume --size 5 --volume-type gp3 --availability-zone <worker-az> \
     --tag-specifications 'ResourceType=volume,Tags=[{Key=Name,Value=prometheus-data-dev}]'
   aws ec2 create-volume --size 5 --volume-type gp3 --availability-zone <worker-az> \
     --tag-specifications 'ResourceType=volume,Tags=[{Key=Name,Value=prometheus-data-prod}]'
   ```
6. Take the two resulting `VolumeId`s (`vol-...`) and paste them into
   `infra/k8s/dev/prometheus-pv.yaml` and `infra/k8s/prod/prometheus-pv.yaml`
   (`spec.csi.volumeHandle`), replacing the `vol-REPLACE_ME_DEV` / `vol-REPLACE_ME_PROD`
   placeholders currently there.

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
`http://localhost:3001` - see Step 7 for which port maps to which environment.)

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

```bash
kubectl apply -f infra/k8s/dev/prometheus-pv.yaml
kubectl apply -f infra/k8s/prod/prometheus-pv.yaml

kubectl apply -n dev  -f infra/k8s/dev/
kubectl apply -n prod -f infra/k8s/prod/
```
(the `prometheus-pv.yaml` files are `PersistentVolume` objects, which are cluster-scoped,
not namespaced - hence no `-n` on those two lines; everything else in each folder is
namespaced and picks up `-n dev` / `-n prod` from the directory-wide apply.)

Check everything came up:
```bash
kubectl get pods -n dev
kubectl get pods -n prod
```

## Step 7 - Access it via port-forward

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

## Step 8 - Test the HPA (yolo)

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

## Step 9 - Verify Prometheus storage actually persists

```bash
kubectl delete pod -n dev -l app=prometheus
kubectl get pods -n dev -w   # wait for the replacement pod to become Ready
kubectl port-forward svc/prometheus-svc 9090:9090 -n dev
```
Open `http://localhost:9090/graph` and confirm historical metrics from before the delete
are still there (proves the PVC/EBS volume, not the pod's local disk, is where the data
actually lives).
