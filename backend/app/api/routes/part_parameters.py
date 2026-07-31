"""A part's own field values — `/api/parts/{part_id}/parameters`.

**The door that was missing.** Categories could be authored, fields could be hung
off them, units could be invented, and a list field could be told to hold several
options — and none of it could be filled in by hand. The only way a
`parameter_value` ever appeared was a *source* proposing one (an MPN decoder, a
datasheet extraction, a BOM import) and a human accepting or correcting it in the
review queue. For a part nobody's decoder recognised there was no way at all, which
made every field authored this week decorative.

Three design points, each one a decision rather than a default:

* **Writes go through `app.services.parameters`, with `MANUAL` provenance.** That
  is the funnel that guarantees `value_min`/`value_max` on a numeric value, which
  is what makes it visible to a range query at all; a route writing the ORM
  directly would be the one way to produce a row that stores fine and matches
  nothing. `MANUAL` is the top of the disagreement priority, so a value typed here
  outranks anything a source later proposes.
* **Not through `parameter_value_candidate`.** The review queue's `correct` records
  a manual candidate because it is *overriding a source's reading* and the evidence
  of what that source said has to survive. Typing a value onto a part with no
  candidate overrides nothing — there is no evidence to preserve, and routing it
  through a proposal table would invent a review step for a decision already made.
* **The fields offered are the category's, inherited ones included** — the same
  `templates_for_category` the facet panel is built from, so the two can never
  disagree about what applies to a filed part. The one place they differ is a part
  filed *nowhere*, and see `_offered` for why: for the facet panel "no category"
  means "no filter", while for a part it means "only the fields every part has".

There is deliberately no bulk write. One field per request keeps a refusal about
one value — `1M` under capacitance is a *good* error, and it belongs against the
box that caused it rather than as one line of a partial-success report.
"""

from __future__ import annotations

from elec_value_parser import ValueParseError
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.limits import RowId
from app.db.session import get_db
from app.models.catalog import Part, PartCategory
from app.models.enums import ValueType
from app.models.parameter import ParameterTemplate, ParameterValue
from app.services import parameter_fields as fields
from app.services import parameters

router = APIRouter(prefix="/api/parts/{part_id}/parameters", tags=["part-parameters"])


class ChoiceValueRead(BaseModel):
    id: int
    key: str
    label: str


class PartParameterRead(BaseModel):
    """One field this part could have a value for, and the value if it has one."""

    template_id: int
    #: The filter key, which is also what identifies this field in a write.
    name: str
    display_name: str
    value_type: str
    base_unit: str | None
    allow_multiple: bool
    #: True when the field comes from an ancestor category or from none at all, so
    #: the screen can group "specific to this kind of part" apart from "everything
    #: has one of these".
    inherited: bool
    sort_order: int

    #: Exactly what was entered, when there is a value — the lossless record.
    raw_input: str | None = None
    value_nominal: float | None = None
    value_min: float | None = None
    value_max: float | None = None
    display: str | None = None
    value_text: str | None = None
    value_bool: bool | None = None
    #: Every option held. One entry for a single-valued field, several for a
    #: multi-valued one, empty for anything that is not a list.
    choices: list[ChoiceValueRead] = Field(default_factory=list)
    provenance: str | None = None
    #: The options this field offers, so the editor can render a picker without a
    #: second request per field.
    options: list[ChoiceValueRead] = Field(default_factory=list)


class PartParametersResponse(BaseModel):
    part_id: int
    category: str | None
    #: Said explicitly rather than left to be inferred from an empty list: a part
    #: filed nowhere gets only the fields every part has, and "why are there so few
    #: fields here" has to have an answer on screen.
    filed: bool
    parameters: list[PartParameterRead]


class PartParameterWrite(BaseModel):
    """The value, in whichever shape the field's type takes.

    Separate optional members rather than one `Any`: a bool is not the string
    'true', and a list field takes a *set*. Sending the wrong one for the type is a
    422 that names the mismatch instead of a coercion nobody asked for.
    """

    value: str | None = Field(
        default=None,
        description=(
            "For a numeric field the shorthand to parse — '22uF', '20-30uF', '>=50V'. "
            "For a text field the text itself, stored verbatim."
        ),
    )
    checked: bool | None = Field(default=None, description="For a yes/no field.")
    choices: list[str] | None = Field(
        default=None,
        description=(
            "For a list field: the complete set of options, by key or by any alias. "
            "One entry unless the field allows several. The whole set, never a delta, "
            "so 'now only SMD' is sayable."
        ),
    )


class PartParameterWritten(BaseModel):
    parameter: PartParameterRead


class PartParameterCleared(BaseModel):
    template_id: int
    name: str
    #: False when there was nothing stored, so a doubled tap is honest rather than
    #: pretending to have removed something.
    removed: bool


def _require_part(db: Session, part_id: RowId) -> Part:
    part = db.get(Part, part_id)
    if part is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason": "unknown_part", "message": f"no part with id {part_id}"},
        )
    return part


def _category_slug(db: Session, part: Part) -> str | None:
    if part.category_id is None:
        return None
    category = db.get(PartCategory, part.category_id)
    return None if category is None else category.slug


def _offered(db: Session, slug: str | None) -> list[ParameterTemplate]:
    """The fields that apply to a part filed under `slug`.

    **Deliberately not `templates_for_category(db, None)` for an unfiled part.**
    That function's null branch means "no category filter applied" — the facet
    panel's "All parts", where showing every field in the system is exactly right.
    A *part* filed nowhere is a different statement: the only fields that reach it
    are the ones that name no category, the `package`-and-`mounting_type` ones every
    part has. Conflating the two would offer an unfiled part a capacitance box, and
    a value entered there would belong to a field no filter panel for that part ever
    shows.
    """
    if slug is not None:
        return fields.templates_for_category(db, slug)
    return [
        template
        for template in fields.templates_for_category(db, None)
        if template.applies_to_category is None
    ]


def _read(
    db: Session, template: ParameterTemplate, value: ParameterValue | None, *, inherited: bool
) -> PartParameterRead:
    options = [
        ChoiceValueRead(id=choice.id, key=choice.key, label=choice.label)
        for choice in fields.choices_of(db, template)
    ]
    held: list[ChoiceValueRead] = []
    if value is not None:
        held = [
            ChoiceValueRead(id=choice.id, key=choice.key, label=choice.label)
            for choice in parameters.choices_held(db, value)
        ]
    return PartParameterRead(
        template_id=template.id,
        name=template.name,
        display_name=template.display_name,
        value_type=template.value_type,
        base_unit=template.base_unit,
        allow_multiple=template.allow_multiple,
        inherited=inherited,
        sort_order=template.sort_order,
        raw_input=None if value is None else value.raw_input,
        value_nominal=None if value is None else value.value_nominal,
        value_min=None if value is None else value.value_min,
        value_max=None if value is None else value.value_max,
        display=None if value is None else _display(value),
        value_text=None if value is None else value.value_text,
        value_bool=None if value is None else value.value_bool,
        choices=held,
        provenance=None if value is None else value.provenance,
        options=options,
    )


def _display(value: ParameterValue) -> str | None:
    """The engineering-notation rendering, from the stored components.

    Assembled rather than recomputed from `value_nominal`: the components are
    stored precisely so '4700 Ω' prints as '4.7 kΩ' without a second opinion about
    where the prefix goes.
    """
    if value.display_mantissa is None:
        return None
    mantissa = f"{value.display_mantissa:g}"
    prefix = value.display_si_prefix or ""
    symbol = value.display_unit_symbol or ""
    return f"{mantissa} {prefix}{symbol}".strip()


def _require_template(db: Session, part: Part, name: str) -> tuple[ParameterTemplate, bool]:
    """The field by name, refused unless it actually applies to this part.

    Checked against `templates_for_category` rather than merely existing, so a value
    cannot be filed against a field this part's category does not offer — which
    would be a value no filter panel for this part ever shows.
    """
    slug = _category_slug(db, part)
    offered = _offered(db, slug)
    for template in offered:
        if template.name == name:
            return template, template.applies_to_category != slug
    raise HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={
            "reason": "field_not_offered",
            "message": (
                f"{name!r} is not a field offered on this part. A field reaches a part through "
                "the category it is filed under — file the part under a category that has this "
                "field, or author the field where the part already sits."
            ),
        },
    )


@router.get("", response_model=PartParametersResponse)
def list_part_parameters(part_id: RowId, db: Session = Depends(get_db)) -> PartParametersResponse:
    """Every field this part could have a value for, with the values it has.

    Fields with no value are returned too, and that is the point: this is an
    editor, and a field you cannot see is a field you will not fill in.
    """
    part = _require_part(db, part_id)
    slug = _category_slug(db, part)
    offered = _offered(db, slug)
    return PartParametersResponse(
        part_id=part.id,
        category=slug,
        filed=slug is not None,
        parameters=[
            _read(
                db,
                template,
                parameters.value_for(db, part, template),
                inherited=template.applies_to_category != slug,
            )
            for template in offered
        ],
    )


@router.put("/{name}", response_model=PartParameterWritten)
def set_part_parameter(
    part_id: RowId, name: str, request: PartParameterWrite, db: Session = Depends(get_db)
) -> PartParameterWritten:
    """Set this part's value for one field.

    Every refusal here is one the search path would otherwise hit later: an
    unparseable value, a one-sided limit that no range query can match, several
    options on a field that holds one. The reason codes are the parser's own, so the
    UI can put the message against the box that caused it.
    """
    part = _require_part(db, part_id)
    template, inherited = _require_template(db, part, name)
    value_type = ValueType(template.value_type)

    try:
        if value_type == ValueType.NUMERIC:
            if request.value is None:
                raise _mismatch(template, "a number, as text to parse — '22uF', '20-30uF'")
            row = parameters.set_numeric(db, part, template, request.value)
        elif value_type == ValueType.TEXT:
            if request.value is None:
                raise _mismatch(template, "the text to store")
            row = parameters.set_text(db, part, template, request.value)
        elif value_type == ValueType.BOOL:
            if request.checked is None:
                raise _mismatch(template, "'checked': true or false")
            row = parameters.set_bool(db, part, template, request.checked)
        else:
            if not request.choices:
                raise _mismatch(template, "'choices': the options this part has")
            row = parameters.set_choices(db, part, template, request.choices)
    except parameters.TooManyChoices as error:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"reason": "too_many_choices", "message": str(error)},
        ) from error
    except parameters.ChoiceNotFound as error:
        db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": "unknown_choice", "message": str(error)},
        ) from error
    except parameters.UnboundedValue as error:
        db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": "unbounded_value", "message": str(error)},
        ) from error
    except ValueParseError as error:
        db.rollback()
        # The parser's own reason — `implausible`, `unit_mismatch`, `syntax` — kept
        # verbatim, because the frontend already has better wording for each of them
        # than a generic 422 could carry.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "reason": error.reason,
                "message": str(error),
                "template": template.name,
            },
        ) from error

    written = _read(db, template, row, inherited=inherited)
    db.commit()
    return PartParameterWritten(parameter=written)


@router.delete("/{name}", response_model=PartParameterCleared)
def clear_part_parameter(
    part_id: RowId, name: str, db: Session = Depends(get_db)
) -> PartParameterCleared:
    """Remove this part's value for one field.

    The row is deleted rather than blanked: a `parameter_value` with every value
    column null is a part claiming an attribute it has no answer for, which is
    counted as populated and matches no query — indistinguishable from a bug.
    """
    part = _require_part(db, part_id)
    template, _ = _require_template(db, part, name)
    removed = parameters.clear_value(db, part, template)
    db.commit()
    return PartParameterCleared(template_id=template.id, name=template.name, removed=removed)


def _mismatch(template: ParameterTemplate, expected: str) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "reason": "wrong_value_shape",
            "message": (
                f"{template.name!r} is a {template.value_type} field, so it takes {expected}."
            ),
        },
    )
