# PolyAI on Kubernetes

Plain-YAML deployment of the PolyAI stack (minus node-exporter) into `dev` and `prod`
namespaces on a self-managed (kubeadm) cluster. No Kustomize, no shared "base" folder
(per instructor requirement) - `infra/k8s/dev/` and `infra/k8s/prod/` each have their own
full, self-contained set of manifests, identical except for the frontend Deployment's image
tag. Namespace-scoped bare service DNS names (`agent`, `yolo`, ...) already resolve
correctly per-namespace on their own.

Every Service is still `ClusterIP` - as of task7 (see `task7.md` Part I), the stack is
additionally reachable from the internet through the Nginx Ingress Controller
(`infra/k8s/bootstrap.sh`, installed as a fixed-NodePort `Service`) fronted by an ALB +
Route 53 wildcard record (`infra/tf/modules/ingress`). `kubectl port-forward` still works
for local debugging and is what Step 8 below documents, but you generally don't need it
anymore - see "Step 14 - Public access via Ingress" at the bottom of this file.

As of task7 Part II, Prometheus and Grafana are no longer hand-written manifests - they're
`prometheus-community/kube-prometheus-stack` (Helm), installed **once, cluster-wide**, into
its own `monitoring` namespace, not duplicated per `dev`/`prod` like the rest of the stack
(see `infra/k8s/monitoring/values.yaml` for why: one Grafana/Prometheus/Alertmanager gives
"a single place to watch all traffic entering your cluster" across both environments,
instead of two separate panes of glass). `ServiceMonitor`s living inside `dev`/`prod`
(`infra/k8s/{dev,prod}/{agent,yolo}/*-servicemonitor.yaml`) are still scraped from there.

The old Docker Compose deployment on the two existing EC2 hosts keeps running unchanged
throughout - nothing here touches it.

> **Note:** As of the ArgoCD integration, `yolo`, `agent`, `frontend`, and `img-proc-mcp`'s
> manifests (`infra/k8s/{dev,prod}/<service>/`) - including each one's `ServiceMonitor` as
> of task7 Part II - are managed by ArgoCD (see `infra/k8s/argo/`), not by manual
> `kubectl apply`. ArgoCD auto-syncs `dev` on every push to the `dev` branch; `prod`
> requires a manual sync click in the ArgoCD UI. `kube-prometheus-stack` itself and the rest
> of `infra/k8s/monitoring/` are installed by `infra/k8s/bootstrap.sh` instead (see Step 5) -
> the steps below still apply as-is to the `Namespace`/`StorageClass` objects still living in
> `infra/k8s/{dev,prod}/` (the only things still applied manually).

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

## Step 0.5 - Bootstrap Calico + ArgoCD + kube-prometheus-stack (one-time, after `terraform apply`)

`infra/tf` provisions the control plane already `kubeadm init`-ed. Installing the
CNI plugin, ArgoCD, the `app-of-apps` `Application` (which in turn makes ArgoCD
pick up every other `Application` in `infra/k8s/argo/` directly from git),
`kube-prometheus-stack` (Prometheus/Grafana/Alertmanager - task7.md Part II), and
Cluster Autoscaler (task7.md Part III bonus) is done by `infra/k8s/bootstrap.sh` -
idempotent, safe to re-run. It needs `SNS_TOPIC_ARN` (a Terraform output,
`module.monitoring`) exported first, so Alertmanager knows which SNS topic to publish
to (see Step 5), and `CLUSTER_NAME`/`AWS_REGION` so Cluster Autoscaler knows which
worker ASG to discover and manage (see Step 15):

```bash
git clone https://github.com/MuhammadSawalha/PolyAI.git
cd PolyAI
export SNS_TOPIC_ARN=$(terraform -chdir=infra/tf output -raw sns_topic_arn)
export CLUSTER_NAME=$(terraform -chdir=infra/tf output -raw cluster_name)
export AWS_REGION=us-east-1   # match the region you provisioned the cluster into
infra/k8s/bootstrap.sh
```

`.github/workflows/cluster.yaml`'s `bootstrap` job runs this same script (copied
over via `scp` instead of a full clone, `SNS_TOPIC_ARN`/`CLUSTER_NAME` passed through
from the `provision` job's Terraform outputs and `AWS_REGION` from the workflow's
`region` input) - see that workflow for the automated version of this step.

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
   the no-Kustomize/no-shared-base rule is about the app Deployments you hand-write):
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

## Step 5 - kube-prometheus-stack (Prometheus, Grafana, Alertmanager)

Installed by `infra/k8s/bootstrap.sh` (Step 0.5), which runs the equivalent of:
```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update prometheus-community

helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  -f infra/k8s/monitoring/values.yaml \
  --set alertmanager.config.receivers[0].sns_configs[0].topic_arn="$SNS_TOPIC_ARN" \
  --set alertmanager.config.receivers[0].sns_configs[0].sigv4.region="$SNS_TOPIC_REGION"

kubectl apply -f infra/k8s/monitoring/grafana-dashboard-agent.yaml           # sidecar-loaded, no manual import
kubectl apply -f infra/k8s/monitoring/grafana-dashboard-nginx-ingress.yaml   # same mechanism, see note below
kubectl apply -f infra/k8s/monitoring/ingress-nginx-metrics.yaml     # Service + ServiceMonitor for ingress-nginx's own /metrics
kubectl apply -f infra/k8s/monitoring/prometheus-rules.yaml          # task7.md Part II step 5's two alert rules
kubectl apply -f infra/k8s/monitoring/grafana-ingress.yaml
kubectl apply -f infra/k8s/monitoring/prometheus-ingress.yaml
```
`infra/k8s/monitoring/values.yaml` sets Prometheus storage to `ebs-sc` (3Gi, `retention: 30d`)
and Grafana persistence to `ebs-sc` (1Gi), and points every Prometheus selector
(`serviceMonitorNamespaceSelector`, `ruleNamespaceSelector`, ...) at `{}` (all namespaces),
so it also picks up the `ServiceMonitor`s that live inside `infra/k8s/{dev,prod}/{agent,yolo}/`
(ArgoCD-managed - each Service now carries `metadata.labels.app` for the `ServiceMonitor`'s
selector to match, and a named `http` port for its endpoint) and the `PrometheusRule` above.
`kubeControllerManager`/`kubeScheduler`/`kubeProxy`/`kubeEtcd` are disabled in the values
file - this is a self-managed kubeadm cluster, not EKS, so those control-plane component
scrapers would just sit permanently `Down` (their default ports are kubeadm's, bound to
`127.0.0.1`).

The community [NGINX Ingress Controller dashboard](https://grafana.com/grafana/dashboards/9614-nginx-ingress-controller/)
(grafana.com id `9614`) and the custom `agent.json` dashboard (same one the legacy Docker
Compose Grafana uses) are both committed as `ConfigMap`s labeled `grafana_dashboard: "1"`,
picked up by the chart's dashboard sidecar (`infra/k8s/monitoring/grafana-dashboard-agent.yaml`,
`infra/k8s/monitoring/grafana-dashboard-nginx-ingress.yaml`) - nothing to import by hand.
(The chart's own `grafana.dashboards.<provider>.gnetId` values mechanism looks like the more
obvious way to pull `9614` in, and was tried first - it downloads the dashboard into the pod's
`/var/lib/grafana/dashboards/default/` via an init container, but this chart's only configured
provisioning provider watches `/tmp/dashboards` instead (fed by `sidecar.dashboards`), so the
download is silently never loaded. Committing it as a `ConfigMap` sidesteps that mismatch by
using the one mechanism that's actually wired up.)

## Step 6 - Apply everything

Everything in each folder, including the `Namespace` and the `StorageClass`, applies in
one shot - there's no volume ID or other placeholder to fill in anywhere first:

```bash
kubectl apply -f infra/k8s/dev/
kubectl apply -f infra/k8s/prod/
```
No `-n` flag needed - every namespaced object in the folder already declares its own
`metadata.namespace`, and the `Namespace`/`StorageClass` objects are cluster-scoped so a
namespace flag wouldn't apply to them anyway. As of task7 Part II this only actually applies
the `Namespace` and `StorageClass` objects - `ebs-sc` still needs to exist here since
`kube-prometheus-stack`'s Prometheus/Grafana PVCs (Step 5) reference it too.

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
(`provisioner: ebs.csi.aws.com`), and `kube-prometheus-stack`'s Prometheus/Grafana just ask
for `storageClassName: ebs-sc` (`infra/k8s/monitoring/values.yaml`) - no EBS volume or PV is
created ahead of time, and no `volumeHandle`/`claimRef` to fill in by hand anywhere. Because
`ebs-sc` uses `volumeBindingMode: WaitForFirstConsumer`, each PVC stays `Pending` until a Pod
that mounts it is actually scheduled onto a node - only then does the EBS CSI driver create
the volume, in that node's exact Availability Zone.

Watch it happen (both PVCs were already created by Step 5's Helm install):
```bash
kubectl get pvc -n monitoring -w
```
You should see the Prometheus and Grafana PVCs both go `Pending` → `Bound` within a few
seconds, once their respective pods schedule. Then:
```bash
kubectl get pv                                             # two new PVs were auto-created
kubectl describe pvc -n monitoring -l app.kubernetes.io/name=grafana
kubectl get pvc -n monitoring -l operator.prometheus.io/name=kube-prometheus-stack-prometheus
```

## Step 8 - Access it via port-forward

Pick a set of local ports per environment so dev and prod can run side by side without
clashing. Suggested mapping (matches the URLs baked into the frontend images in Step 4):

| | dev | prod |
|---|---|---|
| frontend | `kubectl port-forward svc/frontend-svc 3000:3000 -n dev` | `kubectl port-forward svc/frontend-svc 3001:3000 -n prod` |
| agent | `kubectl port-forward svc/agent-svc 8000:8000 -n dev` | `kubectl port-forward svc/agent-svc 8001:8000 -n prod` |

Grafana/Prometheus/Alertmanager are a single, cluster-wide install (`monitoring` namespace,
Step 5) rather than one per environment:

```bash
kubectl port-forward svc/kube-prometheus-stack-grafana 3002:80 -n monitoring
kubectl port-forward svc/kube-prometheus-stack-prometheus 9090:9090 -n monitoring
kubectl port-forward svc/kube-prometheus-stack-alertmanager 9093:9093 -n monitoring
```

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
kubectl delete pod -n monitoring -l app.kubernetes.io/name=prometheus
kubectl get pods -n monitoring -w   # wait for the replacement pod to become Ready
kubectl port-forward svc/kube-prometheus-stack-prometheus 9090:9090 -n monitoring
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

## Step 12 - Verify ServiceMonitor targets and dashboards (task7.md Part II step 3)

Open the Prometheus UI (`kubectl port-forward svc/kube-prometheus-stack-prometheus 9090:9090
-n monitoring`, then `http://localhost:9090/targets`) and confirm these are all `UP`:
- `serviceMonitor/dev/agent/0` and `serviceMonitor/prod/agent/0`
- `serviceMonitor/dev/yolo/0` and `serviceMonitor/prod/yolo/0`
- `serviceMonitor/monitoring/ingress-nginx-controller/0`

If any of these are missing entirely (not just `Down`), the `ServiceMonitor` hasn't been
picked up yet - check `kubectl get servicemonitors -A` and that ArgoCD synced
`infra/k8s/{dev,prod}/{agent,yolo}/*-servicemonitor.yaml` (`kubectl get application -n argocd`).

Then open Grafana (`kubectl port-forward svc/kube-prometheus-stack-grafana 3002:80
-n monitoring`, `http://localhost:3002`, `admin`/`admin123` - see `infra/k8s/monitoring/values.yaml`)
and confirm both dashboards loaded automatically (Dashboards → Browse, no manual import step
for either):
- **Agent Observability** - chat request rate/latency/error-rate/token usage, sourced from
  `agent_chat_requests_total`/`agent_chat_latency_seconds` (`services/agent/app.py`).
- **NGINX Ingress Controller** - request rate, latency, and response codes per `Ingress`
  host, sourced from `ingress-nginx-controller-metrics` - "a single place to watch all
  traffic entering your cluster" across both `dev` and `prod`.

## Step 13 - Alerting: SNS → email, and simulating a failure (task7.md Part II steps 4-6)

**One-time:** the first `terraform apply` after adding `alert_email` to your tfvars sends
that address an SNS subscription-confirmation email - click the link, or Alertmanager's
`sns_configs` publishes will succeed but nothing will ever arrive in your inbox.

`infra/k8s/monitoring/prometheus-rules.yaml` defines two rules against real agent/yolo
metrics, at different severities:
- `AgentHighChatErrorRate` (`critical`) - fires when >20% of the agent's `/chat` requests
  fail over 5 minutes (`agent_chat_requests_total{status="error"}` vs the total).
- `YoloHighPredictLatency` (`warning`) - fires when yolo's `/predict` p95 latency exceeds 2s
  for 5 minutes (`http_request_duration_seconds_bucket{job="yolo",handler="/predict"}`).

To prove the whole chain (`Pending` → `Firing` in Prometheus → Alertmanager → email, then
`RESOLVED`), the easiest one to trigger deliberately is `AgentHighChatErrorRate` - and it
needs the agent pod to keep *running* (so `/metrics` stays scrapeable and `_chat_impl`'s
`except Exception: CHAT_REQUESTS.labels(status="error").inc()` in `services/agent/app.py`
actually executes), not scaled to 0. Point it at a Bedrock model that doesn't exist instead,
so every `/chat` call reaches `_chat_impl`, fails inside it, and increments the `error`
counter for real:

```bash
kubectl patch configmap agent-config -n dev --type merge -p '{"data":{"MODEL":"does-not-exist"}}'
kubectl rollout restart deployment/agent -n dev   # ConfigMap changes aren't hot-reloaded
kubectl rollout status deployment/agent -n dev

# generate a handful of failing /chat requests, spread over a minute or two so rate() has
# enough samples to average over
kubectl port-forward svc/agent-svc 8000:8000 -n dev &
for i in $(seq 1 10); do
  curl -s -X POST http://localhost:8000/chat -H "Content-Type: application/json" \
    -d '{"messages":[{"role":"user","content":"hi"}]}'
  sleep 10
done
```

Watch it progress:
1. Prometheus UI → Alerts: `AgentHighChatErrorRate` goes `Inactive` → `Pending` (once the
   error ratio crosses 20%) → `Firing` (after the 5m `for:` window).
2. Alertmanager UI (`kubectl port-forward svc/kube-prometheus-stack-alertmanager 9093:9093
   -n monitoring`, `http://localhost:9093`): the alert shows up there once Prometheus fires it.
3. Your inbox: an SNS email notification arrives shortly after (`sns_configs` fan-out).

Then fix the cause and confirm the `RESOLVED` notification also arrives:
```bash
kubectl patch configmap agent-config -n dev --type merge -p '{"data":{"MODEL":"amazon.nova-lite-v1:0"}}'
kubectl rollout restart deployment/agent -n dev
kubectl rollout status deployment/agent -n dev
```
Once the error ratio drops back under 20% and stays there past `repeat_interval`, Prometheus
transitions the alert back to `Inactive` and Alertmanager sends a second, `RESOLVED` email
for the same alert. (`infra/k8s/dev/agent/agent-configmap.yaml` was never edited in git, so
ArgoCD's `selfHeal` would have reverted the live patch back to `amazon.nova-lite-v1:0` on its
own anyway.)

## Step 14 - Public access via Ingress

`infra/k8s/bootstrap.sh` installs the Nginx Ingress Controller (baremetal provider
manifest) and pins its Service's `nodePort`s to `30080` (HTTP) / `30443` (HTTPS) -
`infra/tf/modules/ingress` provisions an ALB whose target group points at `30080` on
every worker (via `aws_autoscaling_attachment`, so it stays correct as the ASG scales),
terminates TLS with an ACM certificate for `*.sawalha-polyai.fursa.click`, and a Route 53
alias record for that same wildcard in the shared `fursa.click` zone. Routing to the right
Service happens inside the ingress controller by `Host` header - the ALB/DNS layer don't
know about individual services at all.

Once `terraform apply` and `bootstrap.sh` have both run, and ArgoCD has synced the
`*-ingress.yaml` manifests (`infra/k8s/{dev,prod}/{frontend,agent}/*-ingress.yaml`,
applied automatically by ArgoCD; `infra/k8s/argo/argocd-ingress.yaml`, picked up by the
`app-of-apps` `Application` itself; `infra/k8s/monitoring/{grafana,prometheus}-ingress.yaml`,
applied by `bootstrap.sh` as part of Step 5), everything is reachable directly over HTTPS,
no port-forward or SSH tunnel needed:

| | dev | prod |
|---|---|---|
| frontend | `https://frontend-dev.sawalha-polyai.fursa.click` | `https://frontend-prod.sawalha-polyai.fursa.click` |
| agent | `https://agent-dev.sawalha-polyai.fursa.click` | `https://agent-prod.sawalha-polyai.fursa.click` |

| | single, cluster-wide |
|---|---|
| grafana | `https://grafana.sawalha-polyai.fursa.click` |
| prometheus | `https://prometheus.sawalha-polyai.fursa.click` |
| argocd | `https://argocd.sawalha-polyai.fursa.click` |

DNS propagation plus ACM's DNS validation can take a few minutes after the first
`terraform apply` - `terraform output alb_dns_name` and a direct `curl -H "Host: ..."
http://<alb_dns_name>` (matching one of the hosts above) is a faster way to confirm the
ALB → target group → ingress-nginx path works before waiting on DNS.

## Step 15 - Cluster Autoscaler (task7.md Part III bonus)

`infra/k8s/bootstrap.sh` installs `autoscaler/cluster-autoscaler` (Helm) into
`kube-system`, once, cluster-wide - it watches for `Pending` Pods that don't fit on any
current node and grows `module.k8s_cluster.aws_autoscaling_group.workers`'s
`desired_capacity` (bounded by `asg_min_size`/`asg_max_size`), then shrinks it back down
once nodes sit underutilized for `scale-down-unneeded-time` (`infra/k8s/autoscaler/values.yaml`,
10m). It authenticates through the worker node's own EC2 instance profile - no IRSA, same
pattern as Alertmanager's SNS publish (Step 3) - via the IAM policy in
`infra/tf/modules/autoscaler`, scoped to just this cluster's one ASG
(`autoscaling:SetDesiredCapacity`/`TerminateInstanceInAutoScalingGroup` require the exact
ASG ARN; the read-only `Describe*` calls need `Resource: "*"`, an IAM API limitation, not a
missed scope). It finds that ASG via AWS auto-discovery - the
`k8s.io/cluster-autoscaler/enabled=true` and `k8s.io/cluster-autoscaler/<cluster_name>=owned`
tags Terraform puts on it (`infra/tf/modules/k8s-cluster`) - instead of a hardcoded
`--nodes=min:max:asg-name`, so `asg_min_size`/`asg_max_size` stay the single source of
truth.

**Prove scale-up:** `infra/k8s/autoscaler/scale-up-demo.yaml` is a 3-replica dummy
Deployment (`registry.k8s.io/pause`) that each requests more memory (2500Mi) than
comfortably fits alongside the others on the current worker fleet - deliberately not part
of `app-of-apps`, apply/delete it by hand:

```bash
kubectl apply -f infra/k8s/autoscaler/scale-up-demo.yaml
kubectl get pods -n dev -w                       # some stay Pending on the current node(s)
kubectl -n kube-system logs -l app.kubernetes.io/name=aws-cluster-autoscaler -f   # watch it decide to scale up
```

Within a minute or two you should see a new EC2 instance launch (`aws autoscaling
describe-auto-scaling-groups --auto-scaling-group-name <name>` or the EC2 console/
`kubectl get nodes -w`), join the cluster, and the previously-`Pending` Pods schedule onto
it.

**Prove scale-down:**

```bash
kubectl delete -f infra/k8s/autoscaler/scale-up-demo.yaml
```

Once the extra node(s) sit idle past `scale-down-unneeded-time` (10m), the autoscaler
drains and terminates them and the ASG's `desired_capacity` drops back down -
`kubectl get nodes -w` and the ASG's `DesiredCapacity` both confirm it. Unlike Step 11's
manual `tfvars` scale-down, the autoscaler's own termination path cleans up the matching
`Node` object itself (it calls the Kubernetes API as part of the scale-down, not just the
ASG API) - no leftover `NotReady` node to `kubectl delete` by hand.

> [!IMPORTANT]
> Set `asg_desired_capacity` back to `0` in `infra/tf/tfvars/<region>.tfvars` (and
> re-apply/re-run `cluster.yaml`) once you're done experimenting, to avoid paying for idle
> workers.
