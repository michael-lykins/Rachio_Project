# k8s-logstash

Logstash deployed on Kubernetes (Docker Desktop), with Elastic Agent (Helm) monitoring the cluster and shipping data to your existing Elastic Cloud instance.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Docker Desktop Kubernetes                                       │
│                                                                  │
│  namespace: elastic-stack          namespace: kube-system        │
│  ┌───────────────────┐             ┌──────────────────────────┐ │
│  │   Logstash Pod    │             │  Elastic Agent (Helm)    │ │
│  │  • Beats  :5044   │             │  v9.3.1 DaemonSet        │ │
│  │  • HTTP   :8080   │             │  • K8s metrics           │ │
│  │  • Monitor:9600   │             │  • Pod/container logs    │ │
│  └────────┬──────────┘             └───────────┬──────────────┘ │
│           │                                    │                 │
└───────────┼────────────────────────────────────┼─────────────────┘
            │                                    │
            ▼                                    ▼
   Elastic Cloud (Serverless)          Elastic Cloud (Serverless)
   logs-generic.logstash-default       kubernetes-* / logs-* indices
   :443                                :443
```

## Prerequisites

- Docker Desktop with Kubernetes enabled (Settings → Kubernetes → Enable Kubernetes)
- `kubectl` pointing at the `docker-desktop` context
- `helm` v3+ installed

```bash
kubectl config current-context   # → docker-desktop
kubectl get nodes                 # → single node Ready
helm version                      # → v3.x
```

## File structure

```
k8s-logstash/
├── namespace.yaml                  # elastic-stack namespace
├── deploy.sh                       # up / down / status helper
├── logstash/
│   ├── configmap.yaml              # logstash.yml + pipeline config
│   ├── secret.yaml                 # ES credentials ← fill in
│   ├── deployment.yaml             # Logstash 9.0.3
│   └── service.yaml                # NodePort 30044 (beats), 30080 (http)
└── elastic-agent/
    └── values.yaml                 # Helm values ← fill in decoded api_key
```

---

## Step 1 — Fill in the Logstash secret

`logstash/secret.yaml` already has the ES endpoint pre-filled. You only need to set the API key (already done if you followed setup).

Create an API key in **Kibana → Stack Management → API Keys** with permissions:
- `index`, `create_index`, `auto_configure` on `logs-*`

---

## Step 2 — Fill in the Elastic Agent Helm values

Edit `elastic-agent/values.yaml` and replace `<REPLACE_WITH_DECODED_API_KEY>` with the **decoded** key from the Elastic Cloud install command.

Elastic gave you a base64-encoded key from the install command. Decode it:
```bash
echo "<your-base64-encoded-api-key>" | base64 -d
```

Paste that decoded value into `elastic-agent/values.yaml`:
```yaml
outputs:
  default:
    api_key: "<paste decoded key here>"
```

---

## Step 3 — Deploy

```bash
./deploy.sh up
```

This will:
1. Create the `elastic-stack` namespace
2. Apply Logstash manifests via `kubectl`
3. Install Elastic Agent 9.3.1 via Helm into `kube-system`

---

## Step 4 — Verify

**Watch pods come up:**
```bash
# Logstash (~60-90s to initialize)
kubectl get pods -n elastic-stack -w

# Elastic Agent
kubectl get pods -n kube-system -l app=elastic-agent -w
```

**Logstash health:**
```bash
kubectl port-forward -n elastic-stack deployment/logstash 9600:9600 &
curl http://localhost:9600
```

**Send a test event (HTTP input):**
```bash
curl -X POST http://localhost:30080 \
  -H "Content-Type: application/json" \
  -d '{"message": "hello from k8s-logstash", "test": true}'
```

Then check in **Kibana → Discover** → data stream `logs-generic.logstash-default`.

**Elastic Agent status:**
```bash
kubectl logs -n kube-system -l app=elastic-agent --tail=50
helm status elastic-agent -n kube-system
```

---

## Ports

| Port | Exposed as | Description |
|------|------------|-------------|
| 5044 | `localhost:30044` | Beats input (Filebeat, Metricbeat, etc.) |
| 8080 | `localhost:30080` | HTTP JSON input |
| 9600 | `kubectl port-forward` only | Logstash monitoring API |

---

## Updating the Logstash pipeline

The pipeline lives in `logstash/configmap.yaml` under the `logstash.conf` key. After editing:

```bash
kubectl apply -f logstash/configmap.yaml
kubectl rollout restart deployment/logstash -n elastic-stack
```

## Tear down

```bash
./deploy.sh down
```

---

## Connecting Rachio data through Logstash

To route the Rachio collector through Logstash before Elastic Cloud, send events to `localhost:30080` (HTTP input) or add a Beats output to the collector pointing at `localhost:30044`.
