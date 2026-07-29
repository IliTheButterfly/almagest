/**
 * The datasheet section on the part screen.
 *
 * `docs/PLAN.md`'s "QR-to-datasheet" paragraph ends at "part detail → datasheet
 * one tap". The first two hops already exist and are not this component's job:
 * a scanned tag's URL is `/s/{short_id}`, and `app.api.routes.resolve
 * .open_short_id` redirects a resolved part straight to `/parts/{id}` —
 * `PartScreen`. This panel owns the *last* hop: one button, straight to
 * `partDatasheetUrl`, which 307s to the content-addressed bytes and lets the
 * browser's own viewer (`Content-Disposition: inline`) take it from there.
 * `window.open` rather than a router `<Link>` on purpose — the destination is
 * not a screen in this app, it is a PDF, and leaving the SPA for a moment is
 * the correct behaviour, not a gap.
 *
 * Uploading has nowhere to put a `client_op_id` (see the backend route
 * module's docstring: the body is raw bytes, and there is nowhere to put one).
 * The content address is the idempotency key instead, so a doubled tap just
 * re-uploads the same bytes onto the same row — `created: false` in the
 * response, not a duplicate.
 */

import { useState } from "react";

import {
  attachPartDocument,
  detachPartDocument,
  listPartDocuments,
  partDatasheetUrl,
  uploadDocument,
  type DocumentKind,
  type DocumentLinkRead,
  type DocumentRole,
} from "../lib/api/client";
import { useAsync } from "../lib/hooks/useAsync";
import { ErrorBanner, Loading } from "./Feedback";

//: Mirrors `app.models.enums.DocumentRole` / `DocumentKind`. Kept as a literal
//: list rather than derived from a value at runtime because there is nothing
//: to derive it from on this side — the schema only names the type, not its
//: members — and a mismatch here is exactly what `make fe-check` (a real
//: `<select>` rendered against a real backend in CI) would catch.
const ROLES: readonly DocumentRole[] = [
  "datasheet",
  "reference",
  "photo",
  "count_evidence",
  "marking",
  "other",
];
const KINDS: readonly DocumentKind[] = ["datasheet", "app_note", "errata", "drawing", "photo", "other"];

function linkKey(link: DocumentLinkRead): string {
  return `${link.role}:${link.document.sha256}`;
}

export function DocumentsPanel({ partId }: { partId: number }) {
  const documents = useAsync(() => listPartDocuments(partId), [partId]);
  const links = documents.data?.links ?? [];
  const hasPrimaryDatasheet = links.some((link) => link.role === "datasheet" && link.is_primary);

  return (
    <div className="card">
      <h3>Datasheet</h3>
      {hasPrimaryDatasheet ? (
        <button
          type="button"
          className="primary wide tall"
          onClick={() => {
            window.open(partDatasheetUrl(partId), "_blank", "noopener");
          }}
        >
          View datasheet
        </button>
      ) : (
        <p className="dim">
          None yet — a scan lands here in one tap as soon as one is attached below.
        </p>
      )}
      <ErrorBanner error={documents.error} fallback="The attached documents could not be loaded." />
      {documents.loading && documents.data === null ? (
        <Loading what="the attached documents" />
      ) : (
        links.length > 0 && (
          <DocumentList partId={partId} links={links} onChanged={documents.reload} />
        )
      )}
      <UploadDocument partId={partId} onUploaded={documents.reload} />
    </div>
  );
}

function DocumentList({
  partId,
  links,
  onChanged,
}: {
  partId: number;
  links: readonly DocumentLinkRead[];
  onChanged: () => void;
}) {
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  async function setPrimary(link: DocumentLinkRead): Promise<void> {
    setBusyKey(linkKey(link));
    setError(null);
    try {
      await attachPartDocument(partId, {
        sha256: link.document.sha256,
        // `DocumentLinkRead.role` is a plain `str` on the wire (the backend
        // model declares it that way rather than the enum — see
        // `app.api.routes.documents.DocumentLinkRead`), so this is a widening
        // cast back to the request's own enum, not an assumption about a
        // value that could actually be something else: it can only be a role
        // this same list already rendered.
        role: link.role as DocumentRole,
        is_primary: true,
      });
      onChanged();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusyKey(null);
    }
  }

  async function detach(link: DocumentLinkRead): Promise<void> {
    setBusyKey(linkKey(link));
    setError(null);
    try {
      await detachPartDocument(partId, link.document.sha256);
      onChanged();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusyKey(null);
    }
  }

  return (
    <>
      <ErrorBanner error={error} fallback="That change could not be made." />
      <ul className="list">
        {links.map((link) => {
          const key = linkKey(link);
          const busy = busyKey === key;
          return (
            <li key={key} className="list-item">
              <div className="row">
                <span className="title">
                  {link.document.original_filename ?? `${link.document.sha256.slice(0, 12)}…`}
                </span>
                <span className="spacer" />
                <span className="badge">{link.role}</span>
                {link.is_primary && <span className="badge badge-good">primary</span>}
              </div>
              <div className="sub">
                {link.document.media_type} · {Math.max(1, Math.round(link.document.byte_size / 1024))} KiB
                {link.document.page_count !== null && ` · ${link.document.page_count} pages`}
              </div>
              <div className="row">
                <a href={link.document.url} target="_blank" rel="noreferrer">
                  Open
                </a>
                <span className="spacer" />
                {!link.is_primary && (
                  <button type="button" disabled={busy} onClick={() => void setPrimary(link)}>
                    Set primary
                  </button>
                )}
                <button type="button" className="danger" disabled={busy} onClick={() => void detach(link)}>
                  Remove
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </>
  );
}

function UploadDocument({ partId, onUploaded }: { partId: number; onUploaded: () => void }) {
  const [role, setRole] = useState<DocumentRole>("datasheet");
  const [kind, setKind] = useState<DocumentKind>("datasheet");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function handleFile(file: File): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      // The browser's own guess. Never trusted downstream — `app.services
      // .blobstore` sniffs the first bytes regardless — but it is the honest
      // value to *declare*, and a wrong declaration is refused as
      // `content_mismatch` rather than silently stored.
      const mediaType = file.type !== "" ? file.type : "application/pdf";
      await uploadDocument(file, {
        mediaType,
        kind,
        role,
        partId,
        filename: file.name,
        isPrimary: true,
      });
      onUploaded();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <ErrorBanner error={error} fallback="That file could not be uploaded." />
      <div className="row">
        <label className="field">
          <span>Role</span>
          <select
            value={role}
            disabled={busy}
            onChange={(event) => setRole(event.target.value as DocumentRole)}
          >
            {ROLES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Kind</span>
          <select
            value={kind}
            disabled={busy}
            onChange={(event) => setKind(event.target.value as DocumentKind)}
          >
            {KINDS.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="field">
        <span>Upload a PDF, PNG or JPEG</span>
        <input
          type="file"
          accept="application/pdf,image/png,image/jpeg"
          disabled={busy}
          onChange={(event) => {
            const file = event.target.files?.[0];
            // Cleared so picking the *same* file again still fires `onChange` —
            // otherwise a failed upload cannot be retried without picking a
            // different file first.
            event.target.value = "";
            if (file !== undefined) {
              void handleFile(file);
            }
          }}
        />
      </label>
      {busy && <p className="dim">Uploading…</p>}
    </div>
  );
}
