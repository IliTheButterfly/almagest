# ADR 0008 — Creating a container is two routes, and the UI must offer both

**Status:** accepted
**Date:** 2026-07-29
**Extends:** [ADR 0002](0002-recursive-container-types.md), which it does not
replace. No schema change.

## Context

Iliana's report, twice:

> "I still cant create my own containers."

Both times against a backend that was complete. `POST /api/container-types`,
`PATCH`, `POST .../clone`, `GET|PUT .../slot-template` and
`POST /api/locations/{id}/instantiate` all existed and were tested; nothing in
`frontend/` called `createContainerType`, `createLocation` or `instantiate`, and
the nav's entry point was labelled **"Types"** — the word for the schema concept,
not for the thing a person wants. A complete API and a screen nobody can find are
indistinguishable from the outside, which is why this is recorded as a decision
rather than as a bug fix.

The second thing the report exposed is that "create a container" is genuinely
**two different requests**, and a UI that offers only the tidier one is stuck:

| Route | Materialises the type's layout | Parent |
|---|---|---|
| `POST /api/locations/{id}/instantiate` | **yes**, into each instance's own child `locations` | required — it is in the path |
| `POST /api/locations` | **no**, even when `container_type_id` is given | optional (`parent_id: null` is a root) |

Those two facts intersect badly at exactly one point: **a fresh install has no
root**, and `instantiate` cannot make one. So an app offering only "stamp from a
type" cannot create the room that everything else hangs off, and an app offering
only the plain create silently produces typed containers with none of their
compartments.

## Decision

**One screen, `/containers/new`, with both routes on it**, chosen by an explicit
mode switch: *From a type* (`instantiate`) and *One plain container*
(`createLocation`). The mode switch is not a wizard step — it is the honest
surface of the two routes, and each says in its own words what it does and does
not create.

**It lives outside `/locations/...`.** Every path in that space is a
`/s/{short_id}` redirect target, so a URL there is a promise to a code already
printed on a card or written into a tag. This screen is reached from the storage
tree, from a container's own screen, and from a type — never from a scan.

**Reachable from where the question is asked**, which is not the type library:

- the storage tree, carrying the position already on screen
  (`/containers/new?parent=<id>`), including in its empty state;
- a container's own screen, as a link kept **separate** from "Edit layout" —
  adding containers creates rows, while editing the layout rearranges existing
  ones and goes through the change guard, and one button for both would put a
  create action behind a guard that exists to protect contents;
- a container type, as "create containers from it";
- the nav, whose "Types" tab is now **"Containers"**. The URL stays
  `/container-types`.

**Clone is a per-row button in the library, not a mode of the create form.**
Eleven seeded types ship with every install; "start from `raaco-c8-30` and change
two fields" is both faster and much harder to get wrong than a blank form, and
`POST .../clone` already implements it in one call. The blank form therefore opens
by pointing back at the library rather than presenting itself as the main path.

**ADR 0002's two questions are two fieldsets with the ADR's own wording** — "what
grid does it offer the things inside it?" and "what space does it take up in
whatever it sits in?" — in one shared component (`ContainerTypeForm`) used by both
create and edit. A create form that phrased them differently from the edit form
would teach two mental models of the same columns, and the failure this guards
against is concrete: a reader who meets "rows" and "columns" twice with no framing
fills one pair in with the other's answer, which is precisely the conflation ADR
0002 decoupled the schema to prevent.

## Consequences

**Editing a seed is a confirmation that names the outcome, not "are you sure".**
`PATCH` on a seed clones it (`ensure_editable`), so the row that comes back is not
the row in the URL. The form says so before the button is pressed, the button
itself reads "Save as my own copy", and the screen follows the returned id —
without that last part a second save clones the seed again.

**Three refusals are handled as themselves.** 409 `duplicate_slug` says the slug
is permanent and suggests cloning instead; 422 `bad_naming_pattern` says only
`{n}` is substituted; and 409 `pitch_mismatch` / `footprint_too_wide` /
`footprint_too_deep` say **refused rather than flagged**, because they are the one
place in the capacity area that is not advisory — a 42 mm bin does not seat on a
50 mm plate, so accepting it would record a world that cannot exist.

**The naming pattern is previewed client-side**, restating the server's two rules
(`{n}` is the index; a count above one with no `{n}` gets one appended). It is a
preview, not a second policy: the server stays the authority and its 422 is still
surfaced verbatim.

## What the API could not express

Recorded here rather than worked around silently:

1. **No route instantiates a type at the top of the tree.** This is the whole
   reason the plain-create path is on this screen. A root container built from a
   type therefore gets its columns but not its compartments, and its slots have to
   be laid out afterwards through the instance layout editor.
2. **A slug cannot be changed.** `ContainerTypeWrite` omits it, so it is chosen
   once, at create. The form says so while the choice is still being made; the
   remedy for a bad slug is a clone.
3. **A container type cannot be deleted.** There is no `DELETE`, so a type created
   by mistake stays in the library forever. The `is_seed` filter makes it possible
   to look at only your own, which is a mitigation and not a fix.
4. **`slot_label_params` is free JSON with no schema.** The form exposes exactly
   one key, `zero_pad`, for the `sequential` scheme, and passes anything else
   through untouched — there is no way to ask the API what keys a scheme accepts.

## Rejected alternatives

**A create form on the library screen, like `ProjectsScreen`'s "New project".** A
project is three fields; a container type is fifteen across five groups, two of
which are only distinguishable by their framing. Inlining it would have put that
framing inside a collapsible panel on a list screen — and the framing is the part
that stops the form being filled in wrong.

**Making "stamp from a type" the only path, and seeding a root.** A seeded root is
a name somebody else chose for a room they have never seen, and the INBOX already
demonstrates how that reads. It also would not have removed the need for
`createLocation`: the second, third and fourth rooms have the same problem as the
first.
