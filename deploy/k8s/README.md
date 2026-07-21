# AmpHive on k3s — single-node runbook

A **working** k3s manifest set that mirrors the live docker-compose stack
(`deploy/docker/docker-compose.tls.yml`): Postgres, Mosquitto, backend,
frontend. This is **node 1** of a cluster that will later gain home-machine
agent nodes; today it runs alongside the compose stack on the same VM, so ports
are chosen not to clash with it.

> Not the production deployment (yet). Prod is still Docker Compose on the GCP
> VM via `deploy/scripts/deploy.ps1` — see [docs/DEPLOYMENT.md](../../docs/DEPLOYMENT.md).
> This set exists so k3s can be brought up and verified in parallel, then cut
> over.

## What's here

| File | Contents |
|------|----------|
| `namespace.yaml` | namespace `amphive` |
| `postgres.yaml` | StatefulSet + PVC (local-path SC) + headless Service `db:5432` |
| `mosquitto.yaml` | ConfigMap (mosquitto.conf) + Deployment + Service `mqtt` (ClusterIP 1883) + Service `mqtt-tls` (NodePort **30883** → 8883) |
| `backend.yaml` | Deployment (`amphive-backend:local`, env via `envFrom` Secret) + Service `backend:8000` |
| `frontend.yaml` | Deployment (`amphive-frontend:local`) + Service `frontend:80` + NodePort **30080**; commented-out Ingress |
| `secret.example.yaml` | placeholder keys + generation commands (never commit real values) |
| `kustomization.yaml` | ties the non-secret manifests together |

**Removed:** `headscale.yaml` — the overlay is retired; the fleet is on direct
MQTT (`mqtts://…:8883`), so there is no control-server to run.

**Not mapped from compose (intentional):**
- **Caddy** — no in-cluster reverse proxy. TLS termination stays with the
  existing compose Caddy front door, which at cutover points at the frontend
  NodePort. (A future ingress controller could replace it.)
- **fake-plug** — the `fake_plug.py` simulator is a dev-only tool and is **not**
  in `docker-compose.tls.yml`, so there is no manifest for it. Run it as a
  one-off `kubectl run` against Service `mqtt` if needed.

## Prerequisites

Single-node k3s with **traefik disabled** (we expose via NodePort, not ingress):

```sh
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--disable traefik" sh -
```

k3s ships the `local-path` StorageClass (used by the Postgres PVC) built in.

## 1. Build the images and import them into k3s containerd

There is no registry — build from source and load straight into k3s's
containerd (`imagePullPolicy: IfNotPresent` means it uses the imported image):

```sh
# from repo root
docker build -t amphive-backend:local  ./backend
docker build -t amphive-frontend:local ./frontend

docker save amphive-backend:local  | sudo k3s ctr images import -
docker save amphive-frontend:local | sudo k3s ctr images import -

# verify
sudo k3s ctr images ls | grep amphive
```

(`postgres:15-alpine` and `eclipse-mosquitto:2.0` are pulled from Docker Hub
normally — only the two app images need importing.)

## 2. Create the secrets (never committed)

From the repo root, reusing the files `deploy.ps1` already generates:

```sh
kubectl create namespace amphive   # or let kustomize create it, then re-run these

# App env — one Secret straight from .env (same keys the backend uses)
kubectl create secret generic amphive-env -n amphive --from-env-file=.env

# Broker passwd + acl (the files deploy.ps1 builds on the VM; copy them locally
# or regenerate with mosquitto_passwd / the ACL template in deploy.ps1)
kubectl create secret generic amphive-mqtt-auth -n amphive \
  --from-file=passwd=./mosquitto_passwd \
  --from-file=acl=./mosquitto_acl

# Broker TLS material
kubectl create secret generic amphive-mqtt-certs -n amphive \
  --from-file=ca.crt=deploy/config/mqtt-certs/ca.crt \
  --from-file=server.crt=deploy/config/mqtt-certs/server.crt \
  --from-file=server.key=deploy/config/mqtt-certs/server.key
```

`DATABASE_URL` in `.env` already uses host `db` (deploy.ps1 writes
`postgresql+asyncpg://postgres:<pw>@db:5432/amphive`), which resolves to the
headless `db` Service — no edit needed. `amphive-env` must also carry
`POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` (the StatefulSet reads
them); add them to `.env` if absent. See `secret.example.yaml` for the full key
list.

## 3. Apply

```sh
kubectl apply -k deploy/k8s/
# or, without kustomize (order: namespace first):
#   kubectl apply -f namespace.yaml
#   kubectl apply -f postgres.yaml -f mosquitto.yaml -f backend.yaml -f frontend.yaml
```

## 4. Verify

```sh
kubectl -n amphive get pods,svc,pvc
kubectl -n amphive rollout status deploy/backend
kubectl -n amphive logs deploy/backend | tail        # migrations run on boot
curl http://<node-ip>:30080/                          # frontend SPA
curl http://<node-ip>:30080/api/health                # -> {"status":"healthy",...}
```

The backend liveness/readiness probe hits **`GET /api/health`** (verified in
`backend/main.py` — returns 200 with no DB dependency).

An empty PVC just boots a fresh DB and the backend runs Alembic migrations. To
cut over real data, restore a `pg_dump` into the StatefulSet (see the comment
at the top of `postgres.yaml`).

## Ports & the MQTT / relay implication

| Service | Type | Port | Reachable at |
|---------|------|------|--------------|
| `frontend-nodeport` | NodePort | **30080** → 80 | `http://<node-ip>:30080` |
| `mqtt-tls` | NodePort | **30883** → 8883 | `mqtts://<node-ip>:30883` |
| `mqtt` | ClusterIP | 1883 | in-cluster only |
| `backend` | ClusterIP | 8000 | in-cluster (nginx proxies to it) |
| `db` | Headless | 5432 | in-cluster only |

**MQTT NodePort 30883:** 8883 is deliberately **not** host-published, because
the compose broker already binds host `8883` on this VM — a NodePort of 8883
(outside the 30000-32767 range anyway) would clash. TLS terminates at mosquitto
inside the pod. To move gateways onto k3s, repoint the relay upstream /
firewall / any LB from `mqtts://8.231.81.12:8883` to `<node-ip>:30883`, and open
tcp:30883 on the node's firewall.

## Adding home nodes later (node 1 → cluster)

This is the server (node 1). On the server, get the join token:

```sh
sudo cat /var/lib/rancher/k3s/server/node-token
```

On each home box (agent), join with:

```sh
curl -sfL https://get.k3s.io | \
  K3S_URL=https://<node1-ip>:6443 \
  K3S_TOKEN=<node-token> sh -
```

Note: the Postgres PVC is `local-path` (node-local storage) — the Postgres pod
stays pinned to node 1. Schedule stateless workloads (backend/frontend) freely;
keep DB affinity to node 1 (or move to a networked StorageClass) before spreading
stateful pods.
