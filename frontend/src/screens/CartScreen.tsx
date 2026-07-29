/**
 * The cart, and the one place it is committed.
 *
 * ADR 0007: one mechanism, three destinations — a project's BOM, a build's
 * reservations, or a plain stock movement against one container. They are one
 * screen because a project cart plus a separate take/return basket would be two
 * lists to keep in step and two places to learn, and because the *rows* are the
 * same rows: which door they leave by is a property of the cart, not of the parts.
 *
 * Three things here are load-bearing rather than decorative:
 *
 * - **The clear button.** The ADR is explicit that choosing the cart does not
 *   avoid the invisible-state failure of a mode you forgot was set, it *moves* it
 *   to a cart you forgot was full. A visible count (the nav badge) and an explicit
 *   clear are the mitigation, so clearing is a first-class action and not buried.
 * - **A refused line stays put, saying why.** After a partial checkout the applied
 *   rows are gone and the refused ones are all that is left, so the reason lives on
 *   the row — a banner would be gone after one navigation and the cart would look
 *   inexplicably non-empty. Checking out again retries only what is left, which is
 *   what makes "fix that one row and press it again" work.
 * - **Quantities and names are what was captured**, not a fresh read. The cart
 *   records what you chose; the server is what knows whether the lot still holds it,
 *   and the reconciliation is the checkout.
 *
 * The container destination can be reached by scanning, which is Iliana's request
 * verbatim — "pick a container, scan it and say how many parts you took or put
 * back". It reuses `useScanner`/`Viewfinder`, the same camera path the scan screen
 * uses, and typing a short ID is a first-class equivalent because ADR 0001 means
 * the camera is *absent*, not merely unpermitted, on a plain-HTTP origin.
 */

import { useState } from "react";
import { Link } from "react-router-dom";

import { CodeEntry } from "../components/CodeEntry";
import { ErrorBanner, Empty, Notice } from "../components/Feedback";
import { Viewfinder } from "../components/Viewfinder";
import {
  getPart,
  getProject,
  listProjects,
  resolveScan,
  undoMovement,
  type BuildRead,
  type PartRead,
  type ProjectRead,
} from "../lib/api/client";
import { cameraNotice, detectCapabilities } from "../lib/capabilities";
import { NO_TARGET, shoppingCart, type CartDirection, type CartLine } from "../lib/cart/cart";
import { checkoutCart, type CheckoutOutcome } from "../lib/cart/checkout";
import { describeTarget } from "../lib/cart/describe";
import { useCartLines, useCartTarget } from "../lib/cart/useCart";
import { formatQty, fromMilli, parseQtyToMilli } from "../lib/format";
import { useAsync } from "../lib/hooks/useAsync";
import { useScanner } from "../lib/scan/useScanner";
import { uuid4 } from "../lib/scan/session";

/** The three doors, in the order the ADR lists them. */
const KINDS = ["project", "build", "container"] as const;
type Kind = (typeof KINDS)[number];

const KIND_LABELS: Record<Kind, string> = {
  project: "A project's BOM",
  build: "A build",
  container: "Take or put back",
};

export function CartScreen() {
  const lines = useCartLines();
  const target = useCartTarget();
  const [direction, setDirection] = useState<CartDirection>("take");
  const [outcome, setOutcome] = useState<CheckoutOutcome | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);

  const failing = lines.filter((line) => line.failure !== null).length;

  async function checkout(): Promise<void> {
    setBusy(true);
    setOutcome(null);
    try {
      // `checkoutCart` is itself double-press safe — a second call while one is in
      // flight joins the same promise — so the busy flag here is for the label,
      // not for correctness.
      setOutcome(await checkoutCart(shoppingCart, { defaultDirection: direction }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <div className="card">
        <div className="row">
          <h1 style={{ flex: 1 }}>Cart</h1>
          <Link to="/cart/add">Add parts →</Link>
        </div>
        <p className="muted-note">
          Parts chosen while browsing, committed to nothing yet. Nothing in this list
          has touched the ledger — checking out is what writes, and it writes to
          exactly one of the three destinations below.
        </p>
        <div className="row">
          <span className="badge">{describeTarget(target)}</span>
          <span className="spacer" />
          {lines.length > 0 &&
            (confirmClear ? (
              <>
                <span className="muted-note">Empty the cart and forget the destination?</span>
                <button
                  type="button"
                  className="danger"
                  onClick={() => {
                    shoppingCart.clear();
                    setConfirmClear(false);
                    setOutcome(null);
                  }}
                >
                  Yes, clear it
                </button>
                <button type="button" onClick={() => setConfirmClear(false)}>
                  Keep it
                </button>
              </>
            ) : (
              <button type="button" className="danger" onClick={() => setConfirmClear(true)}>
                Clear cart
              </button>
            ))}
        </div>
      </div>

      {/* Outside the list, deliberately: a successful checkout empties the cart,
          and reporting the result inside the "you have lines" branch meant the
          confirmation — and the one-call undo for a movement — vanished at the
          exact moment they were wanted. */}
      {outcome !== null && <Result outcome={outcome} />}

      {lines.length === 0 ? (
        <div className="card">
          <Empty>
            Nothing in the cart. <Link to="/cart/add">Search your stock</Link> and add what
            the build needs — the facet counts and the quantity on each row are there to
            decide with.
          </Empty>
        </div>
      ) : (
        <>
          <ul className="list">
            {lines.map((line) => (
              <CartRow key={line.id} line={line} />
            ))}
          </ul>

          <DestinationPicker />

          <div className="card">
            <h3>Check out</h3>
            {target.kind === "container" && (
              <>
                <p className="muted-note">
                  Which way the parts went. Every row moves the same way unless it says
                  otherwise, because one trip to a drawer is normally all takes or all
                  returns.
                </p>
                <div className="segmented" role="group" aria-label="Direction">
                  <button
                    type="button"
                    aria-pressed={direction === "take"}
                    onClick={() => setDirection("take")}
                  >
                    I took these out
                  </button>
                  <button
                    type="button"
                    aria-pressed={direction === "return"}
                    onClick={() => setDirection("return")}
                  >
                    I put these back
                  </button>
                </div>
              </>
            )}
            {target.kind === "unset" && (
              <p className="muted-note">
                Pick a destination above first. There is no default — the three write to
                entirely different things.
              </p>
            )}
            <button
              type="button"
              className="primary wide"
              disabled={busy || target.kind === "unset"}
              onClick={() => void checkout()}
            >
              {busy
                ? "Checking out…"
                : failing > 0
                  ? `Retry the ${failing} remaining line(s)`
                  : `Check out ${lines.length} line(s)`}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

/**
 * One row: what was chosen, how many, and why it is still here.
 *
 * The refusal is stated in words as well as being set apart, and the row keeps a
 * plain "not applied" badge — hue alone would leave a colour-blind reader with a
 * cart of identical rows and no way to tell which ones failed.
 */
function CartRow({ line }: { line: CartLine }) {
  const [text, setText] = useState<string | null>(null);
  const shown = text ?? String(fromMilli(line.qtyMilli));

  return (
    <li className="list-item">
      <div className="row">
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="title">
            <Link to={`/parts/${line.partId}`}>{line.partName}</Link>
          </div>
          <div className="sub">
            {line.mpn !== null && <span className="mono">{line.mpn}</span>}
            {line.designator !== null && ` · ${line.designator}`}
            {line.locationLabel !== null && ` · from ${line.locationLabel}`}
            {line.lotId === null && " · no particular package"}
          </div>
          {line.failure !== null && <span className="badge badge-bad">not applied</span>}
        </div>
        <label className="field" style={{ flex: "0 0 7rem", margin: 0 }}>
          <span>Quantity</span>
          <input
            type="number"
            min={0}
            step="any"
            value={shown}
            aria-label={`Quantity of ${line.partName}`}
            onChange={(event) => {
              // The typed text is kept as typed while it is being typed: parsing
              // every keystroke back into the store turns "10" into 1 the moment
              // a digit is deleted, and snaps a half-typed "1." to 1.
              setText(event.target.value);
              const milli = parseQtyToMilli(event.target.value);
              if (milli !== null && milli > 0) {
                shoppingCart.setQuantity(line.id, milli);
              }
            }}
            onBlur={() => setText(null)}
          />
        </label>
        <button
          type="button"
          className="danger"
          aria-label={`Remove ${line.partName}`}
          onClick={() => shoppingCart.remove(line.id)}
        >
          Remove
        </button>
      </div>
      <LotChoice line={line} />
      {line.failure !== null && (
        <Notice kind="warn" title="This line was not applied">
          <p style={{ margin: 0 }}>{line.failure.message}</p>
          {line.failure.reason !== null && (
            <p className="muted-note mono">{line.failure.reason}</p>
          )}
        </Notice>
      )}
    </li>
  );
}

/**
 * Which physical package this row's parts come out of.
 *
 * Search knows what part was picked, not which reel it comes off, so every row
 * added while browsing starts with no package — and a reservation *is* a hold on
 * a lot, so without a way to name one here the build destination refused every
 * row the app could produce and said "choose which package it comes from" with
 * nothing to choose it in. This is that control.
 *
 * The lots are fetched only when it is opened, and per row: a cart of twenty rows
 * must not fire twenty requests to render, and which package a part came out of is
 * a question about one row at a time.
 */
function LotChoice({ line }: { line: CartLine }) {
  const [open, setOpen] = useState(false);
  const part = useAsync<PartRead | null>(
    () => (open ? getPart(line.partId) : Promise.resolve(null)),
    [open, line.partId],
  );
  const lots = part.data?.lots ?? [];

  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)}>
        {line.lotId === null ? "Choose the package" : "Change the package"}
      </button>
    );
  }

  return (
    <div className="stack" style={{ marginTop: "0.3rem" }}>
      <ErrorBanner error={part.error} fallback="That part's packages could not be listed." />
      <label className="field">
        <span>Package</span>
        <select
          value={line.lotId ?? ""}
          aria-label={`Package for ${line.partName}`}
          onChange={(event) => {
            const id = Number(event.target.value);
            const lot = lots.find((candidate) => candidate.id === id);
            shoppingCart.setLot(
              line.id,
              lot === undefined
                ? null
                : {
                    lotId: lot.id,
                    locationId: lot.location_id,
                    label: lot.location_label_path ?? null,
                  },
            );
            setOpen(false);
          }}
        >
          <option value="">No particular package</option>
          {lots.map((lot) => (
            <option key={lot.id} value={lot.id}>
              {`${lot.location_label_path ?? `lot ${lot.id}`} — ${formatQty(lot.qty_milli)}`}
              {lot.date_code === null ? "" : ` · ${lot.date_code}`}
            </option>
          ))}
        </select>
      </label>
      {!part.loading && lots.length === 0 && (
        <p className="muted-note">
          No stock of this part is recorded anywhere, so there is no package to hold.
        </p>
      )}
    </div>
  );
}

/** What came back, in the terms the user asked the question in. */
function Result({ outcome }: { outcome: CheckoutOutcome }) {
  const [undone, setUndone] = useState(false);
  const [undoing, setUndoing] = useState(false);
  const [error, setError] = useState<unknown>(null);

  if (outcome.notAttempted === "no_target") {
    return (
      <Notice kind="warn" title="Nowhere to send it">
        Choose a project, a build or a container first.
      </Notice>
    );
  }
  if (outcome.notAttempted === "empty_cart") {
    return <Notice kind="info" title="Nothing to check out" />;
  }

  async function undo(): Promise<void> {
    setUndoing(true);
    setError(null);
    try {
      // The batch shared one `group_uuid`, so the whole trip to the drawer comes
      // back in one call — and as compensating rows, never a deletion.
      await undoMovement({
        group_uuid_to_undo: outcome.groupUuid,
        client_op_id: uuid4(),
      });
      setUndone(true);
    } catch (cause) {
      setError(cause);
    } finally {
      setUndoing(false);
    }
  }

  return (
    <div className="stack" style={{ marginTop: "0.6rem" }}>
      <Notice
        kind={outcome.failed.length === 0 ? "ok" : "warn"}
        title={
          outcome.failed.length === 0
            ? `${outcome.applied} line(s) applied`
            : `${outcome.applied} applied, ${outcome.failed.length} still in the cart`
        }
      >
        <p style={{ margin: 0 }}>
          {outcome.failed.length === 0
            ? "Those rows have left the cart."
            : "The refused rows are still here, each saying why. Checking out again retries only those."}
          {outcome.replayed > 0 &&
            ` ${outcome.replayed} of them had already been recorded under the same key and were not applied twice.`}
        </p>
      </Notice>
      {/* **Only when this attempt wrote every applied row.** The group is minted
          per request, so a line the server *replayed* contributed no row to it —
          it belongs to the group the earlier attempt wrote. Offering one undo
          then either 404s (nothing in this group at all) or, worse, quietly
          reverses the newly-applied rows and leaves the replayed ones standing
          while reporting success. The rows are still undoable one at a time from
          the lot's history, which is what this says instead of pretending. */}
      {outcome.groupUuid !== null && outcome.applied > 0 && !undone && (
        outcome.replayed === 0 ? (
          <div className="row">
            <span className="muted-note" style={{ flex: 1 }}>
              Wrong drawer? The whole movement can be undone in one go.
            </span>
            <button type="button" disabled={undoing} onClick={() => void undo()}>
              {undoing ? "Undoing…" : "Undo the movement"}
            </button>
          </div>
        ) : (
          <p className="muted-note">
            Part of this checkout had already been recorded by an earlier attempt, so
            there is no single movement to undo. Reverse the rows you did not mean from
            each lot&apos;s history.
          </p>
        )
      )}
      {undone && <Notice kind="ok" title="Undone" />}
      <ErrorBanner error={error} fallback="That movement could not be undone." />
    </div>
  );
}

// ----------------------------------------------------------- destinations --

function DestinationPicker() {
  const target = useCartTarget();
  const [kind, setKind] = useState<Kind | null>(
    target.kind === "unset" ? null : (target.kind as Kind),
  );

  /**
   * Switching which kind of destination is on show **forgets the old one.**
   *
   * The chosen kind is local state and the target is in the cart, so without this
   * the two could disagree: pick a project, then press "Take or put back", and the
   * container panel would render with nothing chosen while the cart still pointed
   * at the project — and Check out, whose only guard is "some target", would write
   * BOM lines. "Nothing is guessed between them" is ADR 0007's rule, and a
   * destination the user has navigated away from is not a choice they are still
   * making. Losing it costs one tap.
   */
  function choose(value: Kind): void {
    setKind(value);
    if (target.kind !== "unset" && target.kind !== value) {
      shoppingCart.setTarget(NO_TARGET);
    }
  }

  return (
    <div className="card">
      <h3>Where is this going?</h3>
      <div className="segmented" role="group" aria-label="Destination">
        {KINDS.map((value) => (
          <button
            key={value}
            type="button"
            aria-pressed={kind === value}
            onClick={() => choose(value)}
          >
            {KIND_LABELS[value]}
          </button>
        ))}
      </div>
      {kind === "project" && <ProjectDestination />}
      {kind === "build" && <BuildDestination />}
      {kind === "container" && <ContainerDestination />}
      {kind === null && (
        <p className="muted-note">
          A BOM line is a requirement, a reservation is a hold on a real package, and a
          movement is stock actually changing hands. Nothing is guessed between them.
        </p>
      )}
    </div>
  );
}

/** Every project, in one request — `listProjects` is already the whole list. */
function useProjects(): { readonly data: ProjectRead[] | null; readonly error: unknown } {
  const projects = useAsync(() => listProjects({ limit: 200 }), []);
  return { data: projects.data?.projects ?? null, error: projects.error };
}

function ProjectDestination() {
  const target = useCartTarget();
  const { data, error } = useProjects();
  const chosen = target.kind === "project" ? target.projectId : null;

  return (
    <div className="stack">
      <p className="muted-note">
        Each row becomes a BOM line, matched to the part you picked — a part chosen by
        hand out of search is the definition of a confirmed match, so none of these land
        in a review queue. Quantities are read as per assembly.
      </p>
      <ErrorBanner error={error} fallback="The projects could not be listed." />
      <label className="field">
        <span>Project</span>
        <select
          value={chosen ?? ""}
          onChange={(event) => {
            const id = Number(event.target.value);
            const project = data?.find((candidate) => candidate.id === id);
            shoppingCart.setTarget(
              project === undefined
                ? NO_TARGET
                : { kind: "project", projectId: project.id, label: project.name },
            );
          }}
        >
          <option value="">Choose a project…</option>
          {(data ?? []).map((project) => (
            <option key={project.id} value={project.id}>
              {project.name}
              {project.revision !== null && ` (${project.revision})`}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

function BuildDestination() {
  const target = useCartTarget();
  const { data, error } = useProjects();
  const [projectId, setProjectId] = useState<number | null>(null);
  // The builds come with the project, not from an endpoint of their own —
  // `ProjectRead.builds` is exactly this list, newest first.
  const project = useAsync<ProjectRead | null>(
    () => (projectId === null ? Promise.resolve(null) : getProject(projectId)),
    [projectId],
  );
  const chosen = target.kind === "build" ? target.buildId : null;

  return (
    <div className="stack">
      <p className="muted-note">
        Each row becomes a hold on the package it names, which is what a reservation is —
        so a row with no particular package chosen will be refused rather than guessed
        at. Name one with &ldquo;Choose the package&rdquo; on the row. Sending them out to
        the project box is the next step, on the build&apos;s own screen.
      </p>
      <ErrorBanner error={error ?? project.error} fallback="Those builds could not be listed." />
      <label className="field">
        <span>Project</span>
        <select
          value={projectId ?? ""}
          onChange={(event) => setProjectId(Number(event.target.value) || null)}
        >
          <option value="">Choose a project…</option>
          {(data ?? []).map((candidate) => (
            <option key={candidate.id} value={candidate.id}>
              {candidate.name}
            </option>
          ))}
        </select>
      </label>
      {project.data !== null && (
        <label className="field">
          <span>Build</span>
          <select
            value={chosen ?? ""}
            onChange={(event) => {
              const id = Number(event.target.value);
              const build = project.data?.builds.find((candidate) => candidate.id === id);
              shoppingCart.setTarget(
                build === undefined
                  ? NO_TARGET
                  : { kind: "build", buildId: build.id, label: buildLabel(build) },
              );
            }}
          >
            <option value="">Choose a build…</option>
            {project.data.builds.map((build) => (
              <option key={build.id} value={build.id}>
                {buildLabel(build)}
              </option>
            ))}
          </select>
        </label>
      )}
      {project.data !== null && project.data.builds.length === 0 && (
        <Notice kind="info" title="That project has no builds yet">
          A build is what carries "how many boards", and therefore what turns a BOM into
          quantities. Start one on{" "}
          <Link to={`/projects/${project.data.id}`}>the project&apos;s screen</Link>.
        </Notice>
      )}
    </div>
  );
}

function buildLabel(build: BuildRead): string {
  const label = build.label === null ? "" : ` — ${build.label}`;
  return `Build #${build.build_no}${label} (×${build.assembly_count})`;
}

/**
 * The container, scanned or typed.
 *
 * Both paths end in the same `resolveScan` call, so a typed short ID and a decoded
 * QR are the same request with the same refusals — and a code that resolves to a
 * part or a lot rather than a container is refused *here*, in terms of what the
 * user was choosing, instead of at checkout as a validation error about a field
 * they never saw.
 */
function ContainerDestination() {
  const capabilities = detectCapabilities();
  const target = useCartTarget();
  const [cameraOn, setCameraOn] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [rejected, setRejected] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function resolve(code: string, symbology: string): Promise<void> {
    setBusy(true);
    setError(null);
    setRejected(null);
    try {
      const response = await resolveScan({ code, symbology });
      const found = response.target;
      if (found === null || found === undefined) {
        setRejected(`Nothing here matches ${code}.`);
        return;
      }
      if (found.entity_type !== "location") {
        setRejected(
          `That code is a ${found.entity_type.replace("_", " ")}, not a container. ` +
            "Scan the drawer or bin the parts came out of.",
        );
        return;
      }
      shoppingCart.setTarget({
        kind: "container",
        locationId: found.entity_pk,
        label: found.label_path ?? found.label,
      });
      setCameraOn(false);
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  const scanner = useScanner({
    active: cameraOn,
    onDecode: (text, symbology) => void resolve(text, symbology),
  });

  return (
    <div className="stack">
      <p className="muted-note">
        No project involved: each row is stock leaving this container or coming back to
        it. A row that named a package moves that package; a row that did not is resolved
        against what this container holds right now, and refused rather than guessed if it
        holds two lots of the part.
      </p>
      {target.kind === "container" && (
        <Notice kind="ok" title="Container chosen">
          <p style={{ margin: 0 }}>{target.label}</p>
        </Notice>
      )}
      {capabilities.camera ? (
        <>
          {cameraOn && (
            <Viewfinder
              videoRef={scanner.videoRef}
              status={scanner.status}
              message={scanner.message}
              unavailableNotice={cameraNotice(capabilities)}
              hint="Hold the container's label inside the box"
            />
          )}
          <button type="button" onClick={() => setCameraOn(!cameraOn)}>
            {cameraOn ? "Stop camera" : "Scan the container"}
          </button>
        </>
      ) : (
        <Notice kind="warn" title="No camera here">
          <p style={{ margin: 0 }}>{cameraNotice(capabilities)}</p>
        </Notice>
      )}
      <CodeEntry
        label="Container short ID"
        placeholder="4K7T-92M8"
        busy={busy}
        onSubmit={(code) => void resolve(code, "manual")}
      />
      {rejected !== null && <Notice kind="warn" title="Not a container">{rejected}</Notice>}
      <ErrorBanner error={error} fallback="That code could not be resolved." />
    </div>
  );
}
