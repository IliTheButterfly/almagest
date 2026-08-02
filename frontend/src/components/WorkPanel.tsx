/**
 * The side panel — ADR 0010's "the side panel is the feature".
 *
 * A tab strip of the open targets, and under the focused one two sections:
 * **already in this project** (derived, per ADR 0004 — nothing new is stored to
 * render it) beside **currently adding** (that tab's uncommitted lines). The
 * second alone is what #40's cart could show, and it is why that cart could not
 * answer the question the feature exists for: "where am I in a job I am halfway
 * through" is a *diff*, not a list.
 *
 * Three properties here are mitigations rather than decoration, and the ADR names
 * all three as required together:
 *
 * 1. **The strip is always visible while anything is open.** The focused tab
 *    decides where a take is attributed, so a mode with no visible indicator is
 *    precisely the failure the panel exists to prevent. The *sections* collapse —
 *    they must not fight the shelf-side phone layout — but the strip does not.
 * 2. **A tab holding uncommitted lines is marked in the strip even when it is not
 *    focused**, or the second tab becomes the invisible state.
 * 3. **Closing a tab with uncommitted lines asks.** The store refuses and says how
 *    many; only a deliberate second press discards them. Same rule, same wording
 *    as `OpenTargetButton`, because a user who learns it in one place has learnt
 *    it in both.
 *
 * What is deliberately *not* here: the per-line detail of a shortage report or a
 * BOM. The panel orients; the build and project screens are where a line is
 * worked on, and a sidebar that reimplements either would be a second
 * implementation to keep in step. Rows link across rather than duplicating.
 */

import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import {
  getShortages,
  listBomLines,
  type BomLineList,
  type ShortageResponse,
} from "../lib/api/client";
import { checkoutCart, type CheckoutOutcome } from "../lib/cart/checkout";
import { describeCommit, describeTarget } from "../lib/cart/describe";
import { carts } from "../lib/cart/registry";
import { netQtyMilli, type CartLine } from "../lib/cart/cart";
import { useCartLines } from "../lib/cart/useCart";
import { formatQty } from "../lib/format";
import { useAsync } from "../lib/hooks/useAsync";
import { useFocusedTarget, useOpenTargets } from "../lib/projectcontext/hooks";
import { routeForTarget } from "../lib/scanctx/route";
import { scanContext, type ScanRecord } from "../lib/scanctx/store";
import { useHasReader, useScans } from "../lib/scanctx/useScanContext";
import { openTargets } from "../lib/projectcontext/store";
import { targetKey, type WorkTarget } from "../lib/projectcontext/target";
import { ErrorBanner, Loading, Notice } from "./Feedback";

/**
 * How many rows of an existing record the panel draws before it stops.
 *
 * A cap with a link out, and it **says** how many it is not showing: a list that
 * silently ends at eight reads as a complete record with eight lines in it, which
 * is the one thing a screen whose job is orientation must never do.
 */
const PREVIEW_ROWS = 8;

export function WorkPanel() {
  const open = useOpenTargets();
  const focused = useFocusedTarget();
  const scans = useScans();
  const hasReader = useHasReader();
  const [showScans, setShowScans] = useState(false);

  // **The panel exists when there is somewhere for a scan to come from**, not
  // only when there is work in it. On the bench a reader is always attached, so
  // the Scanned tab is always there and a tap has somewhere to land before you
  // have thought about where you wanted it. On a phone with no bridge and no
  // open work there is nothing to draw, and drawing an empty panel anyway would
  // be an affordance that cannot do anything — ADR 0003's rule.
  const scanTabExists = hasReader || scans.length > 0;
  if ((open.length === 0 || focused === null) && !scanTabExists) {
    return null;
  }

  // A scan arriving takes the panel: it is the most recent thing you did, and
  // the reason you looked. Switching back to a cart tab is one tap and sticks.
  const onScanTab = showScans || focused === null;

  return (
    <aside className="work-panel" aria-label="What you are working on">
      <div className="work-tabs" role="tablist" aria-label="Scans and open work">
        {scanTabExists && (
          <button
            type="button"
            role="tab"
            aria-selected={onScanTab}
            className={`work-tab${onScanTab ? " is-focused" : ""}`}
            onClick={() => setShowScans(true)}
          >
            Scanned
            {scans.length > 0 && <span className="badge">{scans.length}</span>}
          </button>
        )}
      </div>

      {focused !== null && (
        <TabStrip
          open={open}
          focused={focused}
          onPick={() => setShowScans(false)}
        />
      )}

      {onScanTab ? (
        <ScannedTab scans={scans} hasReader={hasReader} />
      ) : (
        /* Keyed on the target so switching tabs remounts the sections: the two
           halves below are about one target, and carrying one tab's expanded
           state or loaded report onto another would be showing you the wrong
           record under the right name. */
        focused !== null && <TabBody key={targetKey(focused)} target={focused} />
      )}
    </aside>
  );
}

/**
 * What the reader has seen, newest first.
 *
 * Read-only on purpose. A row offers to *open* the thing and to dismiss itself;
 * placing a scan into a field is the field's affordance, not this panel's,
 * because only the field knows whether the scan is the right kind of thing and
 * what it would be replacing. A panel that pushed values into whatever screen
 * was open would be the auto-fill this design deliberately refused.
 */
function ScannedTab({
  scans,
  hasReader,
}: {
  scans: readonly ScanRecord[];
  hasReader: boolean;
}) {
  if (scans.length === 0) {
    return (
      <div className="stack">
        <p className="muted-note" style={{ margin: 0 }}>
          {hasReader
            ? "Hold a tag to the reader. What it reads lands here, and any field that can take a scan will offer it."
            : "Nothing scanned yet."}
        </p>
      </div>
    );
  }

  return (
    <ul className="list">
      {scans.map((scan) => {
        const route = scan.target === null ? null : routeForTarget(scan.target);
        const label = scan.target?.label_path ?? scan.target?.label ?? scan.code;
        return (
          <li key={scan.id} className="list-item">
            <div className="row">
              <span className="title" style={{ flex: 1, overflowWrap: "anywhere" }}>
                {label}
              </span>
              {scan.target === null ? (
                <span className="badge badge-warn">nothing matched</span>
              ) : (
                <span className="badge">{scan.target.entity_type}</span>
              )}
            </div>
            <div className="sub mono" style={{ overflowWrap: "anywhere" }}>
              {scan.code}
            </div>
            <div className="row">
              {route !== null && <Link to={route}>Open</Link>}
              <span className="spacer" />
              <button
                type="button"
                onClick={() => {
                  scanContext.remove(scan.id);
                }}
              >
                Dismiss
              </button>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function TabStrip({
  open,
  focused,
  onPick,
}: {
  open: readonly WorkTarget[];
  focused: WorkTarget;
  onPick: () => void;
}) {
  const [asking, setAsking] = useState<{ target: WorkTarget; lines: number } | null>(null);

  function close(target: WorkTarget, discardLines: boolean): void {
    const outcome = openTargets.close(targetKey(target), { discardLines });
    setAsking(outcome.closed ? null : { target, lines: outcome.lines });
  }

  return (
    <div className="stack">
      <div className="work-tabs" role="tablist" aria-label="Open projects and builds">
        {open.map((target) => (
          <TargetTab
            key={targetKey(target)}
            target={target}
            focused={targetKey(target) === targetKey(focused)}
            onPick={onPick}
            onClose={() => {
              close(target, false);
            }}
          />
        ))}
      </div>

      {asking !== null && (
        <Notice kind="warn" title="That tab still has lines nobody has committed">
          <p style={{ margin: 0 }}>
            {asking.lines === 1 ? "One line" : `${asking.lines} lines`} in{" "}
            {describeTarget(asking.target)} have not been committed, so nothing has been
            written for them. Commit them below, or discard them here.
          </p>
          <div className="row">
            <button
              type="button"
              className="danger"
              onClick={() => {
                close(asking.target, true);
              }}
            >
              Discard {asking.lines === 1 ? "it" : "them"} and close
            </button>
            <button
              type="button"
              onClick={() => {
                setAsking(null);
              }}
            >
              Keep the tab
            </button>
          </div>
        </Notice>
      )}
    </div>
  );
}

/**
 * One tab: focus it, and close it.
 *
 * The uncommitted count is rendered for **every** tab, focused or not — property
 * 2 in the module comment. It is a number rather than a dot because "3" is
 * actionable and a dot is a thing you have to go and investigate.
 */
function TargetTab({
  target,
  focused,
  onClose,
  onPick,
}: {
  target: WorkTarget;
  focused: boolean;
  onClose: () => void;
  /** Leaves the Scanned tab when a cart tab is chosen. */
  onPick: () => void;
}) {
  const lines = useCartLines(carts.for(target));
  const waiting = lines.length;
  const name = describeTarget(target);

  return (
    <div className={focused ? "work-tab is-focused" : "work-tab"}>
      <button
        type="button"
        role="tab"
        aria-selected={focused}
        onClick={() => {
          openTargets.focus(targetKey(target));
          onPick();
        }}
      >
        <span className="work-tab-name">{name}</span>
        {waiting > 0 && (
          <span className="badge badge-accent" aria-label={`${waiting} to commit`}>
            {waiting}
          </span>
        )}
      </button>
      <button type="button" className="work-tab-close" aria-label={`Close ${name}`} onClick={onClose}>
        ×
      </button>
    </div>
  );
}

/**
 * The two sections under the focused tab.
 *
 * Split by target kind rather than by inspecting the response, because the two
 * reads are genuinely different questions — a build asks what is reserved, staged
 * and consumed against it; a project asks what its bill of materials says — and a
 * component that fetched one and rendered the other by sniffing a field would be
 * one renamed key away from drawing the wrong record.
 */
function TabBody({ target }: { target: WorkTarget }) {
  const [showExisting, setShowExisting] = useState(true);
  const [showAdding, setShowAdding] = useState(true);
  /**
   * Bumped by a commit, and a dependency of the section above it.
   *
   * Committing is the one thing that moves a line from "currently adding" to
   * "already in", so leaving the upper section on its pre-commit read would show
   * the two halves disagreeing about work the user just did.
   */
  const [committedAt, setCommittedAt] = useState(0);
  const lines = useCartLines(carts.for(target));

  return (
    <div className="stack">
      {target.kind === "build" ? (
        <BuildSection
          target={target}
          refreshKey={committedAt}
          open={showExisting}
          onToggle={() => {
            setShowExisting(!showExisting);
          }}
        />
      ) : (
        <ProjectSection
          target={target}
          refreshKey={committedAt}
          open={showExisting}
          onToggle={() => {
            setShowExisting(!showExisting);
          }}
        />
      )}

      <Section
        title="Currently adding"
        count={lines.length}
        open={showAdding}
        onToggle={() => {
          setShowAdding(!showAdding);
        }}
      >
        <Adding
          target={target}
          lines={lines}
          onCommitted={() => {
            setCommittedAt(committedAt + 1);
          }}
        />
      </Section>
    </div>
  );
}

function BuildSection({
  target,
  refreshKey,
  open,
  onToggle,
}: {
  target: Extract<WorkTarget, { kind: "build" }>;
  refreshKey: number;
  open: boolean;
  onToggle: () => void;
}) {
  const report = useAsync<ShortageResponse>(
    () => getShortages(target.buildId),
    [target.buildId, refreshKey],
  );
  return (
    <Section title={`Already in ${describeTarget(target)}`} open={open} onToggle={onToggle}>
      <ErrorBanner error={report.error} fallback="The shortage report could not be loaded." />
      {report.data === null ? (
        report.error === null ? (
          <Loading what="what is already in this build" />
        ) : null
      ) : (
        <BuildStanding report={report.data} buildId={target.buildId} />
      )}
    </Section>
  );
}

function ProjectSection({
  target,
  refreshKey,
  open,
  onToggle,
}: {
  target: Extract<WorkTarget, { kind: "project" }>;
  refreshKey: number;
  open: boolean;
  onToggle: () => void;
}) {
  const bom = useAsync<BomLineList>(
    () => listBomLines(target.projectId),
    [target.projectId, refreshKey],
  );
  return (
    <Section title={`Already in ${describeTarget(target)}`} open={open} onToggle={onToggle}>
      <ErrorBanner error={bom.error} fallback="The bill of materials could not be loaded." />
      {bom.data === null ? (
        bom.error === null ? (
          <Loading what="what is already in this project" />
        ) : null
      ) : (
        <BomStanding bom={bom.data} projectId={target.projectId} />
      )}
    </Section>
  );
}

/** A collapsible block. `<details>` would do it, but not the count in the header. */
function Section({
  title,
  count,
  open,
  onToggle,
  children,
}: {
  title: string;
  count?: number;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <section className="card">
      <div className="row">
        {/* Takes the slack so the toggle stays on the heading's line: a real
            target is named "Build #1 — rev B x3", which is long enough to push a
            button onto its own row and make the panel look broken. */}
        <h3 style={{ margin: 0, flex: 1, minWidth: 0 }}>{title}</h3>
        {count !== undefined && count > 0 && <span className="badge badge-accent">{count}</span>}
        <span className="spacer" />
        <button type="button" aria-expanded={open} onClick={onToggle}>
          {open ? "Hide" : "Show"}
        </button>
      </div>
      {open && children}
    </section>
  );
}

/**
 * A build's standing: held, set aside, built in — ADR 0010's three, and ADR 0004's
 * derivation. The wording is `BuildScreen`'s, because the same number under two
 * names in two places is two numbers as far as anyone reading it is concerned.
 */
function BuildStanding({ report, buildId }: { report: ShortageResponse; buildId: number }) {
  const held = report.lines.reduce((total, line) => total + line.reserved_milli, 0);
  const staged = report.lines.reduce((total, line) => total + line.staged_milli, 0);
  const built = report.lines.reduce((total, line) => total + line.consumed_milli, 0);
  const short = report.lines.filter((line) => line.needed_milli > 0);
  const shown = short.slice(0, PREVIEW_ROWS);

  return (
    <div className="stack">
      <p className="muted-note" style={{ margin: 0 }}>
        {report.assembly_count === 1 ? "One assembly" : `${report.assembly_count} assemblies`} ·{" "}
        {report.is_buildable ? "everything it needs is accounted for" : "not yet buildable"}
      </p>
      <ul className="list">
        <li className="list-item">
          <div className="row">
            <span className="title">{formatQty(held)}</span>
            <span className="sub">held in a bin</span>
          </div>
        </li>
        <li className="list-item">
          <div className="row">
            <span className="title">{formatQty(staged)}</span>
            <span className="sub">set aside for this project</span>
          </div>
        </li>
        <li className="list-item">
          <div className="row">
            <span className="title">{formatQty(built)}</span>
            <span className="sub">built in</span>
          </div>
        </li>
      </ul>
      {short.length > 0 && (
        <>
          <p className="muted-note" style={{ margin: 0 }}>
            Still needed:
          </p>
          <ul className="list">
            {shown.map((line) => (
              <li className="list-item" key={line.bom_line_id}>
                <div className="row">
                  <span className="title">Line {line.line_no}</span>
                  <span className="spacer" />
                  <span className="sub">{formatQty(line.needed_milli)} still needed</span>
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
      <p className="muted-note" style={{ margin: 0 }}>
        {short.length > shown.length &&
          `${short.length - shown.length} more line${
            short.length - shown.length === 1 ? "" : "s"
          } not shown here. `}
        <Link to={`/builds/${buildId}`}>Open the build</Link>
      </p>
    </div>
  );
}

/** A project's standing is its BOM: what the project asks for, matched or not. */
function BomStanding({ bom, projectId }: { bom: BomLineList; projectId: number }) {
  const shown = bom.lines.slice(0, PREVIEW_ROWS);
  const unmatched = bom.lines.filter((line) => line.part_id === null).length;

  return (
    <div className="stack">
      <p className="muted-note" style={{ margin: 0 }}>
        {bom.total === 1 ? "One line" : `${bom.total} lines`}
        {unmatched > 0 && ` · ${unmatched} not matched to a part yet`}
      </p>
      <ul className="list">
        {shown.map((line) => (
          <li className="list-item" key={line.id}>
            <div className="title">
              {line.mpn_raw ?? line.description ?? `Line ${line.line_no}`}
            </div>
            <div className="sub">
              {formatQty(line.qty_per_assembly_milli)} per assembly
              {line.designators !== null && ` · ${line.designators}`}
            </div>
          </li>
        ))}
      </ul>
      <p className="muted-note" style={{ margin: 0 }}>
        {bom.total > shown.length &&
          `${bom.total - shown.length} more line${
            bom.total - shown.length === 1 ? "" : "s"
          } not shown here. `}
        <Link to={`/projects/${projectId}/bom`}>Open the bill of materials</Link>
      </p>
    </div>
  );
}

/**
 * "Currently adding": the uncommitted lines, and the one button that applies them.
 *
 * The commit is `checkoutCart`, unchanged from #40 — including the rule that
 * carries the whole feature: **a line whose stock has moved fails that line and
 * not the batch**, and stays in the record with the reason on it. So a partial
 * outcome is the normal one and is reported as such, rather than as an error that
 * throws the rest away.
 */
function Adding({
  target,
  lines,
  onCommitted,
}: {
  target: WorkTarget;
  lines: readonly CartLine[];
  onCommitted: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<CheckoutOutcome | null>(null);
  const [error, setError] = useState<unknown>(null);
  const cart = carts.for(target);
  const net = netQtyMilli(lines);

  async function commit(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const result = await checkoutCart(cart);
      setOutcome(result);
      if (result.applied > 0) {
        onCommitted();
      }
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  if (lines.length === 0) {
    /*
     * Emptied — which is what a clean commit *looks* like, so the outcome has to
     * survive into this state. Dropping it here would mean the one press that
     * actually applied everything was the one that told the user nothing, and the
     * next thing they read would be "nothing yet".
     */
    return (
      <div className="stack">
        {outcome !== null && <CommitOutcomeNotice outcome={outcome} />}
        <p className="muted-note" style={{ margin: 0 }}>
          Nothing yet. Taking from a lot while this tab is focused adds it here instead of
          writing it to the ledger.
        </p>
      </div>
    );
  }

  return (
    <div className="stack">
      <ul className="list">
        {lines.map((line) => (
          <li className="list-item" key={line.id}>
            <div className="row">
              <span className="title">{line.partName}</span>
              <span className="spacer" />
              <button
                type="button"
                aria-label={`Remove ${line.partName}`}
                onClick={() => {
                  cart.remove(line.id);
                }}
              >
                Remove
              </button>
            </div>
            <div className="sub">
              {line.direction === "return" ? "putting back " : ""}
              {formatQty(line.qtyMilli)}
              {line.locationLabel !== null && ` · ${line.locationLabel}`}
            </div>
            {line.failure !== null && (
              <div className="sub">
                <span className="badge badge-bad">refused</span> {line.failure.message}
              </div>
            )}
          </li>
        ))}
      </ul>

      <p className="muted-note" style={{ margin: 0 }}>
        {net < 0
          ? `${formatQty(Math.abs(net))} going back on the shelf, net.`
          : `${formatQty(net)} off the shelf for this, net.`}
      </p>

      <ErrorBanner error={error} fallback="Those lines were not committed." />
      {outcome !== null && <CommitOutcomeNotice outcome={outcome} />}

      <button
        type="button"
        className="primary wide"
        disabled={busy}
        onClick={() => {
          void commit();
        }}
      >
        {busy ? "Committing…" : describeCommit(target)}
      </button>
    </div>
  );
}

function CommitOutcomeNotice({ outcome }: { outcome: CheckoutOutcome }) {
  if (outcome.notAttempted === "empty_cart") {
    return null;
  }
  if (outcome.failed.length === 0) {
    const applied = outcome.applied === 1 ? "One line" : `${outcome.applied} lines`;
    const replayed =
      outcome.replayed > 0 ? `, ${outcome.replayed} of which had already been recorded` : "";
    return <Notice kind="ok">{`${applied} committed${replayed}.`}</Notice>;
  }
  return (
    <Notice kind="warn" title="Some lines were not applied">
      <p style={{ margin: 0 }}>
        {outcome.applied === 0
          ? "Nothing was applied."
          : `${outcome.applied} applied and are gone from this list.`}{" "}
        {outcome.failed.length === 1 ? "One line is" : `${outcome.failed.length} lines are`} still
        here with the reason on the row. Fix them and press again — the ones that already went
        through will not be applied twice.
      </p>
    </Notice>
  );
}
