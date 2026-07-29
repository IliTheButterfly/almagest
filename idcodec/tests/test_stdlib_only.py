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
from pathlib import Path

import pytest

import idcodec

PACKAGE_ROOT = Path(idcodec.__file__).parent

MODULES = sorted(path.stem for path in PACKAGE_ROOT.glob("*.py"))

#: Distributions this package is allowed to import. Empty, and `pyproject.toml`
#: says the same thing in the form the installer reads. Adding a name here is a
#: deliberate act with a review attached, which is the point.
ALLOWED_THIRD_PARTY: frozenset[str] = frozenset()


def test_the_package_has_more_than_one_module() -> None:
    """A sanity check on the glob above: if `MODULES` ever came back empty or
    tiny, every other test in this file would pass by examining nothing."""
    assert set(MODULES) >= {"__init__", "shortid", "tagpayload"}


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


@pytest.mark.parametrize("module", MODULES)
def test_no_module_imports_anything_but_the_standard_library(module: str) -> None:
    """Static: walks the AST, so a deferred import inside a function is caught
    too. `sys.stdlib_module_names` is the interpreter's own answer to "is this
    standard library", which is why it is used instead of a hand-kept list."""
    imported = _top_level_imports(ast.parse((PACKAGE_ROOT / f"{module}.py").read_text()))
    foreign = imported - sys.stdlib_module_names - {"idcodec"} - ALLOWED_THIRD_PARTY
    assert not foreign, f"idcodec.{module} imports non-stdlib {sorted(foreign)}"


def test_importing_the_whole_package_loads_nothing_outside_the_standard_library() -> None:
    """Dynamic, and in a **fresh subprocess** on purpose.

    This test process has pytest loaded and — under `make check` in a shared
    venv — potentially anything else. Asking the current interpreter what it has
    imported would answer for the test runner, not for the package. A child
    started with `-I` (isolated: no site-packages-modifying env, no cwd on the
    path beyond what we set) that imports only `idcodec` gives the answer a
    Raspberry Pi would give.
    """
    targets = ["idcodec" if name == "__init__" else f"idcodec.{name}" for name in MODULES]
    program = f"""
import importlib, json, sys
before = set(sys.modules)
for name in {targets!r}:
    importlib.import_module(name)
print(json.dumps(sorted(set(sys.modules) - before)))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", program],
        capture_output=True,
        text=True,
        check=True,
        cwd=PACKAGE_ROOT.parent,
    )
    loaded = {name.split(".")[0] for name in json.loads(completed.stdout)}
    foreign = loaded - sys.stdlib_module_names - {"idcodec"} - ALLOWED_THIRD_PARTY
    assert not foreign, f"importing idcodec pulled in non-stdlib {sorted(foreign)}"


def test_the_declared_dependency_list_is_empty() -> None:
    """The installer's view of the same promise. A dependency declared but not
    yet imported is still a wheel on the Pi, and it is how the next one arrives.
    """
    text = (PACKAGE_ROOT.parent / "pyproject.toml").read_text()
    assert "\ndependencies = []\n" in text, "idcodec must declare no dependencies"
