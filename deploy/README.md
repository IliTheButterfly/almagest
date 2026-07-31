# Deploying Almagest

Kubernetes manifests for the `aether` cluster, namespace `ili`. The shape and
the reasoning behind it are in [ADR 0009](../docs/adr/0009-cluster-deployment-nodeport-443.md);
this file is the operational half.

`docker compose` (repo root) remains the desktop path and is not generated from
any of this.

## The shape

```
router DNS: almagest.lan -> 192.168.85.101   (node "neon")

  :443 NodePort
    -> almagest-web    nginx + the built PWA, terminates TLS
         |-- /       -> static assets, SPA fallback
         |-- /api/   -> almagest-api:8000
         '-- /s/...  -> almagest-api:8000   (the tag-tap redirect)

  almagest-api         FastAPI, replicas: 1, strategy: Recreate
    '-- /data         -> pvc/almagest-data (5Gi, local-shared, RWO)

  almagest-backup      CronJob, 03:17 daily, keeps 14
```

Everything is named `almagest-*` and labelled
`app.kubernetes.io/part-of=almagest`. That label is the substitute for a
namespace of our own — this cluster cannot provide one (namespaces are
cluster-scoped, the service account cannot create them, and there is no
Hierarchical Namespace Controller), and `ili` is shared with unrelated
production workloads.

> **`ili` is shared.** Never `kubectl delete` with a bare selector, `--all`, or
> a namespace scope. Never `kubectl apply --prune`. Both would reach the octans
> runner and the KubeVirt builder VM.

## First install

```bash
make certs          # private CA + cert for almagest.lan, into the gitignored certs/
make k8s-tls        # certs/ -> secret/almagest-tls
make k8s-secrets    # optional: distributor / LLM keys from .env
make k8s-deploy     # deploys the current commit
```

Then point `almagest.lan` at `192.168.85.101` in the router's DNS, and install
`certs/ca.crt` on every phone that will provision tags — `.lan` cannot obtain a
publicly trusted certificate, and without the CA the browser refuses the secure
context that Web NFC and the camera require.

## Updating

Images are built by `.github/workflows/release.yml` on every push to `main`, and
on demand for a branch via `workflow_dispatch`. **Nothing here can build them
locally** — there is no container runtime on the dev box, which is why the build
lives in CI rather than in a Makefile target.

```bash
make k8s-deploy                       # whatever `git rev-parse HEAD` says
make k8s-deploy TAG=sha-a1b2c3d4e5f6  # a specific build
```

`scripts/k8s-deploy.sh` runs this order, and the order is the point:

1. **Preflight.** Confirms the TLS secret exists and that *both* images are
   actually in GHCR, before touching anything. Without this check, a typo in a
   tag takes the API down and leaves it down in `ImagePullBackOff`.
2. **Pin.** Writes the tag into `overlays/aether/kustomization.yaml`, so the
   file and the cluster agree and `git log` on that file is an honest record of
   what has run.
3. **Scale the API to zero** and wait for the pod to be *gone*, not merely
   terminating.
4. **Migrate** as a one-shot Job that has the volume to itself.
5. **Apply** the manifests, which bring the API back on the new image.

If step 4 fails, step 5 never runs: the old code and old schema are intact and
the script prints the one command that brings the previous version back. The
cost of all this is roughly a minute of downtime per deploy, which is the right
trade for a single-user inventory system on a single-writer database.

### Rolling back

Same command, previous tag. `git log deploy/overlays/aether/kustomization.yaml`
lists them.

```bash
make k8s-deploy TAG=sha-<previous>
```

A rollback across a migration is **not** automatic — Alembic downgrades exist
and are tested against a database with rows in it, but the deploy script only
ever runs `upgrade head`. Downgrade deliberately, by hand, with the API scaled
to zero.

## Day to day

| | |
|---|---|
| `make k8s-status` | everything Almagest owns, and nothing else |
| `make k8s-logs` | follow the API log |
| `make k8s-shell` | shell in the API pod |
| `make k8s-diff` | what a deploy would change, without changing it |
| `make k8s-backup-now` | run the nightly backup immediately |
| `make k8s-backup-pull` | copy the newest backup off the cluster |

## Backups

`almagest-backup` runs SQLite's online backup API nightly, integrity-checks the
**copy** (a backup that will not open is worse than none, because it looks like
one), and keeps 14. It writes to `/data/backups` on the same PVC, which protects
against corruption but *not* against losing the disk — `make k8s-backup-pull`
is the off-cluster half and is currently manual.

## Things that are load-bearing

- **`replicas: 1` and `strategy: Recreate` on the API.** Two SQLite writers is
  corruption, and a RollingUpdate deadlocks trying to attach an RWO volume to a
  second pod. Neither announces itself.
- **`nodePort: 443`, exactly.** Every provisioned tag carries a portless URL.
  See ADR 0009.
- **Migrations are never run on startup.** On boot, a failed migration and a
  failed rollout look identical and a rollback becomes undiagnosable.
- **Resource limits on everything.** There is no ResourceQuota in `ili`, so
  these limits are the only thing standing between a runaway process and the
  co-tenant workloads.
- **`ALMAGEST_BASE_URL` is effectively permanent.** It is stamped into physical
  tags; changing it does not migrate them.
