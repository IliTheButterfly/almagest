"""`/api/parameter-fields` — authoring the things you filter on.

A "field" here is a `parameter_template` row: one filterable attribute, either
numeric with a physical quantity ("capacitance", farads) or a list with a fixed
set of options ("dielectric": C0G, X7R, ...). Until now every one of them came out
of a migration or the demo seed script, so "capacitors also have an ESR" was a
code change.

**Why not `POST /api/parameter-templates`.** That path is already taken by the
*facet reader* (`parameter_facets`) — a read that has to be a POST because it
carries the whole current filter set in its body. FastAPI cannot route two POSTs
to one path, and moving the reader would break the operation id every generated
client already calls. So the write door gets its own prefix, named after what the
user is actually doing: adding a field. `GET /api/parameter-fields` is the plain
list, including the inheritance the facet reader now also honours.

Three properties of this module are load-bearing rather than stylistic, and all
three are explained where they are enforced, in `app.services.parameter_fields`:

* `base_unit` is validated **at authoring time** against the same parser the
  search path uses, because a field with an unparseable unit is creatable,
  visible in the filter panel, and matches nothing;
* `substitution_direction` is **required**, with no default. It is what makes a
  50 V part an acceptable stand-in for a 25 V one and not the reverse; defaulting
  a voltage rating to `exact` would quietly produce a substitution search that
  is wrong by construction;
* a list field is authored **with its options in one request**, because an enum
  template with no choices matches nothing while looking like a working filter.

`name` is globally UNIQUE, and that is right — one real-world concept is one
field, so "voltage rating" means one thing whichever category asks for it. A
collision therefore has to be *explained*, and `on_name_conflict` is how the
client says which of the three defensible answers it wants.
"""

from __future__ import annotations

from enum import StrEnum

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api import idempotency
from app.api.limits import RowId, SortOrder
from app.api.schemas import ReplayableResponse
from app.db.session import get_db
from app.models.enums import SubstitutionDirection, ValueType
from app.models.parameter import ParameterChoice, ParameterTemplate
from app.services import parameter_fields as fields
from app.services.parameters import choice_aliases
from app.services.search.value_parser import (
    canonical_quantity,
    quantity_symbol,
    supported_quantities,
)

router = APIRouter(prefix="/api/parameter-fields", tags=["parameter-fields"])


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


class NameConflictPolicy(StrEnum):
    """What to do when the requested `name` is already a field.

    Not a silent choice, because all three answers are reasonable and they differ
    in what the user ends up with:

    * `fail` — refuse with 409 and hand back the existing field, so the UI can ask
      "did you mean this one?". The default, since the honest answer to a
      collision is usually "the field you want already exists".
    * `reuse` — adopt the existing field instead of creating one, provided it is
      *compatible* (same value type, same quantity). Any option the request names
      that the existing list field lacks is added, which is additive and safe.
      This is the right answer for "capacitors and inductors both have a voltage
      rating": one template, and substitution stays coherent because there is only
      one declared direction for the concept.
    * `namespace` — create a genuinely separate field named
      `<category>.<name>`, for when the collision is an accident of vocabulary
      rather than the same concept. Requires `applies_to_category`, because
      without one there is no namespace to put it in.
    """

    FAIL = "fail"
    REUSE = "reuse"
    NAMESPACE = "namespace"


class ChoiceIn(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=255)
    #: Alternative spellings that resolve to this option. This is what makes dual
    #: notation work — `0603` and `1608` are one package under two conventions, so
    #: the user is never asked which one a source used.
    aliases: list[str] = Field(default_factory=list)
    sort_order: SortOrder = 0


class ParameterFieldCreate(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "The filter key, globally unique — this is the string a search request "
            "and a shared search URL name. See `on_name_conflict` for what happens "
            "when it is taken."
        ),
    )
    display_name: str = Field(min_length=1, max_length=255)
    value_type: ValueType
    base_unit: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Required for a numeric field, forbidden otherwise. The name of a "
            "physical quantity, not a unit symbol: 'ohm', not 'ohms' and not 'Ω'. "
            "GET /api/parameter-fields/base-units lists every accepted value. It is "
            "what makes a bare '1M' read as 1 MΩ under resistance and be refused "
            "under capacitance."
        ),
    )
    substitution_direction: SubstitutionDirection = Field(
        description=(
            "Required, and not cosmetic: it is what a substitution search means. "
            "'higher_ok' for a rating (a 50 V part satisfies a 25 V requirement), "
            "'lower_ok' for a tolerance, 'range_overlap' for a value like "
            "capacitance, 'exact' for a package. There is no default — defaulting a "
            "voltage rating to 'exact' would silently make substitution wrong."
        )
    )
    applies_to_category: str | None = Field(
        default=None,
        max_length=255,
        description=(
            "Category slug. The field is then offered on that category **and every "
            "descendant of it**. Leave it null for a field every part has, like a "
            "package, which is offered everywhere."
        ),
    )
    sort_order: SortOrder = 0
    plausible_min: float | None = None
    plausible_max: float | None = None
    choices: list[ChoiceIn] = Field(
        default_factory=list,
        description=(
            "The complete option list of a list field, authored in this one request. "
            "Required for value_type='enum', forbidden otherwise."
        ),
    )
    on_name_conflict: NameConflictPolicy = NameConflictPolicy.FAIL
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class ParameterFieldUpdate(BaseModel):
    """Every field of a definition that can change, with the three that cannot
    guarded rather than omitted — a client sending `value_type` deserves the
    reason, not a silently ignored key."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    value_type: ValueType | None = None
    base_unit: str | None = Field(default=None, max_length=64)
    substitution_direction: SubstitutionDirection | None = None
    #: Explicit null makes the field global again, which is a real edit.
    applies_to_category: str | None = Field(default=None, max_length=255)
    sort_order: SortOrder | None = None
    plausible_min: float | None = None
    plausible_max: float | None = None
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class ChoiceUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=255)
    sort_order: SortOrder | None = None
    #: The complete alias list, never a delta. Explicit null clears it.
    aliases: list[str] | None = None
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class ChoiceAdd(ChoiceIn):
    client_op_id: str | None = Field(default=None, max_length=36)
    device_id: str | None = Field(default=None, max_length=64)


class ParameterChoiceRead(BaseModel):
    id: int
    key: str
    label: str
    aliases: list[str]
    sort_order: int
    #: How many parts are filed under this option. What "delete" is refused with.
    use_count: int


class ParameterFieldRead(BaseModel):
    id: int
    name: str
    display_name: str
    value_type: str
    base_unit: str | None
    substitution_direction: str
    applies_to_category: str | None
    sort_order: int
    plausible_min: float | None
    plausible_max: float | None
    #: True when this field is offered here by inheritance — it was authored on an
    #: ancestor category, or on none at all — rather than on the category asked
    #: about. The editor has to be able to say "this comes from Passives", because
    #: editing it affects every sibling.
    inherited: bool = False
    #: Part of the shared library: its name, value type and quantity are frozen.
    #: Everything else about it is editable.
    is_seed: bool
    #: How many parts hold a value for this field. The number a refusal names.
    value_count: int
    choices: list[ParameterChoiceRead] = Field(default_factory=list)


class ParameterFieldCreated(ReplayableResponse):
    field: ParameterFieldRead
    #: True when `on_name_conflict='reuse'` adopted an existing field rather than
    #: creating one. Nothing was created; options named in the request that the
    #: existing field lacked were added to it.
    reused: bool = False


class ParameterFieldEdited(ReplayableResponse):
    field: ParameterFieldRead


class ParameterFieldDeleted(BaseModel):
    field_id: int
    name: str


class ChoiceDeleted(BaseModel):
    field_id: int
    choice_id: int
    key: str


class BaseUnitOption(BaseModel):
    """One pickable quantity for a numeric field.

    Served rather than hardcoded in the client for the same reason the parser owns
    the list: a quantity added to the library becomes authorable without a second
    edit, and the UI can offer a **select** instead of a free-text box that
    produces `µF` and `ohms`.
    """

    name: str
    symbol: str


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _choice_read(db: Session, choice: ParameterChoice) -> ParameterChoiceRead:
    return ParameterChoiceRead(
        id=choice.id,
        key=choice.key,
        label=choice.label,
        aliases=list(choice_aliases(choice)),
        sort_order=choice.sort_order,
        use_count=fields.choice_use_count(db, choice),
    )


def _read(
    db: Session, template: ParameterTemplate, *, inherited: bool = False
) -> ParameterFieldRead:
    return ParameterFieldRead(
        id=template.id,
        name=template.name,
        display_name=template.display_name,
        value_type=template.value_type,
        base_unit=template.base_unit,
        substitution_direction=template.substitution_direction,
        applies_to_category=template.applies_to_category,
        sort_order=template.sort_order,
        plausible_min=template.plausible_min,
        plausible_max=template.plausible_max,
        inherited=inherited,
        is_seed=template.is_seed,
        value_count=fields.value_count(db, template),
        choices=[_choice_read(db, choice) for choice in fields.choices_of(db, template)],
    )


def _authoring_error(error: fields.AuthoringError) -> HTTPException:
    """One mapping for every refusal in the service.

    422 for "this definition cannot be written", 409 for "something already there
    is in the way", 404 for a slug that names nothing. The reason code is what the
    UI routes on; the message is what it shows.
    """
    conflicts = {
        "duplicate_name",
        "duplicate_choice_key",
        "seed_immutable",
        "value_type_in_use",
        "base_unit_in_use",
        "field_in_use",
        "choice_in_use",
        "incompatible_existing_field",
    }
    if error.reason == "unknown_category":
        code = status.HTTP_404_NOT_FOUND
    elif error.reason in conflicts:
        code = status.HTTP_409_CONFLICT
    else:
        code = status.HTTP_422_UNPROCESSABLE_CONTENT
    return HTTPException(code, detail={"reason": error.reason, "message": str(error)})


def _require_field(db: Session, field_id: RowId) -> ParameterTemplate:
    template = db.get(ParameterTemplate, field_id)
    if template is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "unknown_parameter_field",
                "message": f"no parameter field with id {field_id}",
            },
        )
    return template


def _require_choice(db: Session, template: ParameterTemplate, choice_id: RowId) -> ParameterChoice:
    choice = db.get(ParameterChoice, choice_id)
    if choice is None or choice.template_id != template.id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "unknown_choice",
                "message": f"no option with id {choice_id} on field {template.name!r}",
            },
        )
    return choice


def _specs(choices: list[ChoiceIn]) -> list[fields.ChoiceSpec]:
    return [
        fields.ChoiceSpec(
            key=choice.key, label=choice.label, aliases=choice.aliases, sort_order=choice.sort_order
        )
        for choice in choices
    ]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@router.get("/base-units", response_model=list[BaseUnitOption])
def list_base_units() -> list[BaseUnitOption]:
    """Every quantity a numeric field's `base_unit` may name.

    Declared above `/{field_id}` so the literal path wins the match — a
    `RowId`-typed path parameter would reject 'base-units' as a 422 rather than
    falling through.
    """
    return [
        BaseUnitOption(name=name, symbol=quantity_symbol(name) or name)
        for name in supported_quantities()
    ]


@router.get("", response_model=list[ParameterFieldRead])
def list_parameter_fields(
    db: Session = Depends(get_db),
    category: str | None = Query(
        default=None,
        description=(
            "Category slug. Returns the fields authored on it, **the fields authored "
            "on any ancestor of it**, and the global ones. Inheritance is the point: "
            "a field added to 'Capacitors' has to be offered under "
            "'Capacitors > Ceramic', which is the node parts are actually filed under."
        ),
    ),
) -> list[ParameterFieldRead]:
    try:
        templates = fields.templates_for_category(db, category)
    except fields.AuthoringError as error:
        raise _authoring_error(error) from error

    own = category
    return [
        _read(
            db,
            template,
            inherited=own is not None and template.applies_to_category != own,
        )
        for template in templates
    ]


@router.get("/{field_id}", response_model=ParameterFieldRead)
def read_parameter_field(field_id: RowId, db: Session = Depends(get_db)) -> ParameterFieldRead:
    return _read(db, _require_field(db, field_id))


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


@router.post("", response_model=ParameterFieldCreated, status_code=status.HTTP_201_CREATED)
def create_parameter_field(
    request: ParameterFieldCreate, db: Session = Depends(get_db)
) -> ParameterFieldCreated:
    """Author one filterable field, options and all.

    The name collision is resolved *before the insert and inside `work`*, and both
    halves of that matter:

    * **before the insert**, never caught after, because `idempotency.run` rolls
      back on `IntegrityError` to absorb a duplicate *client_op_id*, so a
      unique-name violation reaching that handler conflates two unrelated
      conditions and returns a bare 500 — the trap `create_container_type`
      documents;
    * **inside `work`**, because a *retry* of a request that already succeeded has
      to replay, not collide. Checked ahead of `idempotency.run` the second POST of
      one flaky-wifi submission would 409 on the field its own first attempt
      created, which is exactly the failure the idempotency guard exists to
      prevent. `clone_container_type` puts its duplicate-slug check inside `work`
      for the same reason.
    """

    def work() -> ParameterFieldCreated:
        existing = fields.find_by_name(db, request.name)
        name = request.name
        reuse: ParameterTemplate | None = None

        if existing is not None:
            if request.on_name_conflict is NameConflictPolicy.FAIL:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail={
                        "reason": "duplicate_name",
                        "message": (
                            f"a field named {request.name!r} already exists "
                            f"({existing.display_name!r}). One real-world concept is one "
                            "field, so this is usually the field you want: reuse it "
                            "(on_name_conflict='reuse'), or give yours a namespaced name "
                            "(on_name_conflict='namespace')."
                        ),
                        "existing": _read(db, existing).model_dump(),
                    },
                )
            if request.on_name_conflict is NameConflictPolicy.NAMESPACE:
                if request.applies_to_category is None:
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail={
                            "reason": "namespace_needs_category",
                            "message": (
                                "on_name_conflict='namespace' names the new field "
                                "'<category>.<name>', so it needs applies_to_category."
                            ),
                        },
                    )
                name = f"{request.applies_to_category}.{request.name}"
                if fields.find_by_name(db, name) is not None:
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        detail={
                            "reason": "duplicate_name",
                            "message": f"a field named {name!r} already exists too",
                        },
                    )
            else:
                reuse = _compatible_for_reuse(existing, request)

        if reuse is not None:
            # Additive only: an option the caller named that the shared field
            # lacks is genuinely new information, and adding it cannot invalidate
            # a stored value the way a retype or a unit change would.
            for spec in _specs(request.choices):
                if not _has_choice(db, reuse, spec.key):
                    fields.add_choice(
                        db,
                        reuse,
                        key=spec.key,
                        label=spec.label,
                        aliases=spec.aliases,
                        sort_order=spec.sort_order,
                    )
            return ParameterFieldCreated(field=_read(db, reuse), reused=True)

        template = fields.create_template(
            db,
            name=name,
            display_name=request.display_name,
            value_type=request.value_type,
            substitution_direction=request.substitution_direction,
            base_unit=request.base_unit,
            applies_to_category=request.applies_to_category,
            sort_order=request.sort_order,
            plausible_min=request.plausible_min,
            plausible_max=request.plausible_max,
            choices=_specs(request.choices),
        )
        return ParameterFieldCreated(field=_read(db, template))

    try:
        return idempotency.run(
            db,
            client_op_id=request.client_op_id,
            device_id=request.device_id,
            endpoint="POST /api/parameter-fields",
            payload=request,
            response_model=ParameterFieldCreated,
            work=work,
        )
    except fields.AuthoringError as error:
        db.rollback()
        raise _authoring_error(error) from error


def _has_choice(db: Session, template: ParameterTemplate, key: str) -> bool:
    folded = key.strip().casefold()
    return any(choice.key.casefold() == folded for choice in fields.choices_of(db, template))


def _compatible_for_reuse(
    existing: ParameterTemplate, request: ParameterFieldCreate
) -> ParameterTemplate:
    """Adopt the existing field, or explain why it is not the same thing.

    Compatibility is value type plus quantity, and nothing else. Those two decide
    which columns a value occupies and what its numbers *mean*, so a mismatch is
    two different concepts wearing one name — reusing across it would file
    microfarads and millihenries in the same field.
    """
    if ValueType(existing.value_type) != request.value_type:
        raise _authoring_error(
            fields.AuthoringError(
                f"{existing.name!r} already exists as a {existing.value_type} field, and "
                f"you asked for {request.value_type}. Those are different concepts "
                "sharing a name — give yours a different one, or "
                "on_name_conflict='namespace'.",
                reason="incompatible_existing_field",
            )
        )
    if request.value_type == ValueType.NUMERIC:
        wanted = (
            canonical_quantity(request.base_unit) if request.base_unit else None
        ) or request.base_unit
        if wanted != existing.base_unit:
            raise _authoring_error(
                fields.AuthoringError(
                    f"{existing.name!r} already exists measured in {existing.base_unit}, "
                    f"and you asked for {wanted}. Reusing it would file two different "
                    "quantities in one field, where every stored bound would then mean "
                    "whichever unit the row was written under.",
                    reason="incompatible_existing_field",
                )
            )
    return existing


@router.patch("/{field_id}", response_model=ParameterFieldEdited)
def update_parameter_field(
    field_id: RowId, request: ParameterFieldUpdate, db: Session = Depends(get_db)
) -> ParameterFieldEdited:
    """Edit a definition.

    What is refused, and why, is in `app.services.parameter_fields`: `value_type`
    and `base_unit` once any part holds a value (a data migration, not an edit),
    and all three identity fields on a shared-library field.
    """
    template = _require_field(db, field_id)
    assigned = set(request.model_fields_set)

    def work() -> ParameterFieldEdited:
        if "value_type" in assigned and request.value_type is not None:
            fields.retype_template(db, template, request.value_type)
        if "base_unit" in assigned:
            fields.set_base_unit(db, template, request.base_unit)
        if "name" in assigned and request.name is not None:
            fields.rename_template(db, template, request.name)
        if "display_name" in assigned and request.display_name is not None:
            template.display_name = request.display_name
        if "substitution_direction" in assigned and request.substitution_direction is not None:
            template.substitution_direction = request.substitution_direction
        if "applies_to_category" in assigned:
            fields.set_applies_to_category(db, template, request.applies_to_category)
        if "sort_order" in assigned and request.sort_order is not None:
            template.sort_order = request.sort_order
        if "plausible_min" in assigned or "plausible_max" in assigned:
            low = request.plausible_min if "plausible_min" in assigned else template.plausible_min
            high = request.plausible_max if "plausible_max" in assigned else template.plausible_max
            fields.set_plausibility(template, low, high)
        db.flush()
        return ParameterFieldEdited(field=_read(db, template))

    try:
        return idempotency.run(
            db,
            client_op_id=request.client_op_id,
            device_id=request.device_id,
            endpoint="PATCH /api/parameter-fields/{id}",
            payload=request,
            response_model=ParameterFieldEdited,
            work=work,
        )
    except fields.AuthoringError as error:
        db.rollback()
        raise _authoring_error(error) from error


@router.delete("/{field_id}", response_model=ParameterFieldDeleted)
def delete_parameter_field(field_id: RowId, db: Session = Depends(get_db)) -> ParameterFieldDeleted:
    """Remove a field. Refused while any part holds a value for it.

    `parameter_value.template_id` is `ON DELETE CASCADE`, so without the guard this
    would silently delete every value of the field along with it.
    """
    template = _require_field(db, field_id)
    name = template.name
    try:
        fields.delete_template(db, template)
    except fields.AuthoringError as error:
        raise _authoring_error(error) from error
    db.commit()
    return ParameterFieldDeleted(field_id=field_id, name=name)


@router.post(
    "/{field_id}/choices",
    response_model=ParameterFieldEdited,
    status_code=status.HTTP_201_CREATED,
)
def add_parameter_choice(
    field_id: RowId, request: ChoiceAdd, db: Session = Depends(get_db)
) -> ParameterFieldEdited:
    """Add one option to a list field. Additive, so allowed on a shared field too."""
    template = _require_field(db, field_id)

    def work() -> ParameterFieldEdited:
        fields.add_choice(
            db,
            template,
            key=request.key,
            label=request.label,
            aliases=request.aliases,
            sort_order=request.sort_order,
        )
        return ParameterFieldEdited(field=_read(db, template))

    try:
        return idempotency.run(
            db,
            client_op_id=request.client_op_id,
            device_id=request.device_id,
            endpoint="POST /api/parameter-fields/{id}/choices",
            payload=request,
            response_model=ParameterFieldEdited,
            work=work,
        )
    except fields.AuthoringError as error:
        db.rollback()
        raise _authoring_error(error) from error


@router.patch("/{field_id}/choices/{choice_id}", response_model=ParameterFieldEdited)
def update_parameter_choice(
    field_id: RowId, choice_id: RowId, request: ChoiceUpdate, db: Session = Depends(get_db)
) -> ParameterFieldEdited:
    """Relabel, reorder, or re-alias one option.

    **`key` is not editable.** It is what `parameter_value.raw_input` recorded and
    what a filter value names, so renaming it would rewrite the meaning of every
    row already filed under it. A wrong label is fixed here; a wrong key is a new
    option and a re-file.
    """
    template = _require_field(db, field_id)
    choice = _require_choice(db, template, choice_id)
    assigned = set(request.model_fields_set)

    def work() -> ParameterFieldEdited:
        if "label" in assigned and request.label is not None:
            choice.label = request.label
        if "sort_order" in assigned and request.sort_order is not None:
            choice.sort_order = request.sort_order
        if "aliases" in assigned:
            choice.aliases_json = fields.dump_aliases(request.aliases)
        db.flush()
        return ParameterFieldEdited(field=_read(db, template))

    return idempotency.run(
        db,
        client_op_id=request.client_op_id,
        device_id=request.device_id,
        endpoint="PATCH /api/parameter-fields/{id}/choices/{choice_id}",
        payload=request,
        response_model=ParameterFieldEdited,
        work=work,
    )


@router.delete("/{field_id}/choices/{choice_id}", response_model=ChoiceDeleted)
def delete_parameter_choice(
    field_id: RowId, choice_id: RowId, db: Session = Depends(get_db)
) -> ChoiceDeleted:
    """Remove an option. Refused while parts are filed under it, naming how many.

    `parameter_value.choice_id` is `ON DELETE RESTRICT`, so the database already
    refuses — but it refuses as an `IntegrityError`, which reaches the client as a
    500 with no number in it.
    """
    template = _require_field(db, field_id)
    choice = _require_choice(db, template, choice_id)
    key = choice.key
    try:
        fields.delete_choice(db, choice)
    except fields.AuthoringError as error:
        raise _authoring_error(error) from error
    db.commit()
    return ChoiceDeleted(field_id=field_id, choice_id=choice_id, key=key)
