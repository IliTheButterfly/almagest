# syntax=docker/dockerfile:1
#
# The PWA, plus the nginx that fronts both it and the API. Build context is the
# REPO ROOT, matching backend/Dockerfile, so one CI workflow can build both
# images with the same context.
#
#   docker build -f frontend/Dockerfile -t almagest-web .

FROM node:22-slim AS build

# corepack takes the pnpm version from package.json's `packageManager` field,
# so the image, CI and a laptop cannot drift onto different pnpm releases.
RUN corepack enable

WORKDIR /app

# Manifest layer first, so editing a component does not re-resolve the tree.
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN --mount=type=cache,target=/pnpm-store \
    pnpm config set store-dir /pnpm-store && pnpm install --frozen-lockfile

COPY frontend/ ./

# `pnpm build` is `tsc --noEmit && vite build`, so a type error fails the image
# build rather than shipping. vite.config.ts looks for ../certs and correctly
# finds nothing here — the dev HTTPS server is not part of this image.
RUN pnpm build


# The unprivileged variant, not plain `nginx`. The stock image starts its
# master process as root to bind port 80 and to own /var/cache/nginx, which
# trips every rule in PodSecurity `restricted`. This one ships as uid 101 with
# the cache and pid paths already relocated, so the pod needs no securityContext
# gymnastics and no writable root filesystem.
FROM nginxinc/nginx-unprivileged:1.27-alpine

# Build steps write into /etc/nginx, which the runtime user does not own.
USER root

# `Connection: upgrade` only when the client actually asked for it; a bare
# `proxy_set_header Connection upgrade` breaks ordinary keep-alive requests.
RUN printf '%s\n' \
    'map $http_upgrade $connection_upgrade {' \
    '    default upgrade;' \
    "    ''      close;" \
    '}' > /etc/nginx/conf.d/upgrade-map.conf \
    && rm /etc/nginx/conf.d/default.conf

COPY frontend/nginx.conf /etc/nginx/conf.d/almagest.conf
COPY --from=build /app/dist /usr/share/nginx/html

USER 101

# 8080 is cleartext (probes + redirect); 8443 is the real one. Both are above
# 1024 so no capability is needed to bind them — which is what lets this run as
# a non-root user at all.
EXPOSE 8080 8443

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null || exit 1
