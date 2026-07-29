# ADR 0007 — A container's picture is two things, not one

**Status:** accepted
**Date:** 2026-07-29
**Extends:** [ADR 0002](0002-recursive-container-types.md) and
[ADR 0006](0006-per-layer-child-view.md), which it does not replace.

## Context

Iliana's request, verbatim:

> "I want containers to have icons/pictures so I can easily attribute them to
> how they look. This can be both put on a template and edited per container."

That is a type default with an instance override — the exact shape ADR 0006
just established for `child_view`, and the one `esd_safe`/`is_placeable`/
`fill_factor` set as precedent before it. Reusing that mechanism is not in
question. What is in question is what "a picture" actually means, and the
honest answer is that it means two different things that want to be drawn in
two very different places:

* **A photo.** What a drawer actually looks like, shot from a phone standing in
  front of it. One real image.
* **A glyph.** A small symbol chosen from a set — "this is a drawer", "this is
  a bag" — cheap enough to render at every node of a dense tree.

`frontend/src/components/ContainerLayout.tsx` can lay out a whole baseplate's
grid or a whole cabinet's drawer fronts — dozens of cells — in one screen. That
screen cannot show dozens of photographs; loading and decoding one real image
per cell to draw something that ends up a few dozen pixels wide is a real cost
paid for nothing anyone can see. It can show dozens of tiny symbols for free.

## Decision

**Two mechanisms, not one, because they have different costs and different
places to be shown.**

### The photo is not a new column. It is Phase 4's document store, reused.

`docs/adr/0005-extraction-runs-outside-the-api.md` built a content-addressed
store for exactly this shape of fact: `documents` (one row per file) plus
`document_links` (polymorphic by `entity_type`/`entity_pk`, with an
exactly-one-primary-per-role rule already maintained in
`app.services.documents`). `DocumentRole.PHOTO` — "what the thing looks like" —
already existed, added when parts got photos; it needed no change to serve a
`container_type` or a `location` too. Adding a table, or a `photo_document_id`
column, would have been a second, worse implementation of a problem this schema
already solved.

**"Override" falls out of the polymorphic link, not a stored pointer.** A
location's own primary `PHOTO` link wins; a location with none falls back to
its container type's; detaching a location's own link is what "go back to the
type's" means, with no third state to keep synchronised. `app.api.routes.
documents.primary_photo` is the one place this fallback is computed, called
twice — once for the location, once (only if the first came back empty) for its
type — by both `ContainerTypeRead` and `LocationRead`.

Two new routers, `container_types_router` and `locations_router`, both living in
`app.api.routes.documents` for the same reason `parts_router` already does:
what they return belongs to the document store, not to the type or the
location. `upload_document` grew two more mutually-exclusive attachment targets
(`container_type_id`, `location_id`) alongside the existing `part_id`, refused
together as `ambiguous_attachment` — one upload is one file in one role on one
thing.

**Attaching a photo to a container type is an edit to that type, so a seed
clones first.** Review caught this as a real defect: both attach doors wrote
`document_links` straight through, so dressing a seeded Raaco with a photo of
your own bench changed the row every fresh install starts with — the one
remaining way to edit a seed in place, while `PATCH /api/container-types/{id}`
and `PUT .../slot-template` had gone through
`app.services.layout_authoring.ensure_editable` all along. The rule is
therefore about *what is being changed*, not about which table the change lands
in: a `document_links` row keyed to a seed's id is a change to what every
instance of that type looks like, and `entity_pk` being the only column involved
does not make it less of one. So `POST /api/documents?container_type_id=…` and
`POST /api/container-types/{id}/documents` both clone, and both report where the
link landed (`container_type_id`, `cloned`) so the screen can follow the copy
instead of aiming its next save at the seed again. Detaching needs no guard of
its own, for a reason worth stating rather than assuming: with both doors
cloning, a seed can no longer *hold* a link, so the only answer detach can give
for one is the 404 it already gave.

### The glyph is a new column, because there is nothing to attach it to.

A glyph is not a file; it is a name from a closed-but-growable set. Two
nullable `VARCHAR` columns, `container_types.glyph` and `locations.glyph`, no
`CHECK`, the same shape as `child_view` — and deliberately **not** the same
shape in one respect: there is no third, derived rung.

`resolve_child_view` can fall back to reading a type's declared geometry
because a 42 mm pitch really does imply "a tray seen from above" — the
geometry states the drawing. Nothing about a type's geometry implies what it
*looks like*: a Gridfinity bin and an Akro-Mils drawer can both be `box` or
both be something else, and guessing would be a guess wearing the shape of a
fact. So `app.services.glyphs.resolve_glyph` is two rungs — instance override,
else the type's — and `None` all the way down is a real, terminal answer: "no
glyph chosen yet," which the frontend renders as a clean absence, never a
broken image and never an invented one.

### Where each is drawn, and why that is not interchangeable

* **The dense recursive map** (`ContainerLayout.tsx`, `LocationNode.
  effective_glyph`) draws the glyph, and only the glyph. `LocationNode` is
  already the deliberately cheap shape the tree route returns for every node in
  one response; adding a photo there would mean either fetching a document per
  node (an N+1 the rest of that response is built to avoid) or shipping a
  content-addressed URL nobody asked the browser to fetch a hundred times.
* **A container's own screen** (`LocationRead.photo` / `effective_photo`,
  `ContainerTypeRead.photo`) draws the real photo, falling back to the glyph,
  falling back to a neutral placeholder — `ContainerPhoto`'s three rungs,
  mirroring the resolvers behind it. Exactly one image is ever requested here,
  which is the one place loading one is actually worth it.

## What happens to a 12 MP upload

**The API does nothing to it.** `app.services.blobstore.store` checks five
bytes of magic and a size ceiling (64 MiB) and writes the bytes as given —
that is the whole point of ADR 0005's "the API never processes an image or a
PDF." Left alone, a 3–8 MB phone photo is stored, served with
`Cache-Control: immutable`, and fetched at full resolution by every screen that
shows it, including `ContainerPhoto`'s card view, which renders it capped at a
few hundred pixels tall.

Since nothing on the server will ever shrink it, and the wrong place to notice
that is a production drawer full of 6 MB "icons," the browser does it before
the bytes leave the phone: `frontend/src/lib/images/resize.ts` decodes the
photo (`createImageBitmap`), and — only if the longer edge exceeds 1600 px —
draws it onto a canvas at that size and re-encodes it as JPEG at quality 0.85.
A typical 4000×3000 shot becomes roughly 1600×1200, several MB becoming a few
hundred KB. An already-small image is returned untouched rather than
needlessly re-encoded.

**This never blocks the upload.** Decoding, canvas support, and `toBlob` can
all fail — an old browser, a stripped-down WebView, or simply
`createImageBitmap` not existing at all (true of the test suite's own `jsdom`
environment, which is what `resize.test.ts` actually exercises) — and every one
of those paths returns the original file untouched. A container photo that
occasionally uploads at full size is an acceptable cost; one that occasionally
refuses to upload at all is not.

## Consequences

**Entirely additive**, and, again, the no-`CHECK` rule paying for itself:
`ContainerGlyph` gaining a member is one line in `app/models/enums.py` and one
line in `frontend/src/lib/locations/glyphs.ts`'s rendering map, not a rebuild
of either table. `documents`/`document_links` needed no schema change at all —
`DocumentRole.PHOTO` already existed.

**`is_placeable`, capacity, and every other behaviour are untouched.** Both
mechanisms here are presentation: `_check_grid_compatibility`, the capacity
snapshot, and `app.services.assignment`'s auto-placement predicates read
`child_layout`, `footprint_*`, `capacity_model` and nothing this ADR added.

**Cloning a type copies the glyph and not the photo**, and that asymmetry is
the correct reading of what each one is: `glyph` is a scalar column on the row
being copied, so `app.services.layout_authoring.clone_type` copying it is the
same statement it already makes about `child_view`. The photo is a
`document_links` row pointing at the *source* type; a clone starts unpictured
and earns its own, exactly as a clone starts with no printed `short_id`.

**Never encoded in a printed or tag payload.** Same rule as `child_view` and
the same reasoning: a picture is a fact about a level right now, and levels
move.

## Rejected alternatives

**One mechanism — just a photo, resized down for the tree view.** Tempting,
since it needs no new column at all. Rejected because "resized down" is still
a real image behind a real URL: a 12x8 grid is 96 requests (or, if the row's
own `effective_child_view` is `grid_cells`/`cabinet_face` and it happens to
match the seed library's own dimensions, more), and a container that has never
been photographed — the common case for months after this ships — would have
nothing at all to draw at every single node, `ContainerLayout`'s dense views
included, rather than a cheap identifying symbol.

**One mechanism — just a glyph, with a "custom image" glyph kind that points at
an upload.** This is the photo mechanism wearing the glyph's clothes, and it
reintroduces exactly the N-images-per-grid cost the glyph axis exists to avoid,
the moment anyone actually uses it — a "picture" feature where using the
picture half makes the tree slow is not the feature that was asked for.

**A `photo_document_id` column on both tables instead of a `document_links`
role.** Symmetric with `glyph`, and wrong for the same reason a sixth nullable
FK on `document_links` itself was rejected in Phase 4: the exactly-one-primary
bookkeeping (`app.services.documents.attach`/`detach`) would need reimplementing
for a second, parallel "pointer to a document" mechanism, maintained by hand
instead of by the module that already gets it right.
