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
3. **`hostPort` is forbidden.** The namespace enforces PodSecurity `baseline`,
   which rejects `hostPort` outright. Binding the node's 443 directly from a pod
   is not available.
4. **A NodePort may be 443.** The API server's `--service-node-port-range` is
   extended below the usual 30000 floor; a server-side dry run of a Service with
   `nodePort: 443` is accepted.

Finding 4 is the one that decides this, and it is the one that would never have
been guessed — the 30000–32767 range is universal enough that most people treat
it as part of the protocol.

## Decision

**A single `NodePort` Service on port 443, fronted by our own nginx, serving the
PWA and proxying the API as one origin.**

```
router DNS: almagest.lan -> 192.168.85.101   (node "neon")

  phone --https://almagest.lan/s/4K7T-92M8--> :443 NodePort
     -> almagest-web   nginx, TLS from secret/almagest-tls
          |-- /       -> the built PWA
          |-- /api/   -> almagest-api:8000
          '-- /s/...  -> almagest-api:8000
```

No ingress controller, no LoadBalancer, no router port-forward.

## Why the port number is not negotiable

`https://almagest.lan/s/{short_id}` is written into every NFC tag and printed on
every QR label, with **no port in it**. A tag is a physical object glued to a
physical drawer; there is no migration that reaches it. The deployment therefore
has to meet the URL, not the other way round.

`nodePort: 30443` plus a DNAT rule on the router would also land on 443 from the
phone's point of view, and it was the expected answer before finding 4. It is
rejected because it moves a load-bearing piece of the tag contract into a
consumer router's config page — undocumented, unversioned, and invisible to
anyone reading this repository.

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
