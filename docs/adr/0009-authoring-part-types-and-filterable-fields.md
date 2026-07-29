# ADR 0009 — Authoring part types, and what happens when two of them want the same field name

**Status:** accepted
**Date:** 2026-07-29
**Schema change:** one column, `parameter_template.is_seed`
(`20260729_1600_3b7c1a94ef20`).

## Context

Iliana:

> "Something I just noticed is that we currently have no way to create new part
> types. The part type creator should allow you to add fields and units/list for
> filtering. You should be able to create a part type while adding a new part or
> as on its own"

The schema supported every part of this already and had **no write path** for any
of it: `part_kinds`, `part_categories`, `parameter_template` and
`parameter_choice` were all populated exclusively by a migration or by
`app/scripts/seed_demo.py`. So "capacitors also have an ESR" was a code change,
and the filter panel could only ever offer what a developer had shipped.

Three things had to be decided rather than merely built.

## Decision 1 — "part type" is two objects, and the UI says which is which

The phrase is ambiguous in this schema, and the ambiguity matters because only
one of the two owns fields:

| | What it is | Owns fields? | Filtered by |
|---|---|---|---|
| `part_kinds` | what something fundamentally *is* — a tool is not a component | **no** | `part_kind=` |
| `part_categories` | where it sits in the taxonomy | **yes**, via `parameter_template.applies_to_category` | `category=` |

**Both are authorable** (`/api/part-kinds`, `/api/part-categories`), and the UI
must not make the user guess: a *kind* is for a different sort of inventory
altogether, a *category* is for somewhere to hang fields. Fields themselves are
the third route, `/api/parameter-fields`.

`slug` is immutable on both. It is not a label — it is the value that appears in
every search request and therefore in every shared search URL, so renaming it
breaks links that already exist. `display_name` is the editable one, which is why
both columns exist.

### Why the write door is not `POST /api/parameter-templates`

That path is already the facet *reader* (`parameter_facets`) — a read that has to
be a POST because it carries the whole current filter set in its body. FastAPI
cannot route two POSTs to one path, and relocating the reader would change an
operation id every generated client already calls. So authoring gets
`/api/parameter-fields`, named after what the user is doing.

## Decision 2 — a field applies to a category *and its descendants*

`facets.py` tested `template.applies_to_category in (None, request.category)`: an
exact string match. A field authored on "Capacitors" was therefore **not** offered
under "Capacitors > Ceramic" — the node parts are actually filed under. The user
would create a field and then not find it, with nothing on screen to explain why.

Fixed in one place, `app.services.parameter_fields.templates_for_category`, used
by both the facet reader and `GET /api/parameter-fields`: a template applies if it
names no category (unchanged, deliberate — `package` and `mounting_type` are
things every part has) **or** names the category or any of its ancestors. That is
the reverse direction from `TreeRepository.subtree`, and it comes out of the same
cached `id_path` with no recursion. Inheritance is computed from the live tree, so
reparenting a category immediately changes which fields it offers.

`GET /api/parameter-fields?category=` reports `inherited` per field, because
editing an inherited field affects every sibling category and the editor has to be
able to say so.

## Decision 3 — a name collision is explained, and the client picks the rule

`parameter_template.name` is globally UNIQUE, and that is **right**: one
real-world concept is one field, so "voltage rating" means one thing whichever
category asks for it, and there is exactly one `substitution_direction` declaring
what satisfies it. Two `voltage` templates would be two answers to one question.

But a collision must be *explained*, not returned as an `IntegrityError`. The
create request carries `on_name_conflict`, with three answers that are all
defensible:

* **`fail`** (default) — 409 `duplicate_name`, with the existing field embedded in
  the response so the UI can ask "did you mean this one?".
* **`reuse`** — adopt the existing field, provided it is *compatible*: same
  `value_type` and same quantity. Options named in the request that the existing
  list field lacks are added, which is additive and cannot invalidate a stored
  value. Incompatible → 409 `incompatible_existing_field`, naming the difference,
  because reusing across quantities would file microfarads and millihenries in one
  field where every stored bound means whichever unit its row was written under.
* **`namespace`** — create a separate field named `<category>.<name>`, for a
  collision that is an accident of vocabulary rather than the same concept.
  Requires `applies_to_category`; without one there is no namespace.

The check runs **inside** the idempotency `work()` callback, not before it, so a
retried submission replays instead of colliding with the field its own first
attempt created.

## Decision 4 — a seeded field is frozen in three places, and never cloned

`container_types` answers "edit a seed" by cloning it. **That is the wrong answer
for a field definition, and this is the argument the brief asked for.** A clone
needs a different `name` — `capacitance-copy` — which no MPN decoder, no datasheet
extractor and no saved search URL refers to, while both rows would then appear
side by side in the facet panel as two fields meaning the same thing. For a
*container type* a divergent copy is the point; for a *field definition* it is the
failure.

So `parameter_template.is_seed` freezes exactly the three identity-bearing
columns — `name`, `value_type`, `base_unit` — and leaves everything else editable:
display name, ordering, plausibility window, substitution direction, and adding
options. None of those can invalidate a stored value. Deleting a seed is refused
outright. `seed_demo` marks the nine shipped templates; a user's own field is never
marked.

## What is refused, and why each refusal is silent otherwise

Every one of these fails *quietly* if unguarded, which is why they are guards
rather than validation:

| Refusal | Reason code | What happens without it |
|---|---|---|
| `base_unit` the parser does not know (`ohms`, `µF`, `Ω`) | `unknown_base_unit` | field is creatable, appears in the filter panel, and refuses every value forever |
| numeric field with no `base_unit` | `missing_base_unit` | same |
| `value_type` change once values exist | `value_type_in_use` | stored rows strand in columns the executor no longer reads |
| `base_unit` change once values exist | `base_unit_in_use` | bounds computed under the old quantity keep answering range queries in the wrong unit |
| deleting a field parts use | `field_in_use` | `parameter_value.template_id` is `CASCADE` — every value is deleted with it, without asking |
| deleting an option parts use | `choice_in_use` | `choice_id` is `RESTRICT`, so the DB refuses as an `IntegrityError`: a 500 with no number in it |
| list field with no options | `no_choices` | a filter that offers nothing and matches nothing while looking like it works |

`base_unit` is validated through `supported_quantity` — the same adapter the search
path parses with — and stored canonicalised, so `OHM` and the alias `resistance`
both become `ohm` rather than three spellings of one quantity.
`GET /api/parameter-fields/base-units` serves the pickable list, so the UI offers a
select rather than the free-text box that produces `ohms` in the first place.

`substitution_direction` is **required, with no default.** It is what makes
substitution search correct by construction, and silently defaulting a voltage
rating to `exact` would mean a 50 V part no longer satisfies a 25 V requirement
with nothing to say so.

## Consequences

* Nothing in this diff writes `parameter_value`. Values still go through
  `app.services.parameters` and only through it, which is what keeps
  `value_min`/`value_max` populated.
* `CategoryNode` (the browse rail) now reports `id`, so a client holding only
  slugs can reach the authoring routes.
* The two headline tests in
  `backend/tests/integration/test_part_type_authoring.py` author a field over the
  API and then **search for a part by it**, because "the field exists" is not the
  deliverable — "you can filter by it" is.
