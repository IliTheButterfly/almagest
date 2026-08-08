#!/usr/bin/env bash
#
# Tunnel the deployed Almagest to localhost, and keep it up.
#
#   scripts/k8s-tunnel.sh              # web + API
#   scripts/k8s-tunnel.sh --model      # ...and Ollama, see the warning below
#   scripts/k8s-tunnel.sh --quiet      # only complain when something breaks
#
# ## Why this is a supervisor and not one `kubectl port-forward`
#
# `kubectl port-forward` is not a durable connection and does not pretend to be. It
# ends on an idle timeout, on a laptop suspend, and on any blip in the API-server
# connection — and it is broken by every deploy, since the API is
# `strategy: Recreate` and goes to zero replicas mid-deploy.
#
# It breaks in **two different ways**, and only one of them is the obvious one:
#
# 1. the process exits, and the port stops answering;
# 2. the process keeps running and the port keeps accepting connections, while
#    forwarding to a pod that no longer exists. `port-forward svc/x` resolves the
#    Service to one pod at startup and never re-resolves.
#
# The second is the common case after a deploy and the reason a plain restart loop
# is not enough — see `supervise` below, where it is measured rather than assumed.
#
# Either way the browser sees the same thing, and that is what makes it worth
# fixing: the tab is still open on `https://localhost:8443` and the next click
# returns a connection error indistinguishable from the application being broken.
# The natural response is to go and debug Almagest, which is healthy.
#
# So each forward gets a restart loop *and* a watchdog. A drop reconnects in about
# a second; a stale binding is detected within `PROBE_SECONDS` and rebound.
#
# ## Why the web service is here at all
#
# `make k8s-tunnel` used to forward only the API and the model, which meant the
# one surface a person wants to *look* at was the one they could not reach.
#
# And the forwarded port is the best way in even though the web Service is a
# NodePort on 30443, because of the certificate: it is issued by a private CA for
# `almagest.lan`, `localhost`, `127.0.0.1` and `192.168.0.13`. The hostname the
# deploy script prints — `almagest.aether.lan` — is **not** among those, and
# neither is the node IP, so browsing to the NodePort gives a name-mismatch
# warning even with the CA trusted. `localhost` is in the SANs, so this path
# validates cleanly.
#
# That is not cosmetic. A certificate warning costs you the browser's secure
# context, and Web NFC and `getUserMedia` are gated behind it — so tag writing
# and barcode scanning disappear rather than failing loudly (ADR 0001).
#
# Trust the CA once: `certs/ca.crt`, from `make certs`.
#
# ## Why the model forward is opt-in
#
# The Ollama endpoint has **no authentication of any kind**, which is why its
# Service is ClusterIP and why the old version of this comment noted that a
# port-forward "dies with the terminal". This script's whole purpose is that it
# does *not* die, so that reasoning no longer covers it: a self-healing tunnel
# holds an unauthenticated model server open on your loopback indefinitely.
#
# Still only loopback, and still authenticated by your kubeconfig — but it is a
# longer exposure than before, so it now takes a flag rather than coming along by
# default with the thing you wanted.

set -uo pipefail

NAMESPACE="ili"
QUIET=0
WANT_MODEL=0

for arg in "$@"; do
  case "$arg" in
    --model) WANT_MODEL=1 ;;
    --quiet) QUIET=1 ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'unknown argument: %s (try --help)\n' "$arg" >&2; exit 2 ;;
  esac
done

# service|local|remote|probe-url. Pipe-separated because a URL contains colons.
#
# The probe path per service is chosen to be cheap and to mean "the app is
# answering", not merely "something accepted a socket": kubectl accepts the
# connection even when the pod behind it is gone, which is exactly the failure the
# watchdog exists to catch.
FORWARDS=(
  "almagest-web|8443|443|https://127.0.0.1:8443/"
  "almagest-api|8000|8000|http://127.0.0.1:8000/api/system/health"
)
if [ "$WANT_MODEL" -eq 1 ]; then
  FORWARDS+=("almagest-llm|11434|11434|http://127.0.0.1:11434/api/tags")
fi

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mfatal:\033[0m %s\n' "$*" >&2; exit 1; }

command -v kubectl >/dev/null || die "kubectl is not on PATH"

# Without curl the watchdog cannot tell a working tunnel from a bound-but-dead one,
# so it degrades to process supervision and says so rather than implying a
# guarantee it is not providing.
HAVE_CURL=1
if ! command -v curl >/dev/null; then
  HAVE_CURL=0
  warn "curl is not on PATH — reconnecting on process exit only."
  warn "A deploy replaces the pod without killing kubectl, so that case will need a restart."
fi
kubectl auth can-i get pods >/dev/null 2>&1 \
  || die "no access to namespace $NAMESPACE — check your kubeconfig context"

# Fail before binding anything if a port is taken, rather than leaving one
# forward up and one in a silent restart loop that can never succeed.
for spec in "${FORWARDS[@]}"; do
  IFS='|' read -r _svc local_port _remote _probe <<<"$spec"
  # The probe runs in a subshell, so the descriptor it opens is closed with the
  # subshell and there is nothing to tidy up here. An `exec 3<&-` in this branch
  # looks like the careful thing to do and is a silent script-killer: fd 3 was
  # never open in *this* shell, so the redirection fails — and a failed `exec`
  # redirection terminates a non-interactive shell **before** `|| true` is
  # evaluated. Cost: this guard exited 1 with no message at all.
  if (exec 3<>"/dev/tcp/127.0.0.1/$local_port") 2>/dev/null; then
    die "127.0.0.1:$local_port is already in use — another tunnel is probably running"
  fi
done

PIDS=()
cleanup() {
  trap - EXIT INT TERM
  say "closing the tunnel"
  # The whole process group, not just the loops: `kubectl` is a child of each
  # loop, and killing only the loop leaves the forward holding the port — which
  # is the exact "already in use" state the preflight above then refuses to start
  # in. Found the hard way; a `trap` that misses a grandchild is worse than none.
  for pid in "${PIDS[@]}"; do
    kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# How often each forward is probed for actually working. Ten seconds because the
# case it exists for is a deploy, whose downtime is about a minute — so recovery
# lands within ten seconds of the new pod becoming ready.
PROBE_SECONDS=10

# Does the Service have at least one ready endpoint?
#
# This is the gate that stops the watchdog below from spinning. `almagest-llm`
# normally sits at zero replicas *on purpose* (it holds the GPU), so a probe that
# treated "not serving" as "rebind" would restart it forever against a Service
# with nothing behind it. "The Service has endpoints and my forward still does not
# work" is the precise condition worth acting on.
has_endpoints() {
  local ips
  ips="$(kubectl -n "$NAMESPACE" get endpoints "$1" \
         -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null)"
  [ -n "$ips" ]
}

# Is the tunnel actually carrying traffic? `-k` deliberately: this is a liveness
# check, not a trust decision, and the CA may well not be installed in whatever
# shell this runs from.
probe() {
  [ "$HAVE_CURL" -eq 1 ] || return 0
  curl -s -k -o /dev/null --max-time 5 "$1"
}

# One supervised forward. Each runs in its own process group (`set -m`) so
# `cleanup` can take the `kubectl` child with the loop.
#
# ## Why process supervision alone is not enough, which is the whole point
#
# The obvious version of this script restarts `kubectl` when it exits. That heals
# a network blip and **does not heal a deploy**, which is the most common way the
# tunnel dies.
#
# `kubectl port-forward svc/x` resolves the Service to *one pod* when it starts and
# never re-resolves. Replace that pod — `kubectl rollout restart`, or any deploy,
# since the API is `strategy: Recreate` — and kubectl carries on running, still
# holding the local port, forwarding to a pod that no longer exists. Every request
# fails and the supervisor sees a healthy child.
#
# Measured, not theorised: after a `rollout restart` of `almagest-web` the forward
# stayed up, `https://localhost:8443` returned nothing, and no drop was ever
# logged. So the watchdog probes the port and kills the forward itself when the
# Service has endpoints and the tunnel does not work. Killing it is the repair —
# the loop then rebinds to a live pod.
supervise() {
  local service="$1" local_port="$2" remote_port="$3" probe_url="$4"
  local attempts=0
  while true; do
    # `--address 127.0.0.1` explicitly rather than relying on the default: the
    # difference between loopback and 0.0.0.0 is the difference between "my
    # machine" and "the unauthenticated model server is on the LAN".
    kubectl -n "$NAMESPACE" port-forward --address 127.0.0.1 \
      "svc/$service" "$local_port:$remote_port" >/dev/null 2>&1 &
    local kpid=$!

    # Watch it. Two ways out: the child dies, or it lives on pointing at nothing.
    while kill -0 "$kpid" 2>/dev/null; do
      sleep "$PROBE_SECONDS"
      kill -0 "$kpid" 2>/dev/null || break
      has_endpoints "$service" || continue
      probe "$probe_url" && continue
      warn "$service:$local_port is bound but not serving — rebinding to a live pod"
      kill "$kpid" 2>/dev/null || true
      break
    done

    wait "$kpid" 2>/dev/null
    local code=$?
    attempts=$((attempts + 1))

    # A drop is not necessarily a fault — a deploy scaling the API to zero looks
    # exactly like this — so it is a warning rather than an error, and `--quiet`
    # keeps the routine ones off the terminal.
    if [ "$QUIET" -eq 0 ]; then
      warn "$service:$local_port dropped (exit $code) — reconnecting"
    fi

    # Backoff, bounded. A pod that is genuinely gone (mid-deploy, or scaled to
    # zero like almagest-llm normally is) would otherwise spin at one attempt per
    # second for as long as it takes somebody to notice.
    if   [ "$attempts" -lt 5 ];  then sleep 1
    elif [ "$attempts" -lt 20 ]; then sleep 3
    else sleep 15
    fi
  done
}

say "tunnel up — it reconnects on its own, including across a deploy"
printf '\n'
printf '  web    https://localhost:8443/         <- the PWA. Valid cert if certs/ca.crt is trusted\n'
printf '  API    http://127.0.0.1:8000/docs\n'
if [ "$WANT_MODEL" -eq 1 ]; then
  printf '  model  http://127.0.0.1:11434         <- UNAUTHENTICATED, loopback only\n'
fi
printf '\n  ctrl-c to stop\n\n'

set -m
for spec in "${FORWARDS[@]}"; do
  IFS='|' read -r service local_port remote_port probe_url <<<"$spec"
  supervise "$service" "$local_port" "$remote_port" "$probe_url" &
  PIDS+=("$!")
done
set +m

wait
