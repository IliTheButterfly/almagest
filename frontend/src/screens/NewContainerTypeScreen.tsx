/**
 * `/container-types/new` — author a container type from nothing.
 *
 * **A blank form is deliberately the second-best route and says so.** Eleven
 * seeded types ship with every install, and "start from Raaco C8-30 and change
 * two numbers" is both faster and much harder to get wrong than filling in
 * fifteen fields — `POST .../clone` exists for exactly that, and the library
 * screen offers it per row. So this screen opens by pointing back at it rather
 * than pretending the blank form is the main path.
 *
 * On success it does **not** navigate away on its own. Creating the type is not
 * the end of the job: the slots still need laying out, and containers still need
 * stamping from it before anything can be put in one. Both are one tap from the
 * confirmation, which is a better answer than dropping the user on an editor and
 * leaving them to work out what is left to do.
 */

import { useState } from "react";
import { Link } from "react-router-dom";

import { ContainerTypeForm } from "../components/ContainerTypeForm";
import { ErrorBanner, Notice } from "../components/Feedback";
import {
  createContainerType,
  type ContainerTypeRead,
} from "../lib/api/client";
import { BLANK_DRAFT, toCreateRequest, type TypeDraft } from "../lib/containers/typeDraft";
import { uuid4 } from "../lib/scan/session";

export function NewContainerTypeScreen() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [created, setCreated] = useState<ContainerTypeRead | null>(null);

  async function save(draft: TypeDraft): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      // Idempotency-guarded like every other create in this app: a doubled tap on
      // bad wifi must not file two types with different slugs.
      const response = await createContainerType(toCreateRequest(draft, uuid4()));
      setCreated(response.container_type);
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  if (created !== null) {
    return (
      <div className="stack">
        <div className="card">
          <div className="row">
            <Link to="/container-types">← container types</Link>
          </div>
          <h1>{created.display_name}</h1>
          <p className="muted-note mono" style={{ margin: 0 }}>
            {created.slug}
          </p>
        </div>
        <Notice kind="ok" title="Created. Two things left to do.">
          <p style={{ margin: 0 }}>
            The type exists, but nothing has been built from it yet — and a type is only a
            template, so no part can go into one until you stamp a real container from it.
          </p>
          <ul className="list">
            <li className="sub">
              <Link to={`/container-types/${created.id}`}>
                Lay out its slots, and add a photo →
              </Link>{" "}
              — merges, relabels and size classes for the compartments it offers.
            </li>
            <li className="sub">
              <Link to={`/containers/new?type=${created.id}`}>
                Create real containers from it →
              </Link>{" "}
              — pick where they go and how many.
            </li>
          </ul>
        </Notice>
      </div>
    );
  }

  return (
    <div className="stack">
      <div className="card">
        <div className="row">
          <Link to="/container-types">← container types</Link>
        </div>
        <h1>A new container type</h1>
        <p className="muted-note" style={{ margin: 0 }}>
          A template — a kind of cabinet, drawer, baseplate or bin — not a container itself.
          Once it exists you stamp as many real containers from it as you like, and each one
          gets its own copy of the layout.
        </p>
      </div>

      <Notice kind="info" title="Cloning an existing one is usually faster">
        <p style={{ margin: 0 }}>
          Eleven types already ship with Almagest, Gridfinity plates and bins among them.
          Copying the closest one and changing what differs beats filling this in from
          scratch, and the copy is yours to edit freely.
        </p>
        <p style={{ margin: 0 }}>
          <Link to="/container-types">Browse the library and clone one →</Link>
        </p>
      </Notice>

      <ErrorBanner error={error} fallback="That container type could not be created." />

      <div className="card">
        <ContainerTypeForm
          initial={BLANK_DRAFT}
          mode="create"
          clonesOnSave={false}
          busy={busy}
          derivedChildView={null}
          onSubmit={(draft) => void save(draft)}
          onCancel={null}
        />
      </div>
    </div>
  );
}
