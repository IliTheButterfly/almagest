#!/usr/bin/env bash
# Install the station's four user units on the machine at the bench.
#
# Idempotent, and does nothing that needs root — see README.md for the one step
# that does (adding the operator to `dialout`, without which no USB reader can
# be opened at all) and why it is not in here.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
units="${HOME}/.config/systemd/user"

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

echo "==> syncing the two venvs the station runs"
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
(cd "${repo}/backend" && uv sync --quiet --extra labels)
(cd "${repo}/deviceagent" && uv sync --quiet --extra flipper)

echo "==> migrating the database"
(cd "${repo}/backend" && uv run alembic upgrade head)

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
systemctl --user enable almagest-station-api.service \
                        almagest-station-web.service \
                        almagest-station-bridge.service \
                        almagest-station-kiosk.service

echo
echo "Installed. Start them with:"
echo "  systemctl --user start almagest-station-{api,web,bridge,kiosk}"
echo
echo "The bridge cannot open a Flipper until this has been done once, as root:"
echo "  sudo usermod -aG dialout ${USER}   # then log out and back in"
