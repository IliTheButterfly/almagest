"""The other half of `idcodec/tests/test_stdlib_only.py`.

That test guards the *codec* against acquiring dependencies. This one guards the
**agent** against acquiring the API again, which is the regression that actually
happened once: `almagest-backend` sat in `[project.dependencies]` for two pure
functions and put fastapi, sqlalchemy, alembic and pint on a Raspberry Pi 4 that
imports none of them.

It is a manifest test rather than an import test on purpose. The Pi installs with
`uv sync --extra pi --no-dev`, so what lands there is decided by
`[project.dependencies]` and `[project.optional-dependencies]` alone — a wheel is
on the Pi whether or not any code imports it, and `import app` failing in a dev
venv would prove nothing because the dev venv is *supposed* to have the backend
(`tests/test_session_ledger.py` needs real models, real routes, real migrations).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "pyproject.toml"

#: What the Pi must never be asked to install. `almagest-backend` is the whole
#: point; the rest are the wheels it dragged along, listed by name so a
#: *transitive* reappearance through some other dependency fails too.
FORBIDDEN = frozenset({"almagest-backend", "fastapi", "sqlalchemy", "alembic", "pint"})


def _requirement_names(requirements: list[str]) -> set[str]:
    """The distribution name from each PEP 508 requirement string, lower-cased.

    Deliberately crude — split at the first character that can begin a version
    specifier, extra marker or environment marker. A real parser would be a
    dependency, and this file's whole subject is not adding those.
    """
    names: set[str] = set()
    for requirement in requirements:
        name = requirement.strip()
        for separator in ("[", ";", "=", "<", ">", "!", "~", " ", "@"):
            name = name.split(separator, 1)[0]
        names.add(name.strip().lower().replace("_", "-"))
    return names


def test_the_agent_declares_no_runtime_dependency_on_the_api() -> None:
    project = tomllib.loads(MANIFEST.read_text())["project"]
    installed_on_the_pi = _requirement_names(project["dependencies"])
    for extra, requirements in project.get("optional-dependencies", {}).items():
        assert extra  # a nameless extra would silently escape the loop below
        installed_on_the_pi |= _requirement_names(requirements)

    offenders = sorted(installed_on_the_pi & FORBIDDEN)
    assert not offenders, (
        f"{offenders} would be installed on the Pi. The identity rules live in "
        "`almagest-idcodec`, which declares nothing; anything needing the API "
        "belongs in `[dependency-groups] dev`, which `uv sync --no-dev` skips."
    )


def test_the_codec_is_a_runtime_dependency() -> None:
    """The converse: the shared rules must be present, not vendored back in. If
    this fails because somebody copied `normalize_tag_uid` into the agent, the
    two sides can fold a UID differently and a provisioning walk lies."""
    project = tomllib.loads(MANIFEST.read_text())["project"]
    assert "almagest-idcodec" in _requirement_names(project["dependencies"])
