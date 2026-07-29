"""The identity codec: short IDs and tag payloads, with no dependencies at all.

Two components need these rules and only these rules — the API, which mints and
resolves codes and writes `location_tags`, and the station agent on a Raspberry
Pi, which reads a tag several times a second and has to decide locally whether
the container in front of it is the same one as last poll.

Sharing them is not a tidiness preference. `normalize_tag_uid` produces the key
`location_tags.tag_uid` is written with, so bind time and resolve time must fold
a UID identically or a provisioning walk silently reports a cabinet as swapped;
and `parse_ndef_url` verifies the mod-37 check symbol, so a second copy that
skipped it would forward a mis-read short id as fact. One implementation is the
only way to guarantee agreement, and **the fix is never to copy them**.

Why this is its own distribution rather than part of the backend: the agent used
to depend on `almagest-backend` for exactly these functions and got fastapi,
sqlalchemy, alembic and pint along for the ride — about 25 wheels it never
imports, on a Pi 4 running kiosk Chromium next to it. `tests/test_stdlib_only.py`
is the guard that keeps this package free of them.

Submodules, not a flat namespace:

* `idcodec.shortid` — the Crockford base32 codec (`generate`, `validate`,
  `normalize`, `format_display`). Everything session-taking stays in
  `app.services.shortid`, which re-exports this.
* `idcodec.tagpayload` — `normalize_tag_uid` and `parse_ndef_url`, re-exported
  by `app.services.provisioning`.
"""

from __future__ import annotations

from idcodec import shortid, tagpayload

__all__ = ["shortid", "tagpayload"]
