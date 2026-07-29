# idcodec — short IDs and tag payloads, standard library only

The rules that decide **what a scanned thing is called**, factored out of the
backend so the station Pi can have them without having FastAPI.

- `idcodec.shortid` — Crockford base32, 7 data symbols + a mod-37 check symbol,
  rendered `4K7T-92M8`. `generate`, `normalize`, `validate`, `is_valid`,
  `check_value`, `format_display`, `InvalidShortId`.
- `idcodec.tagpayload` — `normalize_tag_uid` and `parse_ndef_url`, plus
  `InvalidTagUid`.

## Why it is a separate distribution

Two components need these rules: the API, which mints codes and writes
`location_tags.tag_uid`, and `deviceagent`, which reads an NFC tag several times
a second on a Raspberry Pi 4 and must decide *locally* whether the container in
front of it is the same one as last poll.

They must agree exactly. A UID folded by a different rule is invisible to the
binding it should match while looking perfectly correct in both places — a
verification walk then reports a whole cabinet as swapped. And `parse_ndef_url`
verifies the check symbol, so a copy that skipped it would forward a mis-read
short id as fact.

The agent used to get that agreement by depending on `almagest-backend`, which
pulled fastapi, sqlalchemy, alembic and pint — roughly 25 wheels it never
imports — onto the Pi. This package is the promotion that comment prescribed.
**The fix was never to copy the functions.**

## What must never end up in here

Anything that needs a database session or application config. `allocate`,
`adopt`, `resolve` and `primary_short_id` stay in `app.services.shortid`;
`normalize_tag_uid`'s caller-facing error translation stays in
`app.services.provisioning`.

`dependencies = []` in `pyproject.toml` is not a coincidence and
`tests/test_stdlib_only.py` enforces it: it imports every module of the package
in a **fresh subprocess** and fails if `sys.modules` grew anything that is not
standard library. That is the guard against this regressing, because the way it
regresses is one convenient `from app.models.enums import EntityType`.

Consequently `format_display` takes a plain `str` for the entity type rather than
`EntityType`, and `idcodec.shortid.DISPLAY_PREFIXES` spells the values out.
`backend/tests/unit/test_shortid_display.py` asserts every enum member has an
entry, so the two cannot drift apart silently.

## Both sides re-export

Nothing in `backend/app` or `deviceagent/agent` imports `idcodec` and then also
keeps a second name for the same function. `app.services.shortid` and
`app.services.provisioning` re-export what moved, so every existing
`shortid.validate(...)` call site is untouched.

## Commands

```bash
make idcodec-check    # ruff, mypy --strict, pytest; folded into `make check`
cd idcodec && uv run pytest -q
```
