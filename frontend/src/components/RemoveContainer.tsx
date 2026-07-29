/**
 * Removing a container — the confirm panel, and the undo for the half that can be undone.
 *
 * Iliana: *"I also noticed that I wasn't able to remove items in the workshop."*
 * There was no delete for a location anywhere.
 *
 * The whole design of this component is one rule: **never ask for a confirmation
 * whose consequence you have not stated.** The backend already decides, per node,
 * between deleting the row, retiring it — the ledger, a printed label or a stuck-on
 * tag names it, so the row and its history stay while the container leaves the tree
 * — and refusing outright because stock is inside. So this asks it first
 * (`GET /api/locations/{id}/removal`, which writes nothing) and shows that answer.
 * Three genuinely different words come out of that:
 *
 * * *"Delete it. This cannot be undone."* Nothing names the row. This is the common
 *   case and the one Iliana hit: an empty cell stamped out of a template.
 * * *"Remove it. It can be restored."* The row survives because history names it.
 * * *"This cannot be removed: <what is inside it>."* Never a bare "constraint
 *   failed", and never an offer to move the stock somewhere — where it goes is a
 *   ledger movement and the user's decision.
 *
 * Self-contained on purpose: the affordance belongs inside the storage screen's
 * **edit mode**, which is being built alongside this. Everything it needs is a
 * `LocationRead` and a reload callback, so hosting it is one line wherever that
 * lands.
 */

import { useCallback, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ErrorBanner, Notice } from "./Feedback";
import {
  previewLocationRemoval,
  removeLocation,
  restoreLocation,
  type LocationRead,
  type RemovalPreview,
} from "../lib/api/client";

/** What each `pins` code means on screen — why a row could not simply be deleted. */
const PIN_WORDS: Readonly<Record<string, string>> = {
  has_lots: "a lot has sat in it",
  in_ledger: "the stock ledger records movements through it",
  printed: "a label has been printed for it",
  bound_tag: "an NFC tag is stuck to it",
  pinned_by_child: "something inside it is being kept",
};

function pinWords(pins: readonly string[]): string {
  const known = pins.map((pin) => PIN_WORDS[pin] ?? pin);
  return known.length === 0 ? "" : known.join(", ");
}

/**
 * The counts the panel's headline is built from.
 *
 * Split out so the wording is derived from the plan rather than chosen by whoever
 * wrote the JSX: "cannot be undone" must appear exactly when at least one row is
 * really going, and never otherwise.
 */
function summarize(preview: RemovalPreview): { deletes: number; retires: number } {
  let deletes = 0;
  let retires = 0;
  for (const node of preview.nodes) {
    if (node.action === "delete") {
      deletes += 1;
    } else {
      retires += 1;
    }
  }
  return { deletes, retires };
}

export function RemoveContainer({
  location,
  onRemoved,
  onChanged,
}: {
  location: LocationRead;
  /** Called after a successful removal. Defaults to navigating to the parent,
   * because the screen the user is standing on has just stopped existing. */
  onRemoved?: (() => void) | undefined;
  /** Called after a restore, when the screen is still valid and only stale. */
  onChanged?: (() => void) | undefined;
}) {
  const navigate = useNavigate();
  const [preview, setPreview] = useState<RemovalPreview | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);

  // Whether the user has ticked "and everything inside it". Held here rather than
  // derived, because it is a second, separate consent: a recursive preview is a
  // different plan and reads differently, so asking for it re-fetches.
  const [recursive, setRecursive] = useState(false);

  const ask = useCallback(
    async (withChildren: boolean) => {
      setBusy(true);
      setError(null);
      try {
        setRecursive(withChildren);
        setPreview(await previewLocationRemoval(location.id, withChildren));
      } catch (caught) {
        setError(caught);
      } finally {
        setBusy(false);
      }
    },
    [location.id],
  );

  const confirm = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await removeLocation(location.id, recursive);
      setPreview(null);
      if (result.retired_location_ids.length > 0) {
        // Still a real screen — the row is there, it is just out of the tree — so
        // stay on it and let the restore button below be the undo.
        setDone("Removed. It can be restored.");
        onChanged?.();
      } else if (onRemoved !== undefined) {
        onRemoved();
      } else {
        navigate(location.parent_id === null ? "/tree" : `/locations/${location.parent_id}`);
      }
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }, [location.id, location.parent_id, navigate, onChanged, onRemoved, recursive]);

  const bringBack = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await restoreLocation(location.id);
      setDone(null);
      onChanged?.();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }, [location.id, onChanged]);

  if (location.retired_at !== null) {
    return (
      <div className="card">
        <Notice kind="warn" title="This container was removed">
          <p style={{ margin: 0 }}>
            It is out of the storage tree, out of its parent&rsquo;s layout and out of
            auto-assignment. The row itself stays because the stock ledger, a printed label or a
            tag names it — and history is never deleted here.
          </p>
        </Notice>
        <ErrorBanner error={error} fallback="Could not bring that container back." />
        <div className="row">
          <p className="muted-note" style={{ flex: 1, margin: 0 }}>
            Restoring brings it back as an unplaced container inside the same parent: its slot cell
            and its spot on the floor plan were released, and something else may be there now.
          </p>
          <button type="button" onClick={bringBack} disabled={busy}>
            Bring it back
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="card">
      <ErrorBanner error={error} fallback="Could not remove that container." />
      {done !== null && <Notice kind="ok" title={done} />}

      {preview === null ? (
        <div className="row">
          <p className="muted-note" style={{ flex: 1, margin: 0 }}>
            Removing a container never touches stock and never touches history. What it can do
            depends on what is inside it, so you will be told before anything happens.
          </p>
          <button type="button" className="danger" onClick={() => void ask(false)} disabled={busy}>
            Remove this container
          </button>
        </div>
      ) : (
        <ConfirmPanel
          location={location}
          preview={preview}
          recursive={recursive}
          busy={busy}
          onIncludeChildren={() => void ask(true)}
          onConfirm={() => void confirm()}
          onCancel={() => {
            setPreview(null);
            setRecursive(false);
          }}
        />
      )}
    </div>
  );
}

function ConfirmPanel({
  location,
  preview,
  recursive,
  busy,
  onIncludeChildren,
  onConfirm,
  onCancel,
}: {
  location: LocationRead;
  preview: RemovalPreview;
  recursive: boolean;
  busy: boolean;
  onIncludeChildren: () => void;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const { deletes, retires } = summarize(preview);
  const blockedByChildren = preview.blockers.some((b) => b.reason === "has_children");

  return (
    <div role="dialog" aria-label={`Remove ${location.name}`} className="stack">
      {!preview.removable && (
        <Notice
          kind="error"
          title={blockedByChildren ? "There are containers inside this one" : "This still holds stock"}
        >
          <p style={{ margin: 0 }}>{preview.message}</p>
          <ul>
            {preview.blockers.map((blocker) => (
              <li key={blocker.location_id}>
                <span className="mono">{blocker.label_path}</span>
                {blocker.reason === "holds_stock" ? ` — ${blocker.detail}` : ""}
              </li>
            ))}
          </ul>
          {blockedByChildren && !recursive && (
            <button type="button" onClick={onIncludeChildren} disabled={busy}>
              Include everything inside it
            </button>
          )}
        </Notice>
      )}

      {preview.removable && (
        <Notice
          kind={deletes > 0 ? "warn" : "info"}
          title={
            deletes > 0 && retires === 0
              ? "This will be deleted, and cannot be undone"
              : retires > 0 && deletes === 0
                ? "This will be removed, and can be restored"
                : "Some of this is deleted for good, some can be restored"
          }
        >
          <p style={{ margin: 0 }}>
            {deletes > 0 && (
              <>
                {deletes} container(s) will be deleted outright — nothing names them: no lot has
                ever sat in them, the ledger records nothing through them, no label was printed and
                no tag is stuck to them.{" "}
              </>
            )}
            {retires > 0 && (
              <>
                {retires} container(s) keep their row and all their history, and leave the tree.
                Those can be brought back.
              </>
            )}
          </p>
          <ul>
            {preview.nodes.map((node) => (
              <li key={node.location_id}>
                <span className="mono">{node.label_path}</span>{" "}
                {node.action === "delete" ? (
                  <span className="badge badge-warn">deleted</span>
                ) : (
                  <>
                    <span className="badge badge-info">kept, out of the tree</span>{" "}
                    <span className="muted-note">{pinWords(node.pins)}</span>
                  </>
                )}
              </li>
            ))}
          </ul>
        </Notice>
      )}

      <div className="row">
        <button type="button" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <span className="spacer" />
        {preview.removable && (
          <button type="button" className="danger" onClick={onConfirm} disabled={busy}>
            {deletes > 0 && retires === 0 ? "Delete it" : "Remove it"}
          </button>
        )}
      </div>
    </div>
  );
}
