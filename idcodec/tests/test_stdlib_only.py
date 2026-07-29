"""The guard that makes this package worth having.

`idcodec` exists so the station agent can share the identity rules with the API
without installing fastapi, sqlalchemy, alembic and pint on a Raspberry Pi. That
property is invisible in every other test — `validate("4K7T92M8")` passes
identically whether or not somebody added a convenient
`from app.models.enums import EntityType` at the top of `shortid.py`. So it is
asserted directly, and it is asserted two ways, because each catches what the
other misses.

The subprocess check is the honest one: it observes what the interpreter actually
loaded. The AST check catches a lazily-imported module that a plain import would
never reach — a function-body `import sqlalchemy` is exactly the shape a "just
this once" dependency arrives in.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import idcodec

PACKAGE_ROOT = Path(idcodec.__file__).parent

#: `rglob`, not `glob`: a future `idcodec/<subpkg>/mod.py` has to be covered too,
#: and a non-recursive glob would have left it checked by *neither* test in this
#: file — silently, since both iterate this one list.
MODULE_PATHS = sorted(PACKAGE_ROOT.rglob("*.py"))


def _import_name(path: Path) -> str:
    """The dotted name `importlib` would be given for a file in the package."""
    parts = ("idcodec", *path.relative_to(PACKAGE_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


IMPORT_NAMES = sorted({_import_name(path) for path in MODULE_PATHS})

#: Distributions this package is allowed to import. Empty, and `pyproject.toml`
#: says the same thing in the form the installer reads. Adding a name here is a
#: deliberate act with a review attached, which is the point.
ALLOWED_THIRD_PARTY: frozenset[str] = frozenset()


def test_the_package_has_more_than_one_module() -> None:
    """A sanity check on the glob above: if `MODULE_PATHS` ever came back empty or
    tiny, every other test in this file would pass by examining nothing."""
    assert set(IMPORT_NAMES) >= {"idcodec", "idcodec.shortid", "idcodec.tagpayload"}


def _top_level_imports(tree: ast.AST) -> set[str]:
    """Every module name imported anywhere in the file, nested imports included."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        # `from . import x` has no module; a relative import cannot leave the
        # package, so it is first-party by construction.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            names.add(node.module.split(".")[0])
    return names


@pytest.mark.parametrize("path", MODULE_PATHS, ids=_import_name)
def test_no_module_imports_anything_but_the_standard_library(path: Path) -> None:
    """Static: walks the AST, so a deferred import inside a function is caught
    too. `sys.stdlib_module_names` is the interpreter's own answer to "is this
    standard library", which is why it is used instead of a hand-kept list."""
    imported = _top_level_imports(ast.parse(path.read_text()))
    foreign = imported - sys.stdlib_module_names - {"idcodec"} - ALLOWED_THIRD_PARTY
    assert not foreign, f"{_import_name(path)} imports non-stdlib {sorted(foreign)}"


def test_importing_the_whole_package_loads_nothing_outside_the_standard_library() -> None:
    """Dynamic, and in a **fresh subprocess** on purpose.

    This test process has pytest loaded and — under `make check` in a shared
    venv — potentially anything else. Asking the current interpreter what it has
    imported would answer for the test runner, not for the package. A child
    started with `-I` is isolated: `PYTHONPATH` and friends are ignored and the
    script directory / cwd is *not* placed on `sys.path`, so the only reason it
    can see `idcodec` at all is that the venv running this test has the package
    installed. That is precisely the Pi's situation, which is why the answer it
    gives is the one worth asserting.
    """
    program = f"""
import importlib, json, sys
before = set(sys.modules)
for name in {IMPORT_NAMES!r}:
    importlib.import_module(name)
print(json.dumps(sorted(set(sys.modules) - before)))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", program],
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = {name.split(".")[0] for name in json.loads(completed.stdout)}
    foreign = loaded - sys.stdlib_module_names - {"idcodec"} - ALLOWED_THIRD_PARTY
    assert not foreign, f"importing idcodec pulled in non-stdlib {sorted(foreign)}"


def test_the_declared_dependency_list_is_empty() -> None:
    """The installer's view of the same promise. A dependency declared but not
    yet imported is still a wheel on the Pi, and it is how the next one arrives.

    Parsed with `tomllib` rather than grepped for `dependencies = []`: a substring
    match would not notice an `[project.optional-dependencies]` group with real
    requirements in it, and `make idcodec-sync` passes `--all-extras`, so an extra
    is a wheel on the Pi exactly like a hard dependency is. `[dependency-groups]
    dev` is deliberately *not* checked — pytest and mypy are never installed by a
    consumer of this distribution.
    """
    manifest = tomllib.loads((PACKAGE_ROOT.parent / "pyproject.toml").read_text())
    project = manifest["project"]
    assert project["dependencies"] == [], "idcodec must declare no dependencies"
    extras = {
        name: requirements
        for name, requirements in project.get("optional-dependencies", {}).items()
        if requirements
    }
    assert not extras, f"idcodec must declare no optional dependencies either: {extras}"
