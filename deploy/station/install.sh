#!/usr/bin/env bash
# Install the station's user units on the machine at the bench.
#
# Idempotent, and does nothing that needs root — see README.md for the one step
# that does (adding the operator to `dialout`, without which no USB reader can
# be opened at all) and why it is not in here.
#
#   ./install.sh                       # this machine keeps its own inventory
#   ./install.sh --upstream cluster    # this machine is a window onto the cluster
#
# The two differ in one thing only: what is listening on 127.0.0.1:8000. See
# README.md, "Where the data comes from".
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
units="${HOME}/.config/systemd/user"

# `local` is the default because it is the mode that needs nothing outside the
# machine: no cluster, no token, no network. A station that cannot reach the
# cluster should come up holding its own data rather than not at all.
upstream="local"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --upstream)
            upstream="${2:-}"
            shift 2
            ;;
        --upstream=*)
            upstream="${1#*=}"
            shift
            ;;
        *)
            echo "unknown argument: $1" >&2
            echo "usage: install.sh [--upstream local|cluster]" >&2
            exit 2
            ;;
    esac
done
if [[ "${upstream}" != "local" && "${upstream}" != "cluster" ]]; then
    echo "--upstream must be 'local' or 'cluster', not '${upstream}'" >&2
    exit 2
fi

# The units hardcode `%h/prog/almagest`, which is where this is normally cloned.
# A checkout somewhere else is not refused — it gets a drop-in. Editing the
# checked-in units instead would make the file in git and the file that runs
# differ, which is the drift that makes a bench machine unexplainable a year
# later; a drop-in is a separate, visible file that `systemctl cat` prints right
# underneath the original.
drop_in_needed=0
if [[ "${repo}" != "${HOME}/prog/almagest" ]]; then
    drop_in_needed=1
    echo "note: this checkout is at ${repo}, not ${HOME}/prog/almagest."
    echo "      Writing per-unit drop-ins so the units point at it."
fi

command -v uv >/dev/null || {
    echo "uv is not on PATH. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
}

echo "==> syncing the venvs the station runs"
# Extras are chosen per project rather than with `--all-extras`, and both
# choices matter:
#
#   backend  `--extra labels`, exactly as backend/Dockerfile does. The
#            handoff QR route needs `segno` at call time — the import is inside
#            the route body, so without the extra the API still starts and only
#            that one route refuses, with a message naming the extra. Installing
#            it here means the station has the whole API rather than all of it
#            but one page. Not `--all-extras`, which would also pull the
#            `datasheets` parser the API is specified never to install.
#
#   agent    `--extra flipper` only. `--all-extras` pulls `spidev` for the
#            RC522, which builds from source and fails on a Jetson Nano — and a
#            station whose reader is on USB has no SPI device to talk to.
(cd "${repo}/idcodec" && uv sync --quiet)
(cd "${repo}/deviceagent" && uv sync --quiet --extra flipper)

if [[ "${upstream}" == "local" ]]; then
    (cd "${repo}/backend" && uv sync --quiet --extra labels)
    echo "==> migrating the database"
    (cd "${repo}/backend" && uv run alembic upgrade head)
else
    # Neither the venv nor the schema is synced in cluster mode, and the second
    # omission is the one that matters: `alembic upgrade head` would *create*
    # `data/almagest.db`, empty and migrated and never read by anything. A file
    # that looks exactly like the station's inventory and is not it is the kind
    # of thing someone restores from a year later.
    echo "==> skipping the local database (upstream is the cluster)"

    command -v "${HOME}/.local/bin/kubectl" >/dev/null || {
        echo "kubectl is not at ~/.local/bin/kubectl — the tunnel unit runs it by" >&2
        echo "absolute path. See README.md, 'Where the data comes from'." >&2
        exit 1
    }
    [[ -r "${HOME}/.kube/config" ]] || {
        echo "~/.kube/config is missing. The station needs its own credential —" >&2
        echo "not the workstation's. See deploy/station/cluster-access.yaml." >&2
        exit 1
    }
fi

if [[ ! -f "${repo}/frontend/dist/index.html" ]]; then
    # Built elsewhere on purpose: Node 22 needs glibc 2.28 and Ubuntu 18.04 has
    # 2.27, so the Nano cannot build the PWA even slowly. README.md has the rsync.
    echo "WARNING: frontend/dist is missing — the kiosk will serve a 503 until it" >&2
    echo "         is built on a workstation and copied over. See README.md." >&2
fi

echo "==> installing user units into ${units}"
mkdir -p "${units}"
for unit in "${repo}"/deploy/station/almagest-station-*.service; do
    name="$(basename "${unit}")"
    install -m 0644 "${unit}" "${units}/"
    # The kiosk unit has no path in it at all — it runs chromium against a URL —
    # so it never needs a drop-in, and writing an empty `[Service]` one would be
    # a file that says nothing and outlives the reason it was written.
    if [[ "${name}" == *-kiosk.service ]]; then
        rm -f "${units}/${name}.d/checkout.conf"
        rmdir "${units}/${name}.d" 2>/dev/null || true
    elif [[ "${drop_in_needed}" == 1 ]]; then
        # `WorkingDirectory` and the paths inside `ExecStart` both move, and
        # `ExecStart=` must be cleared before being set again or systemd appends
        # a second command rather than replacing the first.
        mkdir -p "${units}/${name}.d"
        {
            echo "# Written by deploy/station/install.sh — this checkout is not at"
            echo "# \$HOME/prog/almagest. Delete this directory if it ever moves back."
            echo "[Service]"
            case "${name}" in
                *-api.service)
                    echo "WorkingDirectory=${repo}/backend"
                    ;;
                *-bridge.service)
                    echo "WorkingDirectory=${repo}/deviceagent"
                    ;;
                *-upstream.service)
                    # It runs the repo's own `scripts/k8s-tunnel.sh`, so it moves
                    # with the checkout exactly like the api and web units do.
                    echo "WorkingDirectory=${repo}"
                    echo "ExecStart="
                    echo "ExecStart=${repo}/scripts/k8s-tunnel.sh --api-only --quiet"
                    ;;
                *-web.service)
                    echo "WorkingDirectory=${repo}"
                    echo "ExecStart="
                    echo "ExecStart=${HOME}/.local/bin/uv run --no-project --python 3.12 -- \\"
                    echo "    python ${repo}/deploy/station/station_web.py \\"
                    echo "    --dist ${repo}/frontend/dist --api http://127.0.0.1:8000 --port 8080"
                    ;;
            esac
        } > "${units}/${name}.d/checkout.conf"
    else
        # The checkout moved *back* to the default. Without this the installer is
        # only idempotent in one direction: a stale drop-in keeps pointing the API
        # at the old tree's `data/almagest.db`, and nothing in `systemctl status`
        # says so — you would have to think to run `systemctl cat`.
        if [[ -f "${units}/${name}.d/checkout.conf" ]]; then
            echo "note: removing a drop-in from an earlier checkout at another path"
            rm -f "${units}/${name}.d/checkout.conf"
            rmdir "${units}/${name}.d" 2>/dev/null || true
        fi
    fi
done

systemctl --user daemon-reload

# Exactly one of api/upstream is ever enabled: they both own 127.0.0.1:8000, and
# the loser of that race binds nothing and reports `active` while doing it. The
# unwanted one is *stopped* as well as disabled, because switching modes on a
# running station is the normal way this is used and `disable` alone would leave
# yesterday's process holding the port until the next reboot.
if [[ "${upstream}" == "local" ]]; then
    serve="almagest-station-api.service"
    retire="almagest-station-upstream.service"
else
    serve="almagest-station-upstream.service"
    retire="almagest-station-api.service"
fi
systemctl --user disable --now "${retire}" >/dev/null 2>&1 || true
systemctl --user enable "${serve}" \
                        almagest-station-web.service \
                        almagest-station-bridge.service \
                        almagest-station-kiosk.service

echo
echo "Installed, upstream=${upstream}."
if [[ "${upstream}" == "local" ]]; then
    echo "Start them with:"
    echo "  systemctl --user start almagest-station-{api,web,bridge,kiosk}"
else
    echo "Start them with:"
    echo "  systemctl --user start almagest-station-{upstream,web,bridge,kiosk}"
    echo
    echo "The tunnel is the whole dependency on the cluster. If the bench shows a"
    echo "502, look there first:"
    echo "  systemctl --user status almagest-station-upstream"
fi
echo
echo "The bridge cannot open a Flipper until this has been done once, as root:"
echo "  sudo usermod -aG dialout ${USER}   # then log out and back in"
