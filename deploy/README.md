# Deploying Almagest

Kubernetes manifests for the `aether` cluster, namespace `ili`. The shape and
the reasoning behind it are in [ADR 0009](../docs/adr/0009-cluster-deployment-and-the-443-problem.md);
this file is the operational half.

`docker compose` (repo root) remains the desktop path and is not generated from
any of this.

## Two deployment targets, not one

Everything below is the **cluster**: the Kubernetes manifests, one API replica on
an RWO volume, nginx serving the PWA over TLS.

The other one is **the machine at the bench** — API, PWA, device bridge and a
kiosk browser, all on loopback, all as one user with no root, on a Jetson or a Pi
or a laptop with a Flipper on a cable. It is not a cut-down cluster and does not
share these manifests: see **[station/README.md](station/README.md)**.

## The shape

```
  :30443 NodePort on 192.168.85.101   (node "neon")
    -> almagest-web    nginx + the built PWA, terminates TLS
         |-- /       -> static assets, SPA fallback
         |-- /api/   -> almagest-api:8000
         '-- /s/...  -> almagest-api:8000   (the tag-tap redirect)

  almagest-api         FastAPI, replicas: 1, strategy: Recreate
    '-- /data         -> pvc/almagest-data (5Gi, local-shared, RWO)

  almagest-backup      CronJob, 03:17 daily, keeps 14
  almagest-maintenance CronJob, 03:42 daily, no volume — talks to the API
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

### The port, which is not solved

The cluster cannot serve **443**: there is no ingress controller, no
LoadBalancer, `hostPort` is blocked by PodSecurity, and the node-port range is
the standard 30000–32767. So the app answers on `https://almagest.lan:30443`,
while every NFC tag and printed QR is specified to carry a **portless**
`https://almagest.lan/s/{short_id}`.

**Do not provision any tag until that is reconciled**, or the tag will carry an
origin that never resolves — and a tag cannot be rewritten remotely. Nothing has
been provisioned yet, so this currently costs nothing.

Note that a router *port-forward* does not fix it: a LAN client resolves
`almagest.lan`, gets the node's address, and connects directly, so the router is
never in the path. It needs a **reverse proxy at whatever address `almagest.lan`
resolves to**, forwarding 443 to `192.168.85.101:30443`. Failing that, an ingress
controller or an extended node-port range — both cluster-admin changes — would
let `ALMAGEST_BASE_URL` drop the port with no other change here.

**This is deferred on purpose, not forgotten.** Nothing resolves `almagest.lan`
today and nothing needs to until tags are provisioned; the deployment is reached
by address. The intended fix is OpenWRT on the router providing both the DNS
entry and the 443 reverse proxy (a 10 GbE firewall could host the proxy instead).
Phones will be on the same subnet as the server, so they reach the node directly
— the WireGuard tunnel is only how an off-subnet workstation gets in, is out of
scope here, and must not be modified. When the proxy exists, the change on this
side is one ConfigMap value and a redeploy.

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
| `make k8s-maintenance-now` | run the nightly cache maintenance immediately |
| `make k8s-caches` | what each derived cache's last check found |

## Backups

`almagest-backup` runs SQLite's online backup API nightly, integrity-checks the
**copy** (a backup that will not open is worse than none, because it looks like
one), and keeps 14. It writes to `/data/backups` on the same PVC, which protects
against corruption but *not* against losing the disk — `make k8s-backup-pull`
is the off-cluster half and is currently manual.

## Cache maintenance

`almagest-maintenance` runs at 03:42, after the backup rather than before it: if a
night's run turns up drift, the backup taken 25 minutes earlier is the only copy
holding the *un-repaired* state, and that is what can still show how a write path
broke.

It does two different things to two different kinds of cache:

- **`location_occupancy` is rebuilt.** It is *designed* to go stale — triggers on
  ledger insert and lot relocation mark rows dirty and leave the recompute to a
  batch pass, because a tree walk on every ledger write is exactly what that
  design avoids. A dirty row is the mechanism working.
- **Lot balances and reserved quantities are only checked.** Both are maintained
  incrementally on every write, so drift there is a bug in a write path, not
  expected staleness. Rebuilding it nightly would erase the symptom and leave the
  cause, so the wrong numbers would return the next day with nothing recorded.
  `make k8s-caches` shows what the last check found; the repair is
  `POST /api/system/caches/rebuild`, run deliberately once the cause is known.

**Drift exits non-zero, so the Job fails.** There is no metrics stack here, and a
failed Job is the only channel that surfaces a nightly correctness problem
without one — it appears in `kubectl get jobs` and `failedJobsHistoryLimit: 7`
keeps it for a week. `backoffLimit: 0` pairs with that: drift is a state of the
data, so retrying would only record the same finding six more times. Exit 1 means
the check ran and found drift; exit 2 means it could not reach the API.

**The pod mounts no volume.** A rebuild writes, and SQLite on an RWO volume has
exactly one writer — the API. So the work runs inside the API process and this Job
only asks it to, over HTTP. That is the same division ADR 0005 draws for the
extraction worker, and the reason `almagest-backup` next door goes to the trouble
of opening the database `mode=ro`.

## Things that are load-bearing

- **`replicas: 1` and `strategy: Recreate` on the API.** Two SQLite writers is
  corruption, and a RollingUpdate deadlocks trying to attach an RWO volume to a
  second pod. Neither announces itself.
- **`kubectl apply --dry-run=server` cannot validate a `nodePort`.** It accepts
  an out-of-range value and reports success; the range is enforced by the
  service port allocator, which dry-run bypasses. Everything else here was
  dry-run validated and that held — this one field is the exception.
- **The API image needs `--extra labels`.** `app/api/routes/labels.py` imports
  Pillow at module scope and `app.main` always includes that router, so without
  the extra the image cannot import its own application. Not `--all-extras`:
  ADR 0005 keeps `datasheets` out of this image.
- **Migrations are never run on startup.** On boot, a failed migration and a
  failed rollout look identical and a rollback becomes undiagnosable.
- **Resource limits on everything.** There is no ResourceQuota in `ili`, so
  these limits are the only thing standing between a runaway process and the
  co-tenant workloads.
- **`ALMAGEST_BASE_URL` is effectively permanent.** It is stamped into physical
  tags; changing it does not migrate them.
