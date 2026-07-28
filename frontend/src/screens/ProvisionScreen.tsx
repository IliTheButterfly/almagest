/**
 * `/provision?code=…` — a well-formed short ID that names nothing.
 *
 * This is a redirect target: `/s/{short_id}` sends a code here when it validates
 * but resolves to no row. That is the blank-tag case — a tag written before its
 * container existed, or one from a batch pre-written at a desk.
 *
 * **What this screen cannot do yet, and says so.** Adopting a pre-written code as a
 * container's own short ID means writing an `object_ids` row, and no endpoint does
 * that: `POST /api/locations` mints its own code server-side, which by definition
 * cannot be the one already on the tag. Layout authoring and bulk tag provisioning
 * are named in Phase 1 but are not in the API on this branch.
 *
 * **What it can do, and it is not a workaround.** Binding the payload as a barcode
 * alias to an existing container makes the tag resolve from the next tap onward, via
 * step 2 of the resolver chain. Step 1 deliberately *yields* on a well-formed but
 * unbound code precisely so that binding stays reachable — if it claimed the code and
 * stopped, no alias could ever fix it. So this is the designed route, not a hack; the
 * tag simply resolves through the learning loop rather than through `object_ids`.
 */

import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { ErrorBanner, Notice } from "../components/Feedback";
import { bindScanAlias, resolveShortId } from "../lib/api/client";
import { formatShortId, looksLikeShortId, normalizeShortId } from "../lib/shortid";

interface BoundTo {
  readonly entityType: string;
  readonly entityPk: number;
  readonly label: string;
}

export function ProvisionScreen() {
  const [params] = useSearchParams();
  const code = normalizeShortId(params.get("code") ?? "");

  const [targetCode, setTargetCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [bound, setBound] = useState<BoundTo | null>(null);

  async function bind(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const resolved = await resolveShortId(normalizeShortId(targetCode));
      const target = resolved.target;
      if (target === null || target === undefined) {
        setError(new Error("That code does not name anything either."));
        return;
      }
      await bindScanAlias({
        code,
        symbology: "nfc",
        entity_type: target.entity_type as "location" | "part" | "stock_lot",
        entity_pk: target.entity_pk,
        // The tag carries this payload, so the whole payload is what gets bound.
        alias_kind: "whole_payload",
      });
      setBound({
        entityType: target.entity_type,
        entityPk: target.entity_pk,
        label: target.label_path ?? target.label,
      });
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  if (code === "") {
    return (
      <Notice kind="warn" title="No code to provision">
        <p style={{ margin: 0 }}>
          This screen is reached by tapping a tag whose code is well formed but names
          nothing yet. <Link to="/scan">Scan something →</Link>
        </p>
      </Notice>
    );
  }

  return (
    <div className="stack">
      <div className="card">
        <h1>Unbound tag</h1>
        <p className="mono big-number" style={{ fontSize: "1.6rem" }}>
          {formatShortId(code)}
        </p>
        <p className="muted-note" style={{ margin: 0 }}>
          The check symbol is valid, so this really is one of ours — it just does not
          name a row yet.
        </p>
      </div>

      <Notice kind="info" title="Adopting this code is not wired up yet">
        <p style={{ margin: 0 }}>
          Making this the container&apos;s own short ID needs an endpoint that writes
          the ID table, and there is not one on this build: creating a container mints a
          fresh code server-side, which cannot be the code already written to this tag.
          Layout authoring and bulk provisioning land with that endpoint.
        </p>
      </Notice>

      {bound === null ? (
        <form
          className="card"
          onSubmit={(event) => {
            event.preventDefault();
            void bind();
          }}
        >
          <h3>Point this tag at something that exists</h3>
          <p className="muted-note" style={{ margin: 0 }}>
            Binds this payload as an alias, so the tag resolves from the next tap onward.
            Give the short ID printed on the container it belongs to.
          </p>
          <label className="field">
            <span>Container short ID</span>
            <input
              className="mono"
              value={targetCode}
              onChange={(event) => setTargetCode(event.target.value)}
              placeholder="4K7T-92M8"
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
            />
          </label>
          <ErrorBanner error={error} fallback="Nothing was bound." />
          <button
            type="submit"
            className="primary wide"
            disabled={busy || !looksLikeShortId(targetCode)}
          >
            {busy ? "Binding…" : "Bind"}
          </button>
        </form>
      ) : (
        <Notice kind="ok" title="Bound">
          <p style={{ margin: 0 }}>This tag now resolves to {bound.label}.</p>
          <p style={{ margin: 0 }}>
            <Link to={`/${bound.entityType === "location" ? "locations" : "parts"}/${bound.entityPk}`}>
              Open it →
            </Link>
          </p>
        </Notice>
      )}
    </div>
  );
}
