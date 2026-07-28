/** Small shared pieces: notices, error banners, loading and empty states. */

import type { ReactNode } from "react";

import { describeError } from "../lib/api/errors";

export function Notice({
  kind = "info",
  title,
  children,
}: {
  kind?: "info" | "warn" | "error" | "ok" | undefined;
  title?: string | undefined;
  children?: ReactNode | undefined;
}) {
  return (
    <div className={`notice notice-${kind}`} role={kind === "error" ? "alert" : undefined}>
      {title !== undefined && <h3>{title}</h3>}
      {children}
    </div>
  );
}

/**
 * The server's refusal, said usefully.
 *
 * The `reason` code drives the wording where we have something better to say than
 * the server does, and the server's own message is kept underneath rather than
 * discarded — so a code this build has never heard of still shows the truth.
 */
export function ErrorBanner({
  error,
  fallback,
}: {
  error: unknown;
  fallback?: string | undefined;
}) {
  if (error === null || error === undefined) {
    return null;
  }
  const report = describeError(error, fallback);
  return (
    <Notice kind="error" title={report.template === null ? undefined : `${report.template}`}>
      <p style={{ margin: 0 }}>{report.headline}</p>
      {report.detail !== null && <p className="muted-note">{report.detail}</p>}
      {report.reason !== null && (
        <p className="muted-note mono">
          {report.status === null ? "" : `${report.status} · `}
          {report.reason}
        </p>
      )}
    </Notice>
  );
}

export function Loading({ what }: { what: string }) {
  return (
    <p className="dim" role="status">
      Loading {what}…
    </p>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="dim">{children}</p>;
}
