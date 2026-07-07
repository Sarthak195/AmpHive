# RETIRED — do not deploy from these manifests

**Decision 2026-07-07 (TD#15):** the live deployment is **Docker Compose on
the GCP VM** (`deploy/docker/docker-compose.prod.yml` via
`deploy/scripts/deploy.ps1`), and these K3s manifests are retired,
unmaintained reference material. They have diverged from production:

- In-cluster Postgres (PVC) — prod runs the `postgres:15-alpine` container on
  the VM.
- Docker Hub images `sarthak195/amphive-*:latest` — prod builds from source;
  these are stale/unpublished.
- The backend Deployment omits `JWT_SECRET_KEY`, Razorpay, and MQTT
  credentials — auth/payments would be unconfigured.
- The mosquitto Deployment predates broker auth (no passwd file mount).
- No resource limits; STUN/DERP ports unexposed.

They are kept only as a starting point if Kubernetes ever becomes worth it
(multi-node, autoscaling). Expect to rewrite, not apply. See
[docs/DEPLOYMENT.md](../../docs/DEPLOYMENT.md) for the canonical deployment.
