"""Starter content: parameter templates, their choices, and sample parts.

Distinct from the reference rows in the initial migration. Those are
*structural* — `parts.part_kind_id` is NOT NULL, so a database without
`part_kinds` cannot hold a part at all. What is here is content a user could
reasonably delete or replace, so it lives in a script rather than a migration.

Idempotent: re-running adds nothing and overwrites nothing.

    python -m app.scripts.seed_demo
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.models.catalog import Part, PartCategory, PartKind
from app.models.enums import SubstitutionDirection, ValueType
from app.models.parameter import ParameterChoice, ParameterTemplate
from app.services import parameters
from app.services.scanning.codes import normalize_mpn
from app.services.tree import category_tree


@dataclass(frozen=True)
class _Choice:
    key: str
    label: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Template:
    name: str
    display_name: str
    value_type: ValueType
    substitution_direction: SubstitutionDirection
    base_unit: str | None = None
    choices: tuple[_Choice, ...] = ()
    plausible_min: float | None = None
    plausible_max: float | None = None
    sort_order: int = 0
    applies_to_category: str | None = None


# `substitution_direction` is the whole substitution engine. It is what makes a
# 50 V capacitor an acceptable stand-in for a 25 V one but not the reverse, and
# it is correct by construction rather than by a model's judgement.
TEMPLATES: tuple[_Template, ...] = (
    _Template(
        name="resistance",
        display_name="Resistance",
        value_type=ValueType.NUMERIC,
        base_unit="ohm",
        substitution_direction=SubstitutionDirection.RANGE_OVERLAP,
        sort_order=10,
        applies_to_category="resistor",
    ),
    _Template(
        name="capacitance",
        display_name="Capacitance",
        value_type=ValueType.NUMERIC,
        base_unit="farad",
        substitution_direction=SubstitutionDirection.RANGE_OVERLAP,
        sort_order=10,
        applies_to_category="capacitor",
    ),
    _Template(
        name="inductance",
        display_name="Inductance",
        value_type=ValueType.NUMERIC,
        base_unit="henry",
        substitution_direction=SubstitutionDirection.RANGE_OVERLAP,
        sort_order=10,
        applies_to_category="inductor",
    ),
    # A higher rating always satisfies a requirement for a lower one.
    _Template(
        name="voltage_rating",
        display_name="Voltage rating",
        value_type=ValueType.NUMERIC,
        base_unit="volt",
        substitution_direction=SubstitutionDirection.HIGHER_OK,
        sort_order=20,
    ),
    _Template(
        name="current_rating",
        display_name="Current rating",
        value_type=ValueType.NUMERIC,
        base_unit="ampere",
        substitution_direction=SubstitutionDirection.HIGHER_OK,
        sort_order=20,
    ),
    _Template(
        name="power_rating",
        display_name="Power rating",
        value_type=ValueType.NUMERIC,
        base_unit="watt",
        substitution_direction=SubstitutionDirection.HIGHER_OK,
        sort_order=20,
    ),
    _Template(
        name="mounting_type",
        display_name="Mounting",
        value_type=ValueType.ENUM,
        substitution_direction=SubstitutionDirection.EXACT,
        sort_order=30,
        choices=(
            _Choice("THT", "Through-hole", ("through-hole", "through hole", "thru-hole", "tht")),
            _Choice("SMD", "Surface mount", ("surface-mount", "surface mount", "smt", "smd")),
        ),
    ),
    # Dual-notation keys. `0603` and `1608` are the same package under the
    # imperial and metric conventions, so both spellings resolve to one row and
    # the user is never asked which convention a source used.
    _Template(
        name="package",
        display_name="Package",
        value_type=ValueType.ENUM,
        substitution_direction=SubstitutionDirection.EXACT,
        sort_order=40,
        choices=(
            _Choice("0201_0603", "0201 (imperial) / 0603 (metric)", ("0201", "0603m")),
            _Choice("0402_1005", "0402 (imperial) / 1005 (metric)", ("0402", "1005")),
            _Choice("0603_1608", "0603 (imperial) / 1608 (metric)", ("0603", "1608")),
            _Choice("0805_2012", "0805 (imperial) / 2012 (metric)", ("0805", "2012")),
            _Choice("1206_3216", "1206 (imperial) / 3216 (metric)", ("1206", "3216")),
            _Choice("SOT-23", "SOT-23", ("sot23",)),
            _Choice("SOIC-8", "SOIC-8", ("soic8", "so-8")),
            _Choice("DIP-8", "DIP-8", ("dip8", "pdip-8")),
            _Choice("TO-220", "TO-220", ("to220",)),
            _Choice("TO-92", "TO-92", ("to92",)),
            _Choice("DO-35", "DO-35", ("do35",)),
            _Choice("Axial", "Axial leaded"),
            _Choice("Radial", "Radial leaded"),
        ),
    ),
    _Template(
        name="capacitor_technology",
        display_name="Capacitor technology",
        value_type=ValueType.ENUM,
        substitution_direction=SubstitutionDirection.EXACT,
        sort_order=50,
        applies_to_category="capacitor",
        choices=(
            _Choice("ceramic", "Ceramic", ("mlcc",)),
            _Choice("electrolytic", "Aluminium electrolytic", ("alu", "electrolytic")),
            _Choice("tantalum", "Tantalum"),
            _Choice("film", "Film", ("polyester", "polypropylene")),
            _Choice("polymer", "Polymer"),
            _Choice("supercapacitor", "Supercapacitor", ("supercap",)),
        ),
    ),
    _Template(
        name="dielectric",
        display_name="Dielectric",
        value_type=ValueType.ENUM,
        substitution_direction=SubstitutionDirection.EXACT,
        sort_order=60,
        applies_to_category="capacitor",
        choices=(
            _Choice("C0G", "C0G / NP0", ("np0", "c0g")),
            _Choice("X7R", "X7R"),
            _Choice("X5R", "X5R"),
            _Choice("Y5V", "Y5V"),
        ),
    ),
)

CATEGORIES: tuple[tuple[str, str, str | None], ...] = (
    ("passive", "Passives", None),
    ("resistor", "Resistors", "passive"),
    ("capacitor", "Capacitors", "passive"),
    ("inductor", "Inductors", "passive"),
    ("semiconductor", "Semiconductors", None),
    ("diode", "Diodes", "semiconductor"),
    ("transistor", "Transistors", "semiconductor"),
    ("ic", "Integrated circuits", "semiconductor"),
)


@dataclass
class SeedReport:
    templates: int = 0
    choices: int = 0
    categories: int = 0
    parts: int = 0
    notes: list[str] = field(default_factory=list)


def seed_categories(session: Session) -> int:
    created = 0
    by_slug: dict[str, PartCategory] = {
        row.slug: row for row in session.execute(select(PartCategory)).scalars()
    }
    for slug, name, parent_slug in CATEGORIES:
        if slug in by_slug:
            continue
        parent = by_slug.get(parent_slug) if parent_slug else None
        category = PartCategory(slug=slug, name=name, parent_id=parent.id if parent else None)
        session.add(category)
        session.flush()
        by_slug[slug] = category
        created += 1
    if created:
        category_tree(session).rebuild_paths()
    return created


def seed_parameter_templates(session: Session) -> tuple[int, int]:
    """Returns (templates created, choices created)."""
    templates_created = 0
    choices_created = 0

    for spec in TEMPLATES:
        template = session.execute(
            select(ParameterTemplate).where(ParameterTemplate.name == spec.name)
        ).scalar_one_or_none()

        if template is None:
            template = ParameterTemplate(
                name=spec.name,
                display_name=spec.display_name,
                value_type=spec.value_type,
                base_unit=spec.base_unit,
                substitution_direction=spec.substitution_direction,
                sort_order=spec.sort_order,
                applies_to_category=spec.applies_to_category,
                plausible_min=spec.plausible_min,
                plausible_max=spec.plausible_max,
            )
            session.add(template)
            session.flush()
            templates_created += 1

        existing = {
            row.key
            for row in session.execute(
                select(ParameterChoice).where(ParameterChoice.template_id == template.id)
            ).scalars()
        }
        for order, choice in enumerate(spec.choices):
            if choice.key in existing:
                continue
            session.add(
                ParameterChoice(
                    template_id=template.id,
                    key=choice.key,
                    label=choice.label,
                    aliases_json=json.dumps(list(choice.aliases)) if choice.aliases else None,
                    sort_order=order * 10,
                )
            )
            choices_created += 1
    session.flush()
    return templates_created, choices_created


#: Three capacitors that differ in exactly one facet each. This is the worked
#: example from the design doc: a search for "through-hole 20-30 uF ceramic"
#: must return the first and only the first.
SAMPLE_PARTS: tuple[dict[str, str], ...] = (
    {
        "name": "22uF 25V ceramic, through-hole",
        "mpn": "DEMO-CAP-THT-22U",
        "category": "capacitor",
        "capacitance": "22uF",
        "voltage_rating": "25V",
        "mounting_type": "THT",
        "capacitor_technology": "ceramic",
    },
    {
        "name": "22uF 16V ceramic, 0805",
        "mpn": "DEMO-CAP-SMD-22U",
        "category": "capacitor",
        "capacitance": "22uF",
        "voltage_rating": "16V",
        "mounting_type": "SMD",
        "capacitor_technology": "ceramic",
        "package": "0805",
    },
    {
        "name": "22uF 50V electrolytic, radial",
        "mpn": "DEMO-CAP-THT-22U-ELEC",
        "category": "capacitor",
        "capacitance": "22uF",
        "voltage_rating": "50V",
        "mounting_type": "THT",
        "capacitor_technology": "electrolytic",
    },
    {
        "name": "4k7 0.25W resistor, axial",
        "mpn": "DEMO-RES-4K7",
        "category": "resistor",
        "resistance": "4k7",
        "power_rating": "0.25W",
        "mounting_type": "THT",
    },
    {
        "name": "10k 1% 0603 resistor",
        "mpn": "DEMO-RES-10K",
        "category": "resistor",
        "resistance": "10k ±1%",
        "power_rating": "0.1W",
        "mounting_type": "SMD",
        "package": "0603",
    },
)

_NON_PARAMETER_KEYS = frozenset({"name", "mpn", "category"})


def seed_sample_parts(session: Session) -> int:
    kind = session.execute(select(PartKind).where(PartKind.slug == "component")).scalar_one()
    templates = {row.name: row for row in session.execute(select(ParameterTemplate)).scalars()}
    categories = {row.slug: row for row in session.execute(select(PartCategory)).scalars()}

    created = 0
    for spec in SAMPLE_PARTS:
        if session.execute(select(Part).where(Part.mpn == spec["mpn"])).scalar_one_or_none():
            continue
        category = categories.get(spec["category"])
        part = Part(
            name=spec["name"],
            mpn=spec["mpn"],
            # Via the shared normaliser, not a local `casefold()`. The resolver's
            # bare-MPN step looks this column up by the key `normalize_mpn`
            # produces, so a row written under a different rule is invisible to a
            # scan while looking perfectly correct in the table.
            mpn_norm=normalize_mpn(spec["mpn"]),
            part_kind_id=kind.id,
            category_id=category.id if category else None,
        )
        session.add(part)
        session.flush()

        for key, raw in spec.items():
            if key in _NON_PARAMETER_KEYS:
                continue
            template = templates[key]
            if template.value_type == ValueType.NUMERIC:
                parameters.set_numeric(session, part, template, raw)
            else:
                parameters.set_choice(session, part, template, raw)
        created += 1
    session.flush()
    return created


def seed_all(session: Session) -> SeedReport:
    report = SeedReport()
    report.categories = seed_categories(session)
    report.templates, report.choices = seed_parameter_templates(session)
    report.parts = seed_sample_parts(session)
    return report


def main() -> int:
    session = get_session_factory()()
    try:
        report = seed_all(session)
        session.commit()
    finally:
        session.close()

    print(
        f"seeded: {report.categories} categories, {report.templates} templates, "
        f"{report.choices} choices, {report.parts} parts"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
