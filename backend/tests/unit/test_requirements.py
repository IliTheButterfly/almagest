"""A prose description becomes a structured requirement.

Table-driven over a corpus of real-shaped lines, because the interesting claim is
not that any one line parses — it is **how much of the corpus needs no model at
all**, and that number is asserted (`test_most_of_the_corpus_needs_no_model`) so a
change that quietly weakens the deterministic pass fails here rather than showing
up as a bigger model bill.

No session, no migrations and no network: the vocabulary comes from
`factories.seed_vocabulary()`, built from the same constants the seed script
writes rows from, and `tests/integration/test_requirements.py` is what proves that
snapshot matches a real install's.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.models.enums import ValueType
from app.services.requirements import interpret
from app.services.requirements.parser import (
    AMBIGUITY_REASONS,
    DeterministicRequirementParser,
    FieldOrigin,
    Requirement,
    RequirementProvenance,
    looks_like_a_part_number,
)
from app.services.requirements.vocabulary import CategoryVocab, TemplateVocab, Vocabulary
from app.services.search.query_builder import Filter
from tests.factories import seed_vocabulary

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "requirements" / "interpretations.json"


@pytest.fixture
def vocabulary() -> Vocabulary:
    return seed_vocabulary()


@pytest.fixture
def parser(vocabulary: Vocabulary) -> DeterministicRequirementParser:
    return DeterministicRequirementParser(vocabulary)


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """One description and everything the deterministic pass must make of it."""

    text: str
    quantity: int | None = None
    category: str | None = None
    #: template -> the value that must reach `query_builder.Filter`.
    filters: dict[str, str] = field(default_factory=dict)
    mpn_norm: str | None = None
    residue: tuple[str, ...] = ()
    #: `Rejection.reason`s, in no particular order.
    rejections: tuple[str, ...] = ()
    provenance: RequirementProvenance = RequirementProvenance.DETERMINISTIC

    @property
    def is_actionable(self) -> bool:
        return bool(self.filters or self.category or self.mpn_norm)


CORPUS: tuple[Case, ...] = (
    # --- the five lines from the brief ---
    Case(
        text="3x 10k 1% 0603 resistor",
        quantity=3,
        category="resistor",
        # `1%` is re-attached to the value it belongs to, so the interval the
        # executor computes is 9900-10100 ohm rather than a resistance plus an
        # unreadable token. `0603` is the package because curated spellings are
        # matched before values — it also parses cleanly as 603 ohm.
        filters={"resistance": "10k ±1%", "package": "0603_1608"},
    ),
    # The same line in two other word orders, because **only this one used to
    # work.** The tolerance re-attachment ran before the vocabulary pass, so
    # `10k 0603 1% resistor` produced the token `0603 ±1%` — not a package
    # spelling, a perfectly good 603 ohm, contradicting `10k`, and both filters
    # dropped. `10k resistor 1%` produced `resistor ±1%` and read it as a *part
    # number*. Both parsed with `residue == ()` and `confidence: 1.0`, so nothing
    # in this file noticed. See `tests/integration/test_bom_intake_findings.py`.
    Case(
        text="10k 0603 1% resistor",
        category="resistor",
        filters={"resistance": "10k ±1%", "package": "0603_1608"},
    ),
    Case(
        text="10k resistor 1%",
        category="resistor",
        filters={"resistance": "10k ±1%"},
    ),
    Case(
        text="100nF 50V X7R 0603",
        # No category word: `capacitance` and `dielectric` are both scoped to
        # `capacitor` by `applies_to_category`, so the schema supplies it.
        category="capacitor",
        filters={
            "capacitance": "100nF",
            "voltage_rating": "50V",
            "dielectric": "X7R",
            "package": "0603_1608",
        },
    ),
    Case(
        text="a dual op-amp, rail-to-rail, SOIC-8",
        filters={"package": "SOIC-8"},
        # `a` is filler and dropped; the other three words are the signal that a
        # model is worth calling for this line.
        residue=("dual", "op-amp", "rail-to-rail"),
    ),
    Case(
        text="something to level-shift 3.3V to 5V",
        # Both voltages read as `voltage_rating`, and a requirement carrying
        # either one would assert a rating nobody asked for.
        rejections=("contradictory_value",),
        residue=("level-shift",),
        provenance=RequirementProvenance.NONE,
    ),
    Case(text="LM358N", mpn_norm="lm358n"),
    # --- the required edge cases ---
    Case(text="", provenance=RequirementProvenance.NONE),
    Case(text="3x", quantity=3, provenance=RequirementProvenance.NONE),
    Case(
        # A value with no unit and nothing to scope it. Refused *with its
        # candidate list*, never filed under the first template that reads it.
        text="470",
        rejections=("ambiguous_template",),
        provenance=RequirementProvenance.NONE,
    ),
    Case(
        text="0603 1206",
        rejections=("contradictory_choice",),
        provenance=RequirementProvenance.NONE,
    ),
    Case(
        # The wrong quantity domain: 1 MF does not exist, and the value parser
        # already says so. It must arrive as a refusal, not as a silence.
        text="1M capacitor",
        category="capacitor",
        rejections=("implausible",),
    ),
    # --- and enough ordinary lines that the corpus proportion means something ---
    Case(
        text="qty 3 22uF 25V through-hole ceramic capacitor",
        quantity=3,
        category="capacitor",
        filters={
            "capacitance": "22uF",
            "voltage_rating": "25V",
            "mounting_type": "THT",
            "capacitor_technology": "ceramic",
        },
    ),
    Case(
        text="2x 4k7 0805 resistors",
        quantity=2,
        category="resistor",
        filters={"resistance": "4k7", "package": "0805_2012"},
    ),
    Case(
        text="10 x 100nH inductor",
        quantity=10,
        category="inductor",
        filters={"inductance": "100nH"},
    ),
    Case(
        text="0R22 1206 resistor",
        category="resistor",
        filters={"resistance": "0R22", "package": "1206_3216"},
    ),
    Case(
        text="20-30uF ceramic capacitor, surface mount",
        category="capacitor",
        filters={
            "capacitance": "20-30uF",
            "capacitor_technology": "ceramic",
            "mounting_type": "SMD",
        },
    ),
    Case(text="RC0603FR-0710KL", mpn_norm="rc0603fr0710kl"),
    Case(text="5x 1N4148 DO-35", quantity=5, mpn_norm="1n4148", filters={"package": "DO-35"}),
    Case(
        text="100nF 1608 X5R",
        category="capacitor",
        # `1608` is the metric spelling of the same package. Dual notation is
        # curated, so the user is never asked which convention they meant.
        filters={"capacitance": "100nF", "package": "0603_1608", "dielectric": "X5R"},
    ),
)


@pytest.mark.parametrize("case", CORPUS, ids=lambda case: case.text or "<empty>")
def test_the_corpus_parses_as_specified(parser: DeterministicRequirementParser, case: Case) -> None:
    requirement = parser.parse(case.text)

    assert requirement.text == case.text, "the input is always preserved verbatim"
    assert requirement.quantity == case.quantity
    assert requirement.category_slug == case.category
    assert {item.template: item.value for item in requirement.filters} == case.filters
    assert requirement.mpn_norm == case.mpn_norm
    assert requirement.residue == case.residue
    assert sorted(item.reason for item in requirement.rejections) == sorted(case.rejections)
    assert requirement.provenance == case.provenance
    assert requirement.is_actionable == case.is_actionable


@pytest.mark.parametrize("case", CORPUS, ids=lambda case: case.text or "<empty>")
def test_every_filter_carries_a_source_and_reaches_the_executor_shape(
    parser: DeterministicRequirementParser, case: Case
) -> None:
    """No predicate without traceable words, and no shape the executor cannot take.

    Traceability is asserted **per word** rather than as one substring, because a
    re-attached tolerance need not be adjacent to its value once curated spellings
    are taken out of the stream first: `10k 0603 1% resistor` quotes `10k 1%`, and
    both of those are words the user wrote. What must never appear is a word — or a
    spelling like `±1%` — that is not in the input, which is what this checks.
    """
    requirement = parser.parse(case.text)
    written = case.text.casefold().split()

    for item in requirement.filters:
        assert item.source_text, f"{item.template} has no source text"
        for word in item.source_text.casefold().split():
            assert word in written, f"{item.template} quotes {word!r}, which is not in the input"
        assert item.origin is FieldOrigin.DETERMINISTIC
        assert item.confidence == 1.0
    assert requirement.to_filters() == tuple(
        Filter(template=item.template, value=item.value) for item in requirement.filters
    )


def test_most_of_the_corpus_is_read_into_something_searchable_without_a_model(
    parser: DeterministicRequirementParser, vocabulary: Vocabulary
) -> None:
    """The claim the whole design rests on, as a number that cannot be gamed.

    **Measured on `is_actionable`, not on `residue` being empty**, and that change
    is the point. `not residue` only says "nothing was left unread", which the empty
    string satisfies — so `""`, `"3x"`, `"470"` and `"0603 1206"` all scored as
    successes while producing nothing the executor could run, and four of eighteen
    lines were padding the number the docstring claimed.

    It was also blind in the exact direction it was written to catch. It promised to
    fail on "a change that weakens tokenising ... or matching values before curated
    spellings" — and `parse("10k 0603 1% resistor")` did precisely that, producing
    **zero** filters with `residue == ()`. Adding that line to the corpus would have
    *raised* the asserted proportion. It is in the corpus now, and it counts here
    only if it parses into predicates.

    Three floors rather than one, because they fail differently: actionability
    catches a line that stopped producing anything, the filter set catches a line
    that silently lost its predicates while keeping its category, and the residue
    list catches the reverse — a parser that started guessing at prose.
    """
    parsed = [(case, parser.parse(case.text)) for case in CORPUS]

    actionable = [case for case, requirement in parsed if requirement.is_actionable]
    assert len(actionable) / len(CORPUS) >= 0.7, (
        f"only {len(actionable)}/{len(CORPUS)} lines produced anything the executor "
        f"could run: {[case.text for case, r in parsed if not r.is_actionable]}"
    )

    # Every line the corpus says carries predicates produced them, and no other did.
    # This is the assertion `10k 0603 1% resistor` would have failed.
    assert [case.text for case, requirement in parsed if requirement.filters] == [
        case.text for case in CORPUS if case.filters
    ]
    with_filters = [case for case, requirement in parsed if requirement.filters]
    assert len(with_filters) / len(CORPUS) >= 0.55, (
        f"only {len(with_filters)}/{len(CORPUS)} lines produced a parametric filter"
    )

    # And the lines that need a model are the two prose ones, not a parsing gap.
    assert [case.text for case, requirement in parsed if requirement.residue] == [
        "a dual op-amp, rail-to-rail, SOIC-8",
        "something to level-shift 3.3V to 5V",
    ]
    assert all(
        interpret.request_for_residue(requirement, vocabulary) is None
        for _case, requirement in parsed
        if not requirement.residue
    )


# ---------------------------------------------------------------------------
# The orderings and gates that are load-bearing on their own
# ---------------------------------------------------------------------------


def test_a_curated_spelling_beats_a_value_reading(
    parser: DeterministicRequirementParser,
) -> None:
    """`0603` is a package, not 603 ohm — and the wrong order is silent.

    With values read first, this line gets `resistance` twice (10k and 603), which
    is dropped as a contradiction and leaves a resistor requirement with no
    resistance. A line with no other value gets a 603 ohm requirement that looks
    entirely reasonable.
    """
    requirement = parser.parse("10k 0603 resistor")

    assert {item.template: item.value for item in requirement.filters} == {
        "resistance": "10k",
        "package": "0603_1608",
    }
    assert not requirement.rejections


def test_a_multi_word_spelling_is_matched_as_a_phrase(
    parser: DeterministicRequirementParser,
) -> None:
    requirement = parser.parse("22uF 16V ceramic capacitor, through hole")

    assert {item.template: item.value for item in requirement.filters}["mounting_type"] == "THT"
    assert not requirement.residue


def test_a_bare_number_is_read_under_the_category_that_scopes_it(
    parser: DeterministicRequirementParser,
) -> None:
    """The same token, two categories, two readings — and neither is a guess."""
    assert {item.template: item.value for item in parser.parse("470 resistor").filters} == {
        "resistance": "470"
    }
    assert {item.template: item.value for item in parser.parse("470nH inductor").filters} == {
        "inductance": "470nH"
    }
    # `voltage_rating` applies to every category, so it can never be the unique
    # reading of a bare number: `470 resistor` must not become 470 V.
    assert "voltage_rating" not in {item.template for item in parser.parse("470 resistor").filters}


def test_an_ambiguous_unit_is_refused_with_its_candidates() -> None:
    """Two templates sharing a base unit make a unit-bearing token ambiguous too.

    The seed has one template per quantity, so this needs a vocabulary of its own —
    which is the point: the rule is "exactly one template read it", not "one
    template per unit", and an install that adds `forward_voltage` must not start
    filing every `50V` as a rating.
    """
    vocabulary = Vocabulary(
        templates=(
            TemplateVocab(
                name="voltage_rating",
                display_name="Voltage rating",
                value_type=ValueType.NUMERIC,
                base_unit="volt",
            ),
            TemplateVocab(
                name="forward_voltage",
                display_name="Forward voltage",
                value_type=ValueType.NUMERIC,
                base_unit="volt",
            ),
        ),
        categories=(CategoryVocab(slug="diode", name="Diodes"),),
    )
    requirement = DeterministicRequirementParser(vocabulary).parse("50V diode")

    assert not requirement.filters
    rejection = requirement.rejections[0]
    assert rejection.reason == "ambiguous_template"
    assert rejection.candidates == ("voltage_rating", "forward_voltage")


def test_the_implausible_refusal_propagates_with_its_reason(
    parser: DeterministicRequirementParser,
) -> None:
    """`1M` under capacitance is megafarads. The parser already refuses it.

    Asserted on `reason`, not on the message: `reason` is the stable code the
    review queue routes on, and swallowing this refusal would leave a capacitor
    requirement that silently has no capacitance.
    """
    requirement = parser.parse("1M X7R capacitor")

    rejection = next(item for item in requirement.rejections if item.source_text == "1M")
    assert rejection.reason == "implausible"
    assert rejection.template == "capacitance"
    assert "capacitance" not in {item.template for item in requirement.filters}
    # And the same text under a resistor is a perfectly good megohm.
    assert {item.template: item.value for item in parser.parse("1M resistor").filters} == {
        "resistance": "1M"
    }


def test_a_value_is_never_taken_for_a_part_number(
    parser: DeterministicRequirementParser,
) -> None:
    """The `reads_as_a_quantity` gate, shared with `bom_import._mpn_candidates`.

    A catalogue really can contain a part named `10K`, and matching a `10k`
    resistor line to it puts a part of unknown tolerance and unknown power rating
    into a build and calls the line identified.
    """
    for value in ("10k", "4k7", "100nF", "0R22", "22p", "16MHz", "0603"):
        assert not looks_like_a_part_number(value), value
    for part_number in ("LM358N", "74HC595", "1N4148", "STM32F103C8T6", "RC0603FR-0710KL"):
        assert looks_like_a_part_number(part_number), part_number

    assert parser.parse("3x 10k resistor").mpn_norm is None


def test_a_part_number_is_a_lookup_key_and_not_an_identity(
    parser: DeterministicRequirementParser,
) -> None:
    requirement = parser.parse("2x LM358N")

    assert requirement.quantity == 2
    assert (requirement.mpn, requirement.mpn_norm) == ("LM358N", "lm358n")
    # Nothing here resolves identity. There is no field for a part, on purpose:
    # that a description says `LM358N` is not evidence the catalogue's `LM358N` is
    # the part meant, and only the next stage may say so.
    assert not hasattr(requirement, "part_id")


def test_two_part_numbers_in_one_line_are_refused(
    parser: DeterministicRequirementParser,
) -> None:
    requirement = parser.parse("LM358N or TL072CP")

    assert requirement.mpn_norm is None
    assert [item.reason for item in requirement.rejections] == ["ambiguous_mpn"]


def test_two_category_words_leave_no_category(parser: DeterministicRequirementParser) -> None:
    requirement = parser.parse("0603 resistor capacitor")

    assert requirement.category is None
    assert "contradictory_category" in {item.reason for item in requirement.rejections}
    # The inference must not step in behind the contradiction either.
    assert requirement.category_slug is None


def test_a_quantity_needs_an_explicit_marker(parser: DeterministicRequirementParser) -> None:
    """A bare leading integer is a value, and `5x20` is a dimension."""
    assert parser.parse("3x 10k resistor").quantity == 3
    assert parser.parse("3 x 10k resistor").quantity == 3
    assert parser.parse("qty 3 10k resistor").quantity == 3
    assert parser.parse("3 pcs 10k resistor").quantity == 3
    assert parser.parse("470 resistor").quantity is None
    assert parser.parse("5x20mm fuse").quantity is None


def test_every_ambiguity_reason_used_is_declared() -> None:
    """`AMBIGUITY_REASONS` is what a UI groups on, so it must stay exhaustive."""
    assert "implausible" not in AMBIGUITY_REASONS, "grammar reasons are not ours to declare"
    assert {"ambiguous_template", "contradictory_choice", "ambiguous_mpn"} <= AMBIGUITY_REASONS


def test_confidence_is_about_the_fields_present_and_completeness_is_separate(
    parser: DeterministicRequirementParser,
) -> None:
    """The honest pairing. A line can be exact about one thing and blank about three."""
    partial = parser.parse("a dual op-amp, rail-to-rail, SOIC-8")

    assert partial.confidence == 1.0, "the one field it has is an exact lookup"
    assert not partial.is_complete, "and three words were not accounted for"
    assert partial.residue == ("dual", "op-amp", "rail-to-rail")

    whole = parser.parse("100nF 50V X7R 0603")
    assert (whole.confidence, whole.is_complete) == (1.0, True)

    nothing = parser.parse("")
    assert (nothing.confidence, nothing.is_actionable) == (0.0, False)


# ---------------------------------------------------------------------------
# The model seam
# ---------------------------------------------------------------------------


@pytest.fixture
def fake() -> interpret.FakeInterpreter:
    return interpret.FakeInterpreter(FIXTURE)


def _request(
    parser: DeterministicRequirementParser, vocabulary: Vocabulary, text: str
) -> interpret.InterpretationRequest:
    request = interpret.request_for_residue(parser.parse(text), vocabulary)
    assert request is not None
    return request


def test_no_request_is_built_when_the_grammar_was_enough(
    parser: DeterministicRequirementParser, vocabulary: Vocabulary
) -> None:
    assert interpret.request_for_residue(parser.parse("100nF 50V X7R 0603"), vocabulary) is None
    assert interpret.request_for_residue(parser.parse("LM358N"), vocabulary) is None


def test_the_request_carries_the_residue_and_not_the_refusals(
    parser: DeterministicRequirementParser, vocabulary: Vocabulary
) -> None:
    """A refused value is a review item, not a prompt.

    The only thing a model could do with `1M` under farads, or with a
    contradiction between `3.3V` and `5V`, is talk us out of a correct refusal.
    """
    request = _request(parser, vocabulary, "something to level-shift 3.3V to 5V")

    assert request.residue == ("level-shift",)
    assert "3.3V" not in str(request.residue)
    assert request.established == ()
    assert request.established_category is None


def test_a_request_with_no_residue_is_refused_at_construction(vocabulary: Vocabulary) -> None:
    with pytest.raises(ValueError, match="needs residue"):
        interpret.InterpretationRequest(text="100nF", vocabulary=vocabulary)


def test_the_schema_cannot_express_a_part_number(
    parser: DeterministicRequirementParser, vocabulary: Vocabulary
) -> None:
    """The invariant, made structural: there is no field for a part number.

    A schema-constrained decode then makes one unrepresentable rather than merely
    rejected, which matters because a wrong-but-confident part ID is worse than
    "unknown".
    """
    schema = interpret.schema_for(_request(parser, vocabulary, "a dual op-amp, SOIC-8"))

    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "category",
        "category_confidence",
        "category_source_text",
        "filters",
    }
    item = schema["properties"]["filters"]["items"]
    assert set(item["properties"]) == {"template", "value", "confidence", "source_text"}
    assert item["required"] == ["template", "value", "confidence", "source_text"]
    assert "mpn" not in json.dumps(schema).casefold()
    # Only this install's own parameters and categories are offered.
    assert item["properties"]["template"]["enum"] == [
        template.name for template in vocabulary.templates
    ]
    assert None in schema["properties"]["category"]["enum"]


def _answer(
    *,
    template: str = "package",
    value: str = "SOIC-8",
    confidence: float = 0.5,
    source_text: str = "x",
) -> dict[str, object]:
    """One well-formed answer entry, for a test to break in exactly one place."""
    return {
        "template": template,
        "value": value,
        "confidence": confidence,
        "source_text": source_text,
    }


@pytest.mark.parametrize(
    ("answer", "match"),
    [
        ({"filters": [], "mpn": "LM358N"}, "may not name a part"),
        ({"filters": [], "part_number": "LM358N"}, "may not name a part"),
        ({"filters": [], "manufacturer_part_number": "LM358"}, "may not name a part"),
        ({"filters": [], "notes": "hello"}, "not part of the answer shape"),
        ({"filters": "none"}, "must be an array"),
        ({"filters": [_answer(template="invented")]}, "was not asked for"),
        ({"filters": [_answer(value="TQFP-64")]}, "is not a choice of package"),
        (
            {"filters": [_answer(template="capacitance", value="1M")]},
            "is not a legal capacitance",
        ),
        ({"filters": [_answer(confidence=1.4)]}, "must be between 0 and 1"),
        (
            {"filters": [{"template": "package", "value": "SOIC-8", "confidence": 0.5}]},
            "source_text must be a non-empty string",
        ),
        (
            {"filters": [_answer(), _answer(value="DIP-8", source_text="y")]},
            "is answered twice",
        ),
        ({"filters": [], "category": "sprockets", "category_confidence": 0.5}, "not a category"),
        ({"filters": [], "category": "ic"}, "category_confidence must be a number"),
    ],
)
def test_a_malformed_answer_is_refused(
    parser: DeterministicRequirementParser,
    vocabulary: Vocabulary,
    answer: dict[str, object],
    match: str,
) -> None:
    """Refused, never repaired.

    An answer that half-parses is exactly when guessing what a model meant puts an
    invented predicate into a search with a real confidence attached to it. The
    part-number keys are refused **by name** so the error says why, rather than
    reading as a typo.
    """
    request = _request(parser, vocabulary, "a dual op-amp, SOIC-8")

    with pytest.raises(interpret.InterpretationError, match=match):
        interpret.parse_interpretation(answer, request, provider="p", model="m")


def test_an_answer_is_canonicalised_to_what_the_executor_accepts(
    parser: DeterministicRequirementParser, vocabulary: Vocabulary
) -> None:
    """A model answers in the words it was offered; the key is what travels."""
    request = _request(parser, vocabulary, "a dual op-amp, SOIC-8")
    answer = interpret.parse_interpretation(
        {
            "filters": [
                {
                    "template": "mounting_type",
                    "value": "surface mount",
                    "confidence": 0.8,
                    "source_text": "SOIC-8",
                }
            ]
        },
        request,
        provider="p",
        model="m",
    )

    assert answer.filters[0].value == "SMD"


def test_an_interpreted_confidence_is_capped(
    parser: DeterministicRequirementParser, vocabulary: Vocabulary
) -> None:
    """A model's self-report is not evidence.

    `Requirement.confidence` is the weakest field, so an uncapped 1.0 here would
    let a model make a whole mixed-origin line present as grammar-certain.
    """
    request = _request(parser, vocabulary, "a dual op-amp, SOIC-8")
    answer = interpret.parse_interpretation(
        {
            "filters": [
                {
                    "template": "mounting_type",
                    "value": "SMD",
                    "confidence": 1.0,
                    "source_text": "op-amp",
                }
            ],
            "category": "ic",
            "category_confidence": 1.0,
        },
        request,
        provider="p",
        model="m",
    )

    # Asserted against the literal, not against the constant: comparing to
    # `MAX_INTERPRETED_CONFIDENCE` would pass no matter what that constant became,
    # including 1.0, which is the exact regression this test exists to catch.
    assert interpret.MAX_INTERPRETED_CONFIDENCE < 1.0
    assert answer.filters[0].confidence == 0.9
    assert answer.category_confidence == 0.9


def test_the_fake_replays_a_recorded_answer_through_the_real_parser(
    parser: DeterministicRequirementParser,
    vocabulary: Vocabulary,
    fake: interpret.FakeInterpreter,
) -> None:
    text = "a dual op-amp, rail-to-rail, SOIC-8"
    request = _request(parser, vocabulary, text)

    answer = fake.interpret(request)

    assert fake.calls == [request]
    assert answer.model == "hand-authored-fixture"
    # The fixture answers in an alias spelling, and it came back as the key —
    # which is only true because the fake goes through `parse_interpretation`.
    assert answer.filters[0].value == "SMD"


def test_the_fake_raises_rather_than_inventing_an_empty_answer(
    parser: DeterministicRequirementParser,
    vocabulary: Vocabulary,
    fake: interpret.FakeInterpreter,
) -> None:
    request = _request(parser, vocabulary, "a widget, gadget, SOIC-8")

    with pytest.raises(interpret.InterpretationFixtureMiss):
        fake.interpret(request)


def test_applying_an_interpretation_carries_confidence_and_origin_through(
    parser: DeterministicRequirementParser,
    vocabulary: Vocabulary,
    fake: interpret.FakeInterpreter,
) -> None:
    """The whole point of the seam: a model's guess is visible as one."""
    text = "a dual op-amp, rail-to-rail, SOIC-8"
    requirement = parser.parse(text)
    merged = interpret.apply_interpretation(
        requirement, fake.interpret(_request(parser, vocabulary, text)), vocabulary
    )

    by_template = {item.template: item for item in merged.filters}
    assert by_template["package"].origin is FieldOrigin.DETERMINISTIC
    assert by_template["mounting_type"].origin is FieldOrigin.INTERPRETED
    assert by_template["mounting_type"].confidence == 0.81
    assert merged.category is not None
    assert (merged.category.slug, merged.category.origin) == ("ic", FieldOrigin.INTERPRETED)
    # The weakest field is the line's confidence, and the line says so.
    assert merged.provenance is RequirementProvenance.MIXED
    assert merged.confidence == 0.72
    # Residue shrinks to the words no accepted answer quoted.
    assert merged.residue == ("dual", "rail-to-rail")
    # And nothing a model can say touches these.
    assert (merged.text, merged.quantity, merged.mpn_norm) == (text, None, None)


def test_the_grammar_wins_over_a_contradicting_model(
    parser: DeterministicRequirementParser, vocabulary: Vocabulary
) -> None:
    """`100nF` is not a matter of opinion."""
    requirement = parser.parse("100nF X7R 0603 for decoupling")
    assert requirement.residue == ("decoupling",)

    interpretation = interpret.Interpretation(
        provider="p",
        model="m",
        filters=(
            interpret.InterpretedFilter(
                template="capacitance", value="1uF", confidence=0.9, source_text="decoupling"
            ),
        ),
        category_slug="resistor",
        category_confidence=0.9,
        category_source_text="decoupling",
    )
    merged = interpret.apply_interpretation(requirement, interpretation, vocabulary)

    assert {item.template: item.value for item in merged.filters}["capacitance"] == "100nF"
    assert merged.category_slug == "capacitor"
    assert [item.reason for item in merged.rejections] == [
        "model_contradicted_grammar",
        "model_contradicted_grammar",
    ]
    assert merged.provenance is RequirementProvenance.DETERMINISTIC


def test_an_unparseable_description_is_a_normal_outcome(
    parser: DeterministicRequirementParser,
) -> None:
    """A description nobody can parse still becomes a requirement.

    `bom_lines.part_id` is nullable and losing the line is worse than not
    understanding it, so this must not raise and must not discard the text.
    """
    requirement = parser.parse("that thing Dave used on the mixer board")

    assert isinstance(requirement, Requirement)
    assert requirement.text == "that thing Dave used on the mixer board"
    assert not requirement.is_actionable
    assert requirement.confidence == 0.0
    assert requirement.provenance is RequirementProvenance.NONE
    assert requirement.residue == ("that", "thing", "Dave", "used", "on", "mixer", "board")


# ---------------------------------------------------------------------------
# The live contract test
# ---------------------------------------------------------------------------


def _live_interpreter() -> interpret.RequirementInterpreter | None:
    """The interpreter named by `ALMAGEST_REQUIREMENT_INTERPRETER`, as `module:factory`.

    An environment variable rather than a settings key because **nothing in the API
    process reads it**: per `docs/adr/0005` and the deployment notes the model runs
    as a Job on the local Qwen models that releases the GPU, and the API is a
    single replica pinned to an RWO SQLite volume that must not grow an inference
    dependency. When a real interpreter lands it satisfies
    `RequirementInterpreter` and this hook is how the live test reaches it — no
    other line of this file changes.
    """
    import importlib
    import os

    target = os.environ.get("ALMAGEST_REQUIREMENT_INTERPRETER")
    if not target:
        return None
    module_name, _, attribute = target.rpartition(":")
    factory = getattr(importlib.import_module(module_name), attribute)
    interpreter: interpret.RequirementInterpreter = factory()
    return interpreter


@pytest.mark.live
def test_live_interpretation_matches_the_recorded_contract(
    parser: DeterministicRequirementParser, vocabulary: Vocabulary
) -> None:
    """Catch drift: a real model still answers in the shape this module enforces.

    Never runs in CI, and the assertions are about **shape**, not values. A model
    is not required to agree with the fixture — it is required to answer only with
    parameters it was offered, to quote the words it read each one from, to report
    a confidence in range, and above all never to name a part. Those are the
    properties everything downstream is built on, and a model or prompt upgrade is
    exactly what silently breaks one of them.
    """
    interpreter = _live_interpreter()
    if interpreter is None:
        pytest.skip("set ALMAGEST_REQUIREMENT_INTERPRETER=module:factory to run this")

    text = "a dual op-amp, rail-to-rail, SOIC-8"
    request = _request(parser, vocabulary, text)
    answer = interpreter.interpret(request)

    for item in answer.filters:
        assert item.template in request.field_names
        assert item.source_text
        assert 0.0 <= item.confidence <= interpret.MAX_INTERPRETED_CONFIDENCE
    if answer.category_slug is not None:
        assert answer.category_slug in request.category_slugs

    merged = interpret.apply_interpretation(parser.parse(text), answer, vocabulary)
    assert merged.mpn_norm is None, "an interpreter may never produce a part number"
    assert {item.template: item.value for item in merged.filters}["package"] == "SOIC-8"


def test_the_live_contract_test_is_collected_and_skipped_by_default(
    request: pytest.FixtureRequest,
) -> None:
    """Nobody deletes the live test, and nobody lets CI dial a model.

    Two failure modes, opposite directions. The marker going missing means CI
    starts making network calls; the *test* going missing means provider drift
    stops being detectable and nothing ever says so. Referencing the function by
    name is what makes the second one a collection error rather than a silence.
    """
    live_test = test_live_interpretation_matches_the_recorded_contract
    assert "live" in {mark.name for mark in live_test.pytestmark}

    collected = [
        item
        for item in request.session.items
        if item.name == "test_live_interpretation_matches_the_recorded_contract"
    ]
    if not collected:
        pytest.skip("the live test was not collected in this run")
    assert any(marker.name == "skip" for marker in collected[0].own_markers), (
        "conftest.pytest_collection_modifyitems must skip live tests unless -m live"
    )


def test_the_fixture_says_it_was_hand_authored() -> None:
    """The fixture is ground truth for every test above it, so it says what it is."""
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert "HAND-AUTHORED" in body["_provenance"]
    assert body["model"] == "hand-authored-fixture"
