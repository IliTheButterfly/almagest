#!/usr/bin/env bash
#
# Deploy or update Almagest on the cluster.
#
#   scripts/k8s-deploy.sh              # deploy the current commit
#   scripts/k8s-deploy.sh sha-a1b2c3   # deploy a specific tag (rollback, too)
#
# The order below is the whole point of this script, and it is not the obvious
# one. SQLite tolerates exactly one writer, so the API must be *down* while the
# schema changes:
#
#   1. scale the API to zero and wait for the pod to actually be gone
#   2. run `alembic upgrade head` as a Job, alone with the volume
#   3. only then apply the new manifests, which bring the API back
#
# If step 2 fails, step 3 never runs — so the failure leaves the old code and
# the old schema intact, and recovery is one command with the previous tag.
# The cost is a minute of downtime per deploy, which is the correct trade for a
# single-user inventory system on a single-writer database.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

OVERLAY="deploy/overlays/aether"
NAMESPACE="ili"
REGISTRY="ghcr.io/ilithebutterfly"
IMAGES=(almagest-api almagest-web)

TAG="${1:-sha-$(git rev-parse --short=12 HEAD)}"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mfatal:\033[0m %s\n' "$*" >&2; exit 1; }

# --- Preflight -------------------------------------------------------------
# Everything that can be checked cheaply is checked before anything is changed,
# because the first destructive step takes the API offline.

command -v kubectl >/dev/null || die "kubectl is not on PATH"

kubectl auth can-i get deployments >/dev/null 2>&1 \
  || die "no access to namespace $NAMESPACE — check your kubeconfig context"

say "target: namespace $NAMESPACE, tag $TAG"

# The TLS certificate is not in the repository (it is a real private key), so a
# fresh clone deploying for the first time would otherwise get as far as an
# nginx pod stuck on a missing mount.
kubectl get secret almagest-tls >/dev/null 2>&1 \
  || die "secret/almagest-tls is missing — run 'make k8s-tls' first (needs certs/, built by 'make certs')"

# Confirm both images exist before taking anything down. Without this check the
# failure mode is: API scaled to zero, migration Job in ImagePullBackOff, and
# the system down until someone reads pod events. GHCR issues anonymous pull
# tokens for public packages, so no credentials are needed here.
for image in "${IMAGES[@]}"; do
  token="$(curl -fsSL "https://ghcr.io/token?scope=repository:ilithebutterfly/${image}:pull" \
           | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)" || {
    printf 'warning: could not reach ghcr.io to verify %s:%s — continuing\n' "$image" "$TAG" >&2
    continue
  }
  curl -fsSL -o /dev/null \
    -H "Authorization: Bearer $token" \
    -H 'Accept: application/vnd.oci.image.index.v1+json' \
    -H 'Accept: application/vnd.docker.distribution.manifest.list.v2+json' \
    -H 'Accept: application/vnd.oci.image.manifest.v1+json' \
    -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
    "https://ghcr.io/v2/ilithebutterfly/${image}/manifests/${TAG}" \
    || die "$REGISTRY/$image:$TAG is not in the registry.
       If you just pushed, CI is probably still building it:
         gh run watch \$(gh run list --workflow=release.yml --limit=1 --json databaseId -q '.[0].databaseId')"
  say "found $REGISTRY/$image:$TAG"
done

# --- Pin the tag -----------------------------------------------------------
# Written into the tracked overlay rather than passed to `kubectl set image`,
# so the file and the cluster agree and `git log` records what was deployed.

for image in "${IMAGES[@]}"; do
  python3 - "$OVERLAY/kustomization.yaml" "$REGISTRY/$image" "$TAG" <<'PY'
import re, sys
path, name, tag = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path).read()
pattern = re.compile(
    r"(- name: " + re.escape(name) + r"\n(\s+)newTag: )\S+"
)
new, count = pattern.subn(lambda m: m.group(1) + tag, text)
if count != 1:
    sys.exit(f"expected exactly one newTag for {name} in {path}, found {count}")
if new != text:
    open(path, "w").write(new)
PY
done

# --- 1. Stop the writer ----------------------------------------------------

if kubectl get deployment almagest-api >/dev/null 2>&1; then
  say "scaling the API to zero (single-writer database)"
  kubectl scale deployment almagest-api --replicas=0
  # `rollout status` does not report on a scale-to-zero, so wait on the pods
  # themselves. Without this the migration Job can start while the old API is
  # still shutting down, which is the two-writer case this whole ordering
  # exists to prevent.
  kubectl wait --for=delete pod \
    -l app.kubernetes.io/name=almagest,app.kubernetes.io/component=api \
    --timeout=180s 2>/dev/null || true
else
  say "no existing API deployment — this is a first install"
fi

# --- 2. Migrate ------------------------------------------------------------

JOB_NAME="almagest-migrate-$(printf '%s' "$TAG" | tr -c 'a-z0-9' '-' | cut -c1-24)-$RANDOM"
say "running migrations as job/$JOB_NAME"

# Jobs are immutable, so each run is a new object. Clean up any leftover from a
# previous run of the same tag first; named explicitly, never by selector.
kubectl delete job "$JOB_NAME" --ignore-not-found >/dev/null 2>&1 || true

# The ConfigMap the Job reads through envFrom, and the volume it writes to, may
# not exist yet on a first install — so these two go in ahead of the Job rather
# than with the rest of the manifests. Applied directly rather than through
# kustomize because the alternative is filtering rendered YAML by string match,
# which is fragile in exactly the place that must not be. Step 3's `apply -k`
# re-applies both a moment later and adds the common labels.
kubectl apply -n "$NAMESPACE" -f deploy/base/config.yaml -f deploy/base/pvc.yaml

sed -e "s|__JOB_NAME__|$JOB_NAME|" -e "s|__IMAGE__|$REGISTRY/almagest-api:$TAG|" \
  deploy/jobs/migrate.yaml | kubectl create -f -

if ! kubectl wait --for=condition=complete "job/$JOB_NAME" --timeout=600s; then
  printf '\n'
  kubectl logs "job/$JOB_NAME" --tail=100 || true
  die "migration failed — the API is still scaled to zero and the schema is unchanged.
       Nothing new was deployed. Bring the previous version back with:
         scripts/k8s-deploy.sh <previous-tag>
       or just restart the current one:
         kubectl scale deployment almagest-api --replicas=1"
fi
kubectl logs "job/$JOB_NAME" --tail=20 || true
kubectl delete job "$JOB_NAME" --ignore-not-found >/dev/null

# --- 3. Roll out -----------------------------------------------------------

say "applying manifests"
# The model Deployments declare `replicas: 0`, which is right as a *default* -
# nothing may hold the GPU unasked. But it means a plain apply scales down
# whichever model you are currently using, mid-answer, and for the 27B mid-startup:
# it takes minutes to load, so a deploy during that window silently undoes it and
# the next attempt starts from nothing. That happened three times before it was
# noticed.
#
# So the running replica count is captured and put back. The manifest still owns
# the default for a fresh cluster; the cluster owns what is running right now.
say "remembering which models are running"
declare -A MODEL_REPLICAS=()
for model in almagest-llm almagest-llm-27b; do
  MODEL_REPLICAS["$model"]="$(kubectl get deploy "$model" -n "$NAMESPACE" \
    -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"
done

kubectl apply -k "$OVERLAY"

for model in "${!MODEL_REPLICAS[@]}"; do
  want="${MODEL_REPLICAS[$model]:-0}"
  [ "$want" = "0" ] && continue
  say "restoring $model to $want replica(s)"
  kubectl scale "deploy/$model" -n "$NAMESPACE" --replicas="$want" >/dev/null
done

say "waiting for rollout"
kubectl rollout status deployment/almagest-api --timeout=300s
kubectl rollout status deployment/almagest-web --timeout=300s

say "deployed $TAG"
kubectl get all -l app.kubernetes.io/part-of=almagest
printf '\n  https://almagest.aether.lan:30443/   (almagest.aether.lan -> 192.168.85.101)\n'
printf '  The port is not yet reconciled with the portless URL that tags carry;\n'
printf '  see "The port, which is not solved" in deploy/README.md.\n\n'
