/**
 * `/provision?code=…` — a well-formed short ID that names nothing.
 *
 * This is a redirect target: `/s/{short_id}` sends a code here when it validates
 * but resolves to no row. That is the blank-tag case — a tag written before its
 * container existed, or one from a batch pre-written at a desk.
 *
 * **Two ways out, and they are genuinely different.**
 *
 * *Adopt* makes the code the container's **own** short ID (`POST
 * /api/locations/{id}/short-id`), so the tag resolves through `object_ids` like
 * any minted code — the printed card, the tag and the database then agree, and the
 * container has one identity rather than an identity plus a redirect. This is the
 * right answer for a tag written at a desk before its drawer existed, which is the
 * usual reason to be on this screen. The server verifies the check symbol and
 * refuses a code held by something else rather than quietly substituting a free
 * one, because a substitute would leave the sticker permanently lying.
 *
 * *Bind as an alias* points the payload at a container that already has its own
 * code, through step 2 of the resolver chain. Right when the tag is a *second*
 * carrier for a container that is already labelled — a drawer whose printed card
 * says one thing and whose tag was written from a different batch. Step 1
 * deliberately yields on a well-formed but unbound code so this stays reachable.
 *
 * Adopting is offered first because it produces one identity instead of two.
 */

import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { ContainerPicker, type PickedContainer } from "../components/ContainerPicker";
import { ErrorBanner, Notice } from "../components/Feedback";
import { assignLocationShortId, bindScanAlias, resolveShortId } from "../lib/api/client";
import { uuid4 } from "../lib/scan/session";
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
  const [adopted, setAdopted] = useState<{ locationId: number; label: string } | null>(null);
  const [showAlias, setShowAlias] = useState(false);

  async function adopt(picked: PickedContainer): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await assignLocationShortId(picked.id, { short_id: code, client_op_id: uuid4() });
      setAdopted({ locationId: picked.id, label: picked.label });
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

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

      {adopted !== null ? (
        <Notice kind="ok" title="Adopted">
          <p style={{ margin: 0 }}>
            {adopted.label} now carries this code as its own. The tag, the printed card and
            the database all say the same thing.
          </p>
          <p style={{ margin: 0 }}>
            <Link to={`/locations/${adopted.locationId}`}>Open it →</Link>
          </p>
        </Notice>
      ) : showAlias ? null : (
        <div className="card">
          <h3>Give this code to a container</h3>
          <p className="muted-note" style={{ margin: 0 }}>
            Makes it that container&apos;s own short ID, so the tag resolves like any minted
            code rather than through a redirect. Refused if the code already names something
            else — a substitute would leave the sticker lying.
          </p>
          <ContainerPicker
            onPick={(picked) => void adopt(picked)}
            actionLabel={busy ? "Adopting…" : "Adopt this code"}
          />
          <ErrorBanner error={error} fallback="Nothing was adopted." />
        </div>
      )}

      {adopted !== null ? null : !showAlias && bound === null ? (
        <p className="muted-note">
          Already labelled with a different code?{" "}
          <button type="button" className="button-link" onClick={() => setShowAlias(true)}>
            Point this tag at it instead
          </button>{" "}
          — the container keeps its own code and this payload becomes a second way in.
        </p>
      ) : bound === null ? (
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
