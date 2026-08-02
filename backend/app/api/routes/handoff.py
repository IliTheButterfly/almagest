"""`/api/handoff/qr.svg` — carry on with this on your phone.

Iliana: *"it would be cool to be able to transfer a session to a phone by scanning
a QR code. That way I can go off with my phone and scan the containers as I pick
them up to verify if they are correct."*

**There is no session to transfer, and that is what makes this cheap.** A pick
walk's progress is already in the ledger the moment each take is recorded, and its
position is derived from what remains — exactly as the provisioning cursor is
derived from `location_tags` rather than stored. So handing the walk to a phone is
handing over a *URL*: no handoff table, no expiring token, no state to reconcile
when both screens are open at once. Two devices on one walk is not a conflict here,
it is two views of the same rows.

**A relative path only.** The QR is rendered from `{base_url}{path}` with the base
URL the server already knows (ADR 0001: `https://almagest.lan`), and the path is
validated to be a same-origin absolute path. Encoding a caller-supplied *absolute*
URL would turn this into an open redirect generator with a QR code on the front,
which is a phishing primitive rather than a feature — the phone scanning it has no
way to tell a handoff from a hostile link.

**SVG rather than PNG**: it is a few hundred bytes, scales to whatever the screen
is, and needs no `Pillow` round trip. The label printer path still uses PNG
because a printer wants pixels at a known DPI.
"""

from __future__ import annotations

import io
import re

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.config import get_settings

router = APIRouter(prefix="/api/handoff", tags=["handoff"])

#: A same-origin absolute path, and nothing that could leave the origin. Rejects
#: `//evil.example` (a protocol-relative URL, which is a *different host* despite
#: starting with a slash) and anything carrying a scheme.
_SAFE_PATH = re.compile(r"^/(?!/)[A-Za-z0-9\-._~!$&'()*+,;=:@/%?#\[\]]*$")

#: Well beyond any real deep link, and far below the point where a QR needs a
#: version so dense a phone camera struggles with it at arm's length.
MAX_PATH_LENGTH = 512


@router.get(
    "/qr.svg",
    response_class=Response,
    responses={200: {"content": {"image/svg+xml": {}}}},
)
def handoff_qr(
    path: str = Query(
        description="Same-origin absolute path to open on the phone, e.g. "
        "`/builds/12?tab=pick`. Absolute URLs are refused.",
        max_length=MAX_PATH_LENGTH,
    ),
) -> Response:
    """A QR encoding `{base_url}{path}`."""
    if _SAFE_PATH.match(path) is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "reason": "unsafe_handoff_path",
                "message": "give a same-origin absolute path such as /builds/12?tab=pick",
            },
        )

    # Imported here, not at module scope. `segno` lives in the `labels` extra,
    # which `backend/pyproject.toml` keeps out of the default install "so the
    # API image stays small" — and a module-scope import made that a lie: a
    # default `uv sync` produced a venv whose `app.main` could not be imported
    # at all, so uvicorn exited before binding and the failure named `segno`
    # rather than the missing extra. Deferring it means the rest of the API
    # runs and only this one route 500s, which is what "optional" has to mean.
    import segno

    target = f"{get_settings().base_url.rstrip('/')}{path}"
    buffer = io.BytesIO()
    # ECC "m" matches the label renderer: a screen is a clean scanning surface,
    # so the extra redundancy of "q" would only make the symbol denser.
    segno.make(target, error="m").save(buffer, kind="svg", scale=6, border=2)
    return Response(
        content=buffer.getvalue(),
        media_type="image/svg+xml",
        # The payload is a pure function of the path and the configured base URL,
        # and neither changes without a redeploy.
        headers={"Cache-Control": "public, max-age=3600"},
    )
