"""No model, no image decode, no base64 and no `/v1/` call under `app/api/routes/`.

ADR 0005 says extraction runs outside the API, and ADR 0021 adds the first
image-to-model code in the repository — which makes this the moment the rule stops
being obviously true and starts needing a guard. The pure half of the vision path
(`enrichment/vision.py`) has no transport, the transport (`vision_openai_compat.py`) is
constructed only by a worker, and this test is what keeps the API process from
acquiring either by accident.

**Why a grep and not a design.** The design already forbids it: the worker fetches the
image over HTTP, hands bytes to a provider, and posts a result back. Nothing in the
routes package needs a pixel. But the *tempting* change is small and local — "just
decode the JPEG here to get its dimensions", "just call the model inline, it is only one
request" — and each one reads as reasonable in isolation. What makes them serious is
cumulative and invisible: the API is a single replica pinned to an RWO SQLite volume
(`CLAUDE.md`), so a route that blocks on a model holds the only writer the system has,
and a route that decodes an image puts an image pipeline in the process that must never
have one.

A grep is the right shape for that. It fails on the line that introduces the problem,
names the file, and costs nothing to run.

## What is deliberately *not* forbidden

Talking *about* these things. Docstrings in the routes package discuss base64 and
datasheets at length — `documents.py` explains why a JSON envelope would mean base64 for
every PDF — and forbidding the word would make the rule unexplainable in the place it
applies. So the check reads code with comments and docstrings stripped, which is also
what stops it from being satisfied by rewording a comment.
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

import pytest

import app.api.routes as routes_package

#: Substrings that must not appear in executable code under `app/api/routes/`.
#:
#: Each is a *specific* mechanism rather than a topic, so the test says what to do
#: instead rather than merely objecting:
#:
#: * `base64` — encoding an image for a model. The worker does this
#:   (`vision_openai_compat._b64`); a route serving bytes uses `FileResponse`.
#: * `/v1/chat/completions`, `/v1/completions` — an OpenAI-compatible model call.
#:   Belongs to a worker process, which can block for 39 seconds without holding the
#:   database.
#:
#:   Ollama's native `/api/chat` is deliberately **not** in this list: it collides with
#:   this repository's own `/api/chat` router prefix, and a check that fires on the
#:   application's own URL space is a check somebody deletes rather than fixes. The
#:   `ollama` entry in `FORBIDDEN_IMPORTS` and the module names below cover the same
#:   ground without the collision.
#: * `PIL`, `Image.open`, `cv2` — decoding pixels. `captures.width_px` is stored
#:   precisely so the API never has to.
#: * `enrichment.vision`, `vision_openai_compat`, `enrichment.extract`,
#:   `openai_compat` — the model-facing modules themselves. Importing one into a route is
#:   how the others arrive later.
FORBIDDEN = (
    "base64",
    "/v1/chat/completions",
    "/v1/completions",
    "Image.open",
    "cv2",
    "enrichment.vision",
    "vision_openai_compat",
    "enrichment.extract",
    "enrichment import extract",
    "openai_compat",
)

#: Import names that would pull an image or model library into the API process. Checked
#: separately from `FORBIDDEN` because `PIL` is three characters and appears inside
#: ordinary words; only an import of it is the problem.
FORBIDDEN_IMPORTS = ("PIL", "pillow", "tesseract", "zxing", "torch", "transformers", "ollama")


def _route_modules() -> list[Path]:
    directory = Path(routes_package.__file__).parent
    modules = sorted(path for path in directory.glob("*.py") if path.name != "__init__.py")
    # A guard on the guard: an empty glob would make every assertion below vacuous, and
    # the failure mode is silent — a moved package and a green test.
    assert len(modules) > 20, f"only found {len(modules)} route modules in {directory}"
    return modules


def _code_only(source: str) -> str:
    """`source` with comments and docstrings removed.

    Talking about base64 is allowed and necessary — see the module docstring — so the
    check has to look at code rather than at text. Implemented with `tokenize` rather
    than a regex because a regex over Python string literals is exactly the kind of
    approximation that eventually reports something a reader cannot reproduce.

    Line structure is **preserved** — comments and docstrings are blanked in place
    rather than removed. The import check below is line-oriented (`from x import y` has
    to survive as one line), and an earlier version of this that joined surviving tokens
    with newlines split every import across two lines and silently matched nothing. It
    passed against the whole repository while checking essentially nothing, which is the
    failure mode `test_the_fence_actually_fires` exists to catch.
    """
    lines = source.splitlines()
    blanked = list(lines)
    previous = tokenize.INDENT
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        # A string that stands alone as a statement is a docstring; one used as a value
        # is code. `previous` distinguishes them: a docstring follows a NEWLINE, INDENT
        # or DEDENT, never an operator or a name.
        is_docstring = token.type == tokenize.STRING and previous in {
            tokenize.NEWLINE,
            tokenize.NL,
            tokenize.INDENT,
            tokenize.DEDENT,
        }
        if token.type == tokenize.COMMENT or is_docstring:
            first, last = token.start[0], token.end[0]
            for row in range(first, last + 1):
                index = row - 1
                if row == first and row == last:
                    line = blanked[index]
                    blanked[index] = line[: token.start[1]] + line[token.end[1] :]
                elif row == first:
                    blanked[index] = blanked[index][: token.start[1]]
                elif row == last:
                    blanked[index] = blanked[index][token.end[1] :]
                else:
                    blanked[index] = ""
        if token.type not in {tokenize.NL, tokenize.NEWLINE}:
            previous = token.type
    return "\n".join(blanked)


@pytest.mark.parametrize("module", _route_modules(), ids=lambda path: path.name)
def test_no_model_or_image_work_in_a_route(module: Path) -> None:
    """The fence. ADR 0005 and ADR 0021's first consequence."""
    code = _code_only(module.read_text(encoding="utf-8"))
    offenders = [needle for needle in FORBIDDEN if needle in code]
    assert offenders == [], (
        f"{module.name} contains {offenders}. Model calls and image decoding belong in a "
        "worker process: the API is a single replica on an RWO SQLite volume, so a route "
        "that blocks on a model holds the only writer there is."
    )


@pytest.mark.parametrize("module", _route_modules(), ids=lambda path: path.name)
def test_no_image_or_model_library_imported_by_a_route(module: Path) -> None:
    code = _code_only(module.read_text(encoding="utf-8"))
    imports = [
        line
        for line in code.splitlines()
        if line.strip().startswith(("import ", "from "))
        and any(name in line for name in FORBIDDEN_IMPORTS)
    ]
    assert imports == [], (
        f"{module.name} imports an image or model library: {imports}. "
        "The API image deliberately does not carry one."
    )


#: A route module as somebody would actually break this rule: decode the image inline to
#: read its size, base64 it, post it to a model. Every line here is the plausible version
#: of the change, not a caricature.
_OFFENDING_ROUTE = '''
"""A route that talks about base64 in prose, which is allowed."""

import base64

from PIL import Image

from app.services.enrichment.vision import VisionRequest


def read_the_label(sha256: str) -> dict[str, object]:
    # Just decoding it here to get the dimensions; it is only one image.
    image = Image.open(f"data/{sha256}")
    payload = base64.b64encode(image.tobytes()).decode()
    return {"url": "http://model/v1/chat/completions", "image": payload, "req": VisionRequest}
'''

#: The same file with only *prose* mentions. Must pass, or the rule becomes
#: unexplainable in the place it applies — `documents.py` needs to be able to say why a
#: JSON envelope would mean base64 for every datasheet.
_INNOCENT_ROUTE = '''
"""Bytes are served raw, because a JSON envelope would mean base64 for every datasheet.

Nothing here calls /v1/chat/completions or imports PIL; the worker does that.
"""

from fastapi.responses import FileResponse


def read_document(sha256: str) -> FileResponse:
    # No base64, no Image.open — see the docstring.
    return FileResponse(f"data/{sha256}")
'''


def test_the_fence_actually_fires(tmp_path: Path) -> None:
    """The guard on the guard, and it has already earned its keep.

    An earlier `_code_only` joined surviving tokens with newlines, which split every
    `from x import y` across two lines. The import check then matched nothing and passed
    against the entire repository while verifying essentially nothing — the same shape as
    the idempotency test in `docs/HANDOFF-vision-and-bench.md` that asserted only the
    fields which were already idempotent.

    So both checks are run against a file that *should* fail and a file that should not,
    rather than only against a tree that currently passes.
    """
    offending = tmp_path / "bad_route.py"
    offending.write_text(_OFFENDING_ROUTE, encoding="utf-8")
    code = _code_only(offending.read_text(encoding="utf-8"))

    # In `FORBIDDEN`'s own order, and spelled out rather than asserted non-empty: a
    # length check would pass if only one of the four mechanisms were still detected.
    assert [needle for needle in FORBIDDEN if needle in code] == [
        "base64",
        "/v1/chat/completions",
        "Image.open",
        "enrichment.vision",
    ]
    imports = [
        line
        for line in code.splitlines()
        if line.strip().startswith(("import ", "from "))
        and any(name in line for name in FORBIDDEN_IMPORTS)
    ]
    assert imports == ["from PIL import Image"], imports

    innocent = tmp_path / "good_route.py"
    innocent.write_text(_INNOCENT_ROUTE, encoding="utf-8")
    clean = _code_only(innocent.read_text(encoding="utf-8"))
    assert [needle for needle in FORBIDDEN if needle in clean] == []
    assert "base64" not in clean


def test_the_dispatch_route_is_covered_by_this_fence() -> None:
    """The route module ADR 0021 added is actually in the swept set.

    Named explicitly because the fence's own failure mode is scanning a set that does
    not contain the file the fence was written for — which is what would happen if the
    dispatch routes ever moved into a subpackage. `_route_modules` globs one level.
    """
    assert "dispatch.py" in {path.name for path in _route_modules()}
