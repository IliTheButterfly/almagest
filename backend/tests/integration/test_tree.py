"""Tree repository, exercised against both hierarchies.

The point of `TreeRepository` is that physical storage and logical taxonomy are
the *same* structure, so the parameterised tests run over both tables. A test
that only covered `locations` would not prove the shared implementation works.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models.catalog import PartCategory
from app.models.storage import Location
from app.services.tree import CycleError, TreeRepository

TREE_MODELS = [Location, PartCategory]


def _make(repo: TreeRepository, name: str, parent_id: int | None = None):  # type: ignore[type-arg]
    kwargs = {"name": name, "parent_id": parent_id}
    if repo.model is PartCategory:
        kwargs["slug"] = name.lower().replace(" ", "-")
    node = repo.model(**kwargs)
    repo.session.add(node)
    repo.session.flush()
    return node


def _cabinet(repo: TreeRepository):  # type: ignore[type-arg]
    """room -> cabinet -> drawer -> tray, plus a sibling cabinet."""
    room = _make(repo, "Room")
    cabinet = _make(repo, "Cabinet A", room.id)
    drawer = _make(repo, "Drawer 3", cabinet.id)
    tray = _make(repo, "Tray 1", drawer.id)
    other = _make(repo, "Cabinet B", room.id)
    repo.rebuild_paths()
    return room, cabinet, drawer, tray, other


@pytest.mark.parametrize("model", TREE_MODELS)
def test_paths_are_built_from_parent_ids(db: Session, model: type) -> None:
    repo = TreeRepository(db, model)
    room, cabinet, drawer, tray, _ = _cabinet(repo)

    assert room.id_path == f"/{room.id}/"
    assert cabinet.id_path == f"/{room.id}/{cabinet.id}/"
    assert tray.id_path == f"/{room.id}/{cabinet.id}/{drawer.id}/{tray.id}/"

    assert room.depth == 0
    assert tray.depth == 3

    assert tray.label_path == "Room / Cabinet A / Drawer 3 / Tray 1"


@pytest.mark.parametrize("model", TREE_MODELS)
def test_rebuild_is_idempotent(db: Session, model: type) -> None:
    """Running it twice must change nothing — the second call reports zero rows
    updated, which is also what makes it cheap to run defensively."""
    repo = TreeRepository(db, model)
    _cabinet(repo)

    first = {n.id: (n.depth, n.id_path, n.label_path) for n in repo.subtree_all()}
    changed = repo.rebuild_paths()
    second = {n.id: (n.depth, n.id_path, n.label_path) for n in repo.subtree_all()}

    assert changed == 0
    assert first == second


@pytest.mark.parametrize("model", TREE_MODELS)
def test_a_corrupted_cache_is_fully_recoverable(db: Session, model: type) -> None:
    """The property everything else leans on: the cache is derived, so a cache
    bug is a stale label and never data loss."""
    repo = TreeRepository(db, model)
    _cabinet(repo)
    expected = {n.id: (n.depth, n.id_path, n.label_path) for n in repo.subtree_all()}

    for node in repo.subtree_all():
        node.depth = 999
        node.id_path = "/nonsense/"
        node.label_path = "garbage"
    db.flush()

    repo.rebuild_paths()
    assert {n.id: (n.depth, n.id_path, n.label_path) for n in repo.subtree_all()} == expected


@pytest.mark.parametrize("model", TREE_MODELS)
def test_subtree_and_ancestors(db: Session, model: type) -> None:
    repo = TreeRepository(db, model)
    room, cabinet, drawer, tray, other = _cabinet(repo)

    assert {n.id for n in repo.subtree(cabinet)} == {cabinet.id, drawer.id, tray.id}
    assert {n.id for n in repo.subtree(cabinet, include_self=False)} == {drawer.id, tray.id}
    assert [n.id for n in repo.ancestors(tray)] == [room.id, cabinet.id, drawer.id]
    assert repo.ancestors(room) == []
    assert other.id not in {n.id for n in repo.subtree(cabinet)}


@pytest.mark.parametrize("model", TREE_MODELS)
def test_prefix_matching_does_not_confuse_id_1_with_id_12(db: Session, model: type) -> None:
    """The separators wrapping every id are load-bearing: without the trailing
    one, `/1/` would prefix-match `/12/` and a subtree query would silently
    return unrelated rows."""
    repo = TreeRepository(db, model)
    roots = [_make(repo, f"Root {i}") for i in range(14)]
    repo.rebuild_paths()

    first, twelfth = roots[0], roots[11]
    assert repo.subtree(first) == [first]
    assert twelfth.id not in {n.id for n in repo.subtree(first)}


@pytest.mark.parametrize("model", TREE_MODELS)
def test_move_reparents_and_refreshes_the_whole_subtree(db: Session, model: type) -> None:
    repo = TreeRepository(db, model)
    _, _, drawer, tray, other = _cabinet(repo)

    repo.move(drawer, other.id)

    db.refresh(drawer)
    db.refresh(tray)
    assert drawer.parent_id == other.id
    assert tray.label_path == "Room / Cabinet B / Drawer 3 / Tray 1"
    assert tray.id_path.startswith(other.id_path)


@pytest.mark.parametrize("model", TREE_MODELS)
def test_a_node_cannot_be_moved_under_itself(db: Session, model: type) -> None:
    repo = TreeRepository(db, model)
    _, cabinet, _, _, _ = _cabinet(repo)
    with pytest.raises(CycleError):
        repo.move(cabinet, cabinet.id)


@pytest.mark.parametrize("model", TREE_MODELS)
def test_a_node_cannot_be_moved_under_its_own_descendant(db: Session, model: type) -> None:
    repo = TreeRepository(db, model)
    _, cabinet, _, tray, _ = _cabinet(repo)
    with pytest.raises(CycleError):
        repo.move(cabinet, tray.id)


@pytest.mark.parametrize("model", TREE_MODELS)
def test_the_cycle_guard_does_not_trust_the_cache(db: Session, model: type) -> None:
    """Deliberately stronger than the `id_path LIKE` test the design sketches.

    A stale cache would let a cycle through, and a cycle makes the rebuild CTE
    recurse forever — unrecoverable without manual surgery. So the guard walks
    `parent_id`, which is authoritative.
    """
    repo = TreeRepository(db, model)
    _, cabinet, _, tray, _ = _cabinet(repo)

    # Corrupt exactly the thing the sketched guard would have consulted.
    for node in repo.subtree_all():
        node.id_path = "/0/"
    db.flush()

    with pytest.raises(CycleError):
        repo.move(cabinet, tray.id)


@pytest.mark.parametrize("model", TREE_MODELS)
def test_moving_to_root_is_allowed(db: Session, model: type) -> None:
    repo = TreeRepository(db, model)
    _, cabinet, _, tray, _ = _cabinet(repo)
    repo.move(cabinet, None)

    db.refresh(cabinet)
    db.refresh(tray)
    assert cabinet.parent_id is None
    assert cabinet.depth == 0
    assert tray.label_path == "Cabinet A / Drawer 3 / Tray 1"


def test_nearest_ancestor_value_resolves_inherited_esd(db: Session) -> None:
    """Marking a whole cabinet ESD-safe must be one edit, not one per drawer."""
    repo = TreeRepository(db, Location)
    _, cabinet, drawer, tray, _ = _cabinet(repo)

    assert repo.nearest_ancestor_value(tray, "esd_safe") is None

    cabinet.esd_safe = True
    db.flush()
    assert repo.nearest_ancestor_value(tray, "esd_safe") is True

    # The nearest non-NULL wins, so one drawer can opt out of an ESD-safe cabinet.
    drawer.esd_safe = False
    db.flush()
    assert repo.nearest_ancestor_value(tray, "esd_safe") is False

    # A node's own value beats every ancestor.
    tray.esd_safe = True
    db.flush()
    assert repo.nearest_ancestor_value(tray, "esd_safe") is True


def test_the_two_trees_are_independent(db: Session) -> None:
    """Logical taxonomy is not physical storage. Conflating them is what makes
    a storage tree unusable as a browse tree."""
    locations = TreeRepository(db, Location)
    categories = TreeRepository(db, PartCategory)
    _make(locations, "Shelf")
    _make(categories, "Passives")
    locations.rebuild_paths()
    categories.rebuild_paths()

    assert len(locations.subtree_all()) == 1
    assert len(categories.subtree_all()) == 1
