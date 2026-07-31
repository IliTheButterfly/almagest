# ADR 0009 — The cluster deployment: one origin on NodePort 443

**Status:** accepted, 2026-07-31
**Supersedes:** the "Ingress class and hostname pattern are unknown" open question

## Context

ADR 0001 settled the origin as `https://almagest.lan`, behind a private CA
installed on every phone. What was never settled was how a request to that name
actually reaches a pod. The deployment notes carried an explicit open question —
*ingress class and hostname pattern are unknown, ask before writing an Ingress
manifest* — because the service account has no cluster-scope reads, so
`ingressclasses` could not be listed, and there were no Ingresses in the
namespace to copy from.

The question could not be answered by asking either: it is not a preference, it
is a fact about the cluster. So it was probed.

## What the cluster actually offers

Four findings, each from a cheap, reversible probe rather than an assumption:

1. **No LoadBalancer provider.** A `LoadBalancer` Service sits at
   `EXTERNAL-IP: <pending>` indefinitely and emits no events at all — not even a
   failure. There is no MetalLB or equivalent.
2. **No default IngressClass.** An Ingress created without `ingressClassName`
   is admitted, is never defaulted to a class, and no controller ever populates
   its address. If a controller exists at all it is not the default and nobody
   knows its name.
3. **No ingress controller under any common name.** Nine Ingresses were created,
   one each for `nginx`, `traefik`, `haproxy`, `contour`, `istio`, `kong`,
   `cilium`, `openshift-default` and `public`. Not one was claimed or given an
   address. Combined with finding 2, there is no ingress on this cluster.
4. **`hostPort` and `hostNetwork` are forbidden.** The namespace enforces
   PodSecurity `baseline`, which rejects both, and namespace labels cannot be
   changed from here. Binding the node's 443 from a pod is unavailable.
5. **A NodePort may *not* be 443.** The range is the standard 30000–32767.

### A false positive worth recording

Finding 5 was initially recorded as its opposite. `kubectl apply --dry-run=server`
**accepts** a Service with `nodePort: 443` and reports it as created; the real
apply then rejects it with `provided port is not in the valid range`.

The reason is that the range is enforced by the **service port allocator**, which
runs during storage and which `--dry-run=server` bypasses entirely. So a
server-side dry run — normally the most trustworthy check available without
mutating anything — is *not* a valid test of a nodePort value. Only a real apply
is. Everything else in these manifests was validated by dry run and that
validation held; this one field is the exception.

## Decision

**A single `NodePort` Service on 30443, fronted by our own nginx, serving the PWA
and proxying the API as one origin.**

```
  client --https--> <whatever answers on 443 for almagest.lan>
                      '--> 192.168.85.101:30443   (node "neon")
                             -> almagest-web   nginx, TLS from secret/almagest-tls
                                  |-- /       -> the built PWA
                                  |-- /api/   -> almagest-api:8000
                                  '-- /s/...  -> almagest-api:8000
```

## The unresolved half: who answers on 443

`https://almagest.lan/s/{short_id}` is written into every NFC tag and printed on
every QR label, with **no port in it**. A tag is a physical object glued to a
drawer; no migration reaches it. The cluster cannot serve 443 — findings 1–5 —
so something outside it must, and forward to `192.168.85.101:30443`.

**A router port-forward does not solve this**, and the reason is easy to miss: a
LAN client asks the router for `almagest.lan`, gets the node's address, and then
connects to the node *directly*. The router is never in the path, so it has
nothing to forward. What is needed is a **reverse proxy at whatever address
`almagest.lan` resolves to**.

Until that exists, `ALMAGEST_BASE_URL` must include the port, and **no tag may be
provisioned**, because a tag written now would carry an origin that never
resolves. Nothing has been provisioned yet — this is a first install — so the
cost of the delay is zero, which is the only reason it is acceptable.

### What is known about how it will be closed

Settled in conversation on 2026-07-31, and deliberately **not built yet** —
nothing needs the name until tags are provisioned, and building it early would be
guessing at a configuration nobody has asked for:

- **The phones will be on the same subnet as the server.** So they reach
  `192.168.85.101:30443` directly, and none of this depends on the WireGuard
  tunnel. That tunnel is only how a developer workstation off that subnet reaches
  the cluster; it is out of scope here and must not be modified.
- **`almagest.lan` resolves to nothing today**, and does not need to. The
  deployment is reached by address until someone wants the name.
- **OpenWRT on the router can supply both halves** when the time comes: a DNS
  entry for `almagest.lan`, and a reverse proxy listening on 443 that forwards to
  `192.168.85.101:30443`. A 10 GbE firewall is also available and could host the
  proxy instead. Either way the proxy needs `certs/server.crt` and its key, or it
  can pass TLS through untouched to our nginx, which already holds the same
  certificate.

Cleaner escapes, if either becomes available: an ingress controller on the
cluster, or an extended `--service-node-port-range` so 443 can be taken directly.
Both are cluster-admin changes, and either would remove the need for an external
proxy entirely.

Whichever route is taken, the change inside this repository is **one ConfigMap
value** — dropping `:30443` from `ALMAGEST_BASE_URL` — and a redeploy. Nothing
about the manifests, the certificate or the tag payload moves. That is the
property the one-origin design was chosen for.

## Why one origin, rather than splitting the PWA and the API

Three independent things break if they are on different origins, and all three
break quietly:

- `frontend/src/lib/api/client.ts` uses `baseUrl: currentOrigin()`. There is no
  API-host setting and there should not be one.
- `/s/{short_id}` is answered by the **backend**, with a 302 to a **relative**
  path. The browser resolves it against the origin that served the redirect, so
  the PWA must be there.
- `getUserMedia` and `NDEFReader` need a secure context. Over plain http they do
  not fail, they are *absent* — every scanning affordance silently disappears.

## Consequences

- **The router is the only piece outside the repository**: it must resolve
  `almagest.lan` to the node. That is one DNS record, and it is the intended
  division — the cluster does not do LAN DNS.
- **Deploys have downtime, on purpose.** SQLite tolerates exactly one writer, so
  `scripts/k8s-deploy.sh` scales the API to zero, migrates alone with the
  volume, and only then rolls the new version out. `ReadWriteOnce` does not
  prevent the two-writer case by itself: both pods land on the same node, and
  RWO permits that.
- **A tag scanned off the LAN still resolves to nothing.** Unchanged from ADR
  0001, and still accepted. A tunnel can be pointed at the same name later
  without any manifest changing.
- **If an ingress controller is installed later**, switching is a Service type
  change plus one Ingress. Nothing about the origin, the certificate or the tag
  payload moves, which is the property worth having.
- The node IP (`192.168.85.101`) appears in this ADR and in the deploy script's
  closing message only as a convenience. It is not in any manifest; the Service
  does not care which node it lands on.
