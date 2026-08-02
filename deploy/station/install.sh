#!/usr/bin/env bash
# Install the station's four user units on the machine at the bench.
#
# Idempotent, and does nothing that needs root — see README.md for the one step
# that does (adding the operator to `dialout`, without which no USB reader can
# be opened at all) and why it is not in here.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
units="${HOME}/.config/systemd/user"

if [[ "${repo}" != "${HOME}/prog/almagest" ]]; then
    # The units hardcode %h/prog/almagest. Rewriting them at install time would
    # make the checked-in file and the running one differ, which is exactly the
    # sort of drift that makes a bench machine unexplainable a year later.
    echo "This checkout is at ${repo}, but the units expect ${HOME}/prog/almagest." >&2
    echo "Clone it there, or edit WorkingDirectory in deploy/station/*.service." >&2
    exit 1
fi

command -v uv >/dev/null || {
    echo "uv is not on PATH. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
}

echo "==> syncing the two venvs the station runs"
# Not `--all-extras`: that pulls `spidev` for the RC522, which needs to compile
# and fails on a Jetson Nano — and this station has no SPI reader anyway.
(cd "${repo}/idcodec" && uv sync --quiet)
(cd "${repo}/backend" && uv sync --quiet)
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
    install -m 0644 "${unit}" "${units}/"
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
